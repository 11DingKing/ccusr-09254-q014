"""指标定义与单指标计算（纯函数，不依赖数据库）。

三项校级指标：
- timeliness（及时率，越高越好）：在规定时限内上报的事件占比；
- duplicate_rate（重复率，越低越好）：同类别、同内容指纹的重复上报占比；
- conflict_rate（冲突率，越低越好）：同一内容指纹被以不同类别矛盾上报的占比。

口径参数固定在规则版本（MetricSpec.params）中，批次一经计算即引用
不可变的规则版本；口径调整只能发布新规则版本。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping

DEFAULT_RULE_VERSION = "rv-2026-09"

TIMELINESS = "timeliness"
DUPLICATE_RATE = "duplicate_rate"
CONFLICT_RATE = "conflict_rate"

METRIC_CODES: tuple[str, ...] = (TIMELINESS, DUPLICATE_RATE, CONFLICT_RATE)

# 低于该样本量的指标标记为低置信。
DEFAULT_MIN_SAMPLE = 5
# 及时上报时限（小时）。
DEFAULT_DEADLINE_HOURS = 24


@dataclass(frozen=True)
class Incident:
    """一条学院上报事件（计算的最小输入单元）。"""

    report_id: str
    college_id: str
    category: str
    occurred_at: datetime
    reported_at: datetime
    fingerprint: str

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None or self.reported_at.tzinfo is None:
            raise ValueError("incident 时间必须包含时区")


@dataclass(frozen=True)
class MetricSpec:
    """指标口径定义；同一 code 在不同规则版本下可有不同参数。"""

    code: str
    rule_version: str
    display_name: str
    direction: str  # higher=越高越好，lower=越低越好
    weight: float
    params: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.direction not in ("higher", "lower"):
            raise ValueError("direction 必须是 higher 或 lower")
        if not 0 <= self.weight <= 1:
            raise ValueError("权重必须落在 [0,1]")
        if self.code not in METRIC_CODES:
            raise ValueError(f"未知指标 {self.code}")

    def scale_score(self, value: float) -> float:
        """把 [0,1] 的指标值换算为 [0,100] 的得分。"""
        if self.direction == "higher":
            return round(value * 100, 6)
        return round((1 - value) * 100, 6)

    def to_document(self) -> dict[str, Any]:
        return {
            "metric_code": self.code,
            "rule_version": self.rule_version,
            "display_name": self.display_name,
            "direction": self.direction,
            "weight": self.weight,
            "params": dict(self.params),
        }


def build_default_specs(
    rule_version: str = DEFAULT_RULE_VERSION,
    *,
    weights: Mapping[str, float] | None = None,
    params: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, MetricSpec]:
    """构造某规则版本下的三项指标口径。"""
    weights = dict(weights or {TIMELINESS: 0.4, DUPLICATE_RATE: 0.3, CONFLICT_RATE: 0.3})
    params = {code: dict(overrides or {}) for code, overrides in (params or {}).items()}
    defaults: dict[str, tuple[str, str, dict[str, Any]]] = {
        TIMELINESS: (
            "及时上报率",
            "higher",
            {"deadline_hours": DEFAULT_DEADLINE_HOURS, "min_sample": DEFAULT_MIN_SAMPLE},
        ),
        DUPLICATE_RATE: (
            "重复上报率",
            "lower",
            {"min_sample": DEFAULT_MIN_SAMPLE},
        ),
        CONFLICT_RATE: (
            "类别冲突率",
            "lower",
            {"min_sample": DEFAULT_MIN_SAMPLE},
        ),
    }
    specs: dict[str, MetricSpec] = {}
    for code in METRIC_CODES:
        display_name, direction, base_params = defaults[code]
        merged_params = {**base_params, **params.get(code, {})}
        specs[code] = MetricSpec(
            code=code,
            rule_version=rule_version,
            display_name=display_name,
            direction=direction,
            weight=float(weights[code]),
            params=merged_params,
        )
    return specs


def derive_fingerprint(category: str, payload: Mapping[str, Any]) -> str:
    """依据类别与规范化载荷生成确定性内容指纹。"""
    canonical = json.dumps(
        {"category": category, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _lag_seconds(incident: Incident) -> int:
    return int(
        (
            incident.reported_at.astimezone(timezone.utc)
            - incident.occurred_at.astimezone(timezone.utc)
        ).total_seconds()
    )


def timeliness(
    incidents: list[Incident], spec: MetricSpec
) -> tuple[float | None, dict[str, Any]]:
    """执行确定性的业务处理。"""
    n = len(incidents)
    deadline = int(spec.params["deadline_hours"]) * 3600
    if n == 0:
        return None, {"deadline_hours": spec.params["deadline_hours"], "total": 0,
                      "on_time": 0, "late": [], "lag_seconds": {}}
    lags = {incident.report_id: _lag_seconds(incident) for incident in incidents}
    late = sorted(rid for rid, lag in lags.items() if lag > deadline)
    on_time = n - len(late)
    evidence = {
        "deadline_hours": spec.params["deadline_hours"],
        "total": n,
        "on_time": on_time,
        "late": late,
        "lag_seconds": dict(sorted(lags.items())),
    }
    return on_time / n, evidence


def duplicate_rate(
    incidents: list[Incident], spec: MetricSpec
) -> tuple[float | None, dict[str, Any]]:
    """同类别 + 同指纹的多次上报中，除首条外均计为重复。"""
    n = len(incidents)
    if n == 0:
        return None, {"total": 0, "duplicate": 0, "groups": []}
    groups: dict[tuple[str, str], list[str]] = {}
    for incident in incidents:
        groups.setdefault((incident.category, incident.fingerprint), []).append(
            incident.report_id
        )
    duplicated: list[dict[str, Any]] = []
    duplicate_count = 0
    for (category, fingerprint), report_ids in sorted(groups.items()):
        report_ids.sort()
        if len(report_ids) > 1:
            duplicate_count += len(report_ids) - 1
            duplicated.append(
                {
                    "category": category,
                    "fingerprint": fingerprint,
                    "count": len(report_ids),
                    "report_ids": report_ids,
                }
            )
    evidence = {"total": n, "duplicate": duplicate_count, "groups": duplicated}
    return duplicate_count / n, evidence


def conflict_rate(
    incidents: list[Incident], spec: MetricSpec
) -> tuple[float | None, dict[str, Any]]:
    """同一指纹被以多个不同类别上报时，组内全部记为冲突。"""
    n = len(incidents)
    if n == 0:
        return None, {"total": 0, "conflict": 0, "groups": []}
    by_fingerprint: dict[str, list[Incident]] = {}
    for incident in incidents:
        by_fingerprint.setdefault(incident.fingerprint, []).append(incident)
    conflicts: list[dict[str, Any]] = []
    conflict_count = 0
    for fingerprint, members in sorted(by_fingerprint.items()):
        categories = sorted({m.category for m in members})
        if len(categories) > 1:
            report_ids = sorted(m.report_id for m in members)
            conflict_count += len(members)
            conflicts.append(
                {
                    "fingerprint": fingerprint,
                    "categories": categories,
                    "count": len(members),
                    "report_ids": report_ids,
                }
            )
    evidence = {"total": n, "conflict": conflict_count, "groups": conflicts}
    return conflict_count / n, evidence


_METRIC_FUNCTIONS = {
    TIMELINESS: timeliness,
    DUPLICATE_RATE: duplicate_rate,
    CONFLICT_RATE: conflict_rate,
}


def compute_metric(
    code: str, incidents: Iterable[Incident], spec: MetricSpec
) -> "MetricComputation":
    """执行确定性的业务处理。"""
    func = _METRIC_FUNCTIONS[code]
    value, evidence = func(list(incidents), spec)
    sample_size = int(evidence.get("total", 0))
    min_sample = int(spec.params.get("min_sample", DEFAULT_MIN_SAMPLE))
    if value is None:
        return MetricComputation(
            code=code, value=None, score=None, sample_size=0, confident=False,
            evidence=evidence,
        )
    score = spec.scale_score(value)
    return MetricComputation(
        code=code,
        value=round(value, 6),
        score=score,
        sample_size=sample_size,
        confident=sample_size >= min_sample,
        evidence=evidence,
    )


@dataclass(frozen=True)
class MetricComputation:
    code: str
    value: float | None
    score: float | None
    sample_size: int
    confident: bool
    evidence: dict[str, Any]
