"""观察窗口与评分批次计算（纯函数）。

批次三元组在计算时固定：
1. 观察窗口（window_start / window_end，按配置时区锚定到 UTC）；
2. 输入游标（input_cursor，纳入的最大上报行，迟到数据落在游标之后）；
3. 规则版本（rule_version，决定指标口径与权重）。

历史批次结果不可变；迟到数据只能以 revision_of 指向旧批次生成修订版。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence

from ..core.clock import get_zone
from .metrics import (
    METRIC_CODES,
    Incident,
    MetricComputation,
    MetricSpec,
    compute_metric,
)


@dataclass(frozen=True)
class ObservationWindow:
    """半开区间 [start, end)；UTC 表达，边界由配置时区的本地午夜锚定。"""

    start: datetime
    end: datetime
    timezone: str

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("观察窗口边界必须包含时区")
        if self.end <= self.start:
            raise ValueError("观察窗口结束必须晚于开始")

    def contains(self, moment: datetime) -> bool:
        instant = moment.astimezone(timezone.utc)
        return self.start <= instant < self.end

    def to_document(self) -> dict[str, str]:
        return {
            "window_start": _iso(self.start),
            "window_end": _iso(self.end),
            "timezone": self.timezone,
        }


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _local_midnight_as_utc(day: date, tz_name: str) -> datetime:
    zone = get_zone(tz_name)
    return datetime.combine(day, time(0, 0)).replace(tzinfo=zone).astimezone(timezone.utc)


def window_bounds(
    start_day: date | str,
    end_exclusive_day: date | str,
    tz_name: str,
) -> ObservationWindow:
    """按本地日期构造半开窗口：[start_day 00:00, end_exclusive_day 00:00)。

    跨时区（含 DST 切换）下，窗口长度可能不是整数天，但边界在各学院
    本地日历上始终对齐午夜。
    """
    if isinstance(start_day, str):
        start_day = date.fromisoformat(start_day)
    if isinstance(end_exclusive_day, str):
        end_exclusive_day = date.fromisoformat(end_exclusive_day)
    start = _local_midnight_as_utc(start_day, tz_name)
    end = _local_midnight_as_utc(end_exclusive_day, tz_name)
    return ObservationWindow(start=start, end=end, timezone=tz_name)


def window_for_span(
    start_day: date | str,
    days: int,
    tz_name: str,
) -> ObservationWindow:
    """执行确定性的业务处理。"""
    if isinstance(start_day, str):
        start_day = date.fromisoformat(start_day)
    if days <= 0:
        raise ValueError("窗口天数必须为正")
    return window_bounds(start_day, start_day + timedelta(days=days), tz_name)


@dataclass(frozen=True)
class MetricResult:
    metric_code: str
    value: float | None
    score: float | None
    sample_size: int
    confident: bool
    evidence: dict[str, Any]

    @classmethod
    def from_computation(cls, comp: MetricComputation) -> "MetricResult":
        return cls(
            metric_code=comp.code,
            value=comp.value,
            score=comp.score,
            sample_size=comp.sample_size,
            confident=comp.confident,
            evidence=comp.evidence,
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "metric_code": self.metric_code,
            "value": self.value,
            "score": self.score,
            "sample_size": self.sample_size,
            "confident": self.confident,
            "evidence": self.evidence,
        }


@dataclass
class CollegeScore:
    college_id: str
    metrics: dict[str, MetricResult]
    composite: float | None
    confident: bool

    def to_document(self) -> dict[str, Any]:
        return {
            "college_id": self.college_id,
            "composite_score": self.composite,
            "confident": self.confident,
            "metrics": [self.metrics[code].to_document() for code in METRIC_CODES],
        }


@dataclass
class BatchComputation:
    batch_id: str
    window: ObservationWindow
    rule_version: str
    input_cursor: str | None
    specs: dict[str, MetricSpec]
    colleges: dict[str, CollegeScore] = field(default_factory=dict)
    revision_of: str | None = None

    def to_manifest(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "rule_version": self.rule_version,
            "input_cursor": self.input_cursor,
            "revision_of": self.revision_of,
            "window": self.window.to_document(),
            "specs": [self.specs[c].to_document() for c in METRIC_CODES],
            "colleges": [
                self.colleges[cid].to_document() for cid in sorted(self.colleges)
            ],
        }


def _composite(
    metrics: Mapping[str, MetricResult], specs: Mapping[str, MetricSpec]
) -> tuple[float | None, bool]:
    """对可得指标按权重归一化加权；任一参与指标低置信则整体低置信。"""
    weighted_sum = 0.0
    weight_used = 0.0
    confident = True
    available = 0
    for code in METRIC_CODES:
        result = metrics.get(code)
        if result is None or result.score is None:
            confident = False
            continue
        available += 1
        weight = specs[code].weight
        weighted_sum += result.score * weight
        weight_used += weight
        if not result.confident:
            confident = False
    if available == 0 or weight_used <= 0:
        return None, False
    return round(weighted_sum / weight_used, 6), confident


def calculate_batch(
    *,
    batch_id: str,
    incidents: Sequence[Incident],
    window: ObservationWindow,
    specs: Mapping[str, MetricSpec],
    input_cursor: str | None = None,
    revision_of: str | None = None,
) -> BatchComputation:
    """固定窗口 / 游标 / 规则版本，逐学院计算分项证据与置信标记。

    input_cursor 为纳入的最大游标值（report_id 字典序），用于在导入
    仍在继续时冻结输入；游标之后的迟到数据不影响本批次。
    """
    rule_version = next(iter(specs.values())).rule_version
    by_college: dict[str, list[Incident]] = {}
    for incident in incidents:
        if input_cursor is not None and incident.report_id > input_cursor:
            continue
        if not window.contains(incident.occurred_at):
            continue
        by_college.setdefault(incident.college_id, []).append(incident)

    colleges: dict[str, CollegeScore] = {}
    for college_id in sorted(by_college):
        college_incidents = by_college[college_id]
        metric_results: dict[str, MetricResult] = {}
        for code in METRIC_CODES:
            comp = compute_metric(code, college_incidents, specs[code])
            metric_results[code] = MetricResult.from_computation(comp)
        composite, confident = _composite(metric_results, specs)
        colleges[college_id] = CollegeScore(
            college_id=college_id,
            metrics=metric_results,
            composite=composite,
            confident=confident,
        )

    return BatchComputation(
        batch_id=batch_id,
        window=window,
        rule_version=rule_version,
        input_cursor=input_cursor,
        specs=dict(specs),
        colleges=colleges,
        revision_of=revision_of,
    )


def rank_colleges(
    computation: BatchComputation,
) -> list[dict[str, Any]]:
    """按综合分降序排名；同分以 college_id 升序兜底，空样本不参与排名。

    排名结果是批次输入的确定性函数，重跑得到完全一致的名次。没有任何
    可评分指标（空样本）的学院附在末尾，rank 为 None。
    """
    scorable = [c for c in computation.colleges.values() if c.composite is not None]
    unranked = [c for c in computation.colleges.values() if c.composite is None]
    ordered = sorted(
        scorable, key=lambda c: (-c.composite, c.college_id)  # type: ignore[operator]
    )

    standings: list[dict[str, Any]] = [
        {
            "rank": position,
            "college_id": college.college_id,
            "composite_score": college.composite,
            "confident": college.confident,
        }
        for position, college in enumerate(ordered, start=1)
    ]
    for college in sorted(unranked, key=lambda c: c.college_id):
        standings.append(
            {
                "rank": None,
                "college_id": college.college_id,
                "composite_score": None,
                "confident": False,
            }
        )
    return standings


def apply_source_corrections(
    incidents: Sequence[Incident],
    *,
    excluded_report_ids: Sequence[str] = (),
    recategorized: Mapping[str, str] | None = None,
) -> list[Incident]:
    """按经核实的来源修正输入：剔除误报，或更正其类别。"""
    excluded = set(excluded_report_ids)
    recategorized = dict(recategorized or {})
    corrected: list[Incident] = []
    for incident in incidents:
        if incident.report_id in excluded:
            continue
        if incident.report_id in recategorized:
            incident = Incident(
                report_id=incident.report_id,
                college_id=incident.college_id,
                category=recategorized[incident.report_id],
                occurred_at=incident.occurred_at,
                reported_at=incident.reported_at,
                fingerprint=incident.fingerprint,
            )
        corrected.append(incident)
    return corrected


def recompute_metric(
    *,
    incidents: Sequence[Incident],
    metric_code: str,
    spec: MetricSpec,
    excluded_report_ids: Sequence[str] = (),
    recategorized: Mapping[str, str] | None = None,
) -> MetricResult:
    """申诉核实后重算单一指标；仅作用于经核实的来源记录。"""
    corrected = apply_source_corrections(
        incidents,
        excluded_report_ids=excluded_report_ids,
        recategorized=recategorized,
    )
    comp = compute_metric(metric_code, corrected, spec)
    return MetricResult.from_computation(comp)


def scorecard_fingerprint(
    *,
    batch_id: str,
    rule_version: str,
    input_cursor: str | None,
    window: ObservationWindow,
    rankings: Sequence[Mapping[str, Any]],
) -> str:
    """对签发内容生成指纹，供重启恢复时识别已签发批次。"""
    canonical = json.dumps(
        {
            "batch_id": batch_id,
            "rule_version": rule_version,
            "input_cursor": input_cursor,
            "window": window.to_document(),
            "rankings": list(rankings),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()
