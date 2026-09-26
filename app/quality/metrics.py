"""学院数据质量指标的纯函数计算。

评分口径由规则版本（:class:`RuleDefinition`）固定：观察窗口按学院本地
IANA 时区解释，迟到宽限、重复匹配、冲突判定的全部阈值都取自规则快照，
因此同一输入在任何时候重放都得到相同结果。

三个分项指标（取值 0..1，越高越好）：

* ``timeliness`` —— 及时率：应在截止时限前上报的事件中，按时（含宽限）
  到达的比例；
* ``duplicate``   —— 无重复率：1 减去按规则指纹聚类得到的重复比例；
* ``conflict``    —— 无冲突率：1 减去与其他事件语义矛盾的事件比例。

每个分项同时输出证据（参与统计的事件、迟到/重复/冲突明细）和置信标记
（``reliable`` / ``small_sample`` / ``empty_sample``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from ..core.clock import get_zone, to_utc

METRICS = ("timeliness", "duplicate", "conflict")

# ---------------------------------------------------------------------------
# 规则定义（口径版本）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleDefinition:
    """一次发布的评分口径快照。"""

    rule_version: str
    timezone: str
    # 事件发生 -> 应上报截止的小时数；按事件发生时刻所在的本地自然日。
    deadline_hours: int = 24
    # 迟到宽限（秒）：发生时刻 + deadline 后仍计入及时的宽限。
    grace_seconds: int = 0
    # 同一学院内，多少秒内、同一活动、同一学员的两次签到视为重复。
    duplicate_window_seconds: int = 120
    # 时间重叠容忍秒数；两次签到重叠超过该值即构成冲突。
    overlap_tolerance_seconds: int = 0
    # 低于该样本量时给出 small_sample 置信标记（仍照常出分）。
    min_sample_size: int = 5
    # 各分项权重，缺省等权。
    weights: Mapping[str, float] = field(
        default_factory=lambda: {"timeliness": 1 / 3, "duplicate": 1 / 3, "conflict": 1 / 3}
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_version": self.rule_version,
            "timezone": self.timezone,
            "deadline_hours": self.deadline_hours,
            "grace_seconds": self.grace_seconds,
            "duplicate_window_seconds": self.duplicate_window_seconds,
            "overlap_tolerance_seconds": self.overlap_tolerance_seconds,
            "min_sample_size": self.min_sample_size,
            "weights": dict(self.weights),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RuleDefinition":
        weights = dict(
            data.get("weights")
            or {"timeliness": 1 / 3, "duplicate": 1 / 3, "conflict": 1 / 3}
        )
        return cls(
            rule_version=data["rule_version"],
            timezone=data["timezone"],
            deadline_hours=int(data.get("deadline_hours", 24)),
            grace_seconds=int(data.get("grace_seconds", 0)),
            duplicate_window_seconds=int(data.get("duplicate_window_seconds", 120)),
            overlap_tolerance_seconds=int(data.get("overlap_tolerance_seconds", 0)),
            min_sample_size=int(data.get("min_sample_size", 5)),
            weights=weights,
        )

    def validate(self) -> None:
        get_zone(self.timezone)
        if self.deadline_hours < 0:
            raise ValueError("deadline_hours 不能为负")
        if self.grace_seconds < 0:
            raise ValueError("grace_seconds 不能为负")
        if self.duplicate_window_seconds < 0:
            raise ValueError("duplicate_window_seconds 不能为负")
        if self.overlap_tolerance_seconds < 0:
            raise ValueError("overlap_tolerance_seconds 不能为负")
        if self.min_sample_size < 0:
            raise ValueError("min_sample_size 不能为负")
        for name in METRICS:
            if name not in self.weights:
                raise ValueError(f"缺少分项权重: {name}")
            if self.weights[name] < 0:
                raise ValueError(f"分项权重不能为负: {name}")
        if sum(self.weights.values()) <= 0:
            raise ValueError("权重之和必须为正")


# ---------------------------------------------------------------------------
# 输入事件
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoredEvent:
    """参与评分的一条上报事件。"""

    event_id: str
    college_id: str
    student_id: str
    event_type: str
    occurred_at: datetime
    reported_at: datetime
    activity_id: str = ""
    check_in_at: datetime | None = None
    check_out_at: datetime | None = None


@dataclass(frozen=True)
class Window:
    """按学院本地时区解释的观察窗口 [start, end)。"""

    start_utc: datetime
    end_utc: datetime
    timezone: str

    def contains(self, moment_utc: datetime) -> bool:
        instant = to_utc(moment_utc)
        return self.start_utc <= instant < self.end_utc


def local_window(
    start_local: datetime,
    end_local: datetime,
    timezone_name: str,
) -> Window:
    """把本地（必须带时区，或显式给出 tz）边界转换为 UTC 半开区间。"""
    zone = get_zone(timezone_name)
    if start_local.tzinfo is None:
        start_local = start_local.replace(tzinfo=zone)
    if end_local.tzinfo is None:
        end_local = end_local.replace(tzinfo=zone)
    start_utc = to_utc(start_local)
    end_utc = to_utc(end_local)
    if end_utc <= start_utc:
        raise ValueError("观察窗口结束必须晚于开始")
    return Window(start_utc=start_utc, end_utc=end_utc, timezone=timezone_name)


def in_window(event: ScoredEvent, window: Window) -> bool:
    """窗口按事件发生时刻（occurred_at）归属。"""
    return window.contains(event.occurred_at)


# ---------------------------------------------------------------------------
# 单项结论
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricResult:
    metric: str
    score: float
    numerator: int
    denominator: int
    flagged_event_ids: tuple[str, ...]
    detail: tuple[dict[str, Any], ...]

    def confidence(self, min_sample: int) -> str:
        if self.denominator == 0:
            return "empty_sample"
        if self.denominator < min_sample:
            return "small_sample"
        return "reliable"

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "score": round(self.score, 6),
            "numerator": self.numerator,
            "denominator": self.denominator,
            "flagged_event_ids": list(self.flagged_event_ids),
            "detail": list(self.detail),
        }


def _round6(value: float) -> float:
    return round(value, 6)


# ---------------------------------------------------------------------------
# 分项计算
# ---------------------------------------------------------------------------


def score_timeliness(
    events: list[ScoredEvent], rule: RuleDefinition
) -> MetricResult:
    """及时率：reported_at <= occurred_at 当日 23:59:59... + deadline + grace。

    截止时刻定义为「事件发生时刻所在本地自然日的次日午夜（UTC）」再加
    ``deadline_hours``；因此 DST 切换日的截止时刻同样按本地日历推进。
    """
    zone = get_zone(rule.timezone)
    late: list[dict[str, Any]] = []
    on_time = 0
    for ev in events:
        occurred_local = to_utc(ev.occurred_at).astimezone(zone)
        next_local_midnight = datetime(
            occurred_local.year,
            occurred_local.month,
            occurred_local.day,
            tzinfo=zone,
        ) + timedelta(days=1)
        deadline_utc = next_local_midnight.astimezone(timezone.utc) + timedelta(
            hours=rule.deadline_hours
        )
        due_utc = deadline_utc + timedelta(seconds=rule.grace_seconds)
        if to_utc(ev.reported_at) <= due_utc:
            on_time += 1
        else:
            late.append(
                {
                    "event_id": ev.event_id,
                    "occurred_at_utc": to_utc(ev.occurred_at)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "reported_at_utc": to_utc(ev.reported_at)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "deadline_utc": deadline_utc.isoformat().replace("+00:00", "Z"),
                    "lateness_seconds": int(
                        (to_utc(ev.reported_at) - deadline_utc).total_seconds()
                    ),
                }
            )
    total = len(events)
    score = (on_time / total) if total else 0.0
    return MetricResult(
        metric="timeliness",
        score=_round6(score),
        numerator=on_time,
        denominator=total,
        flagged_event_ids=tuple(item["event_id"] for item in late),
        detail=tuple(late),
    )


def score_duplicate(
    events: list[ScoredEvent], rule: RuleDefinition
) -> MetricResult:
    """无重复率。

    按 (student_id, activity_id, event_type) 分组并按发生时刻排序；与同组
    前一事件的发生间隔不超过 ``duplicate_window_seconds`` 的事件记为重复。
    每组最早的一条是原始件，不计重复。
    """
    groups: dict[tuple[str, str, str], list[ScoredEvent]] = {}
    for ev in events:
        groups.setdefault(
            (ev.student_id, ev.activity_id, ev.event_type), []
        ).append(ev)

    duplicates: list[dict[str, Any]] = []
    for _, members in sorted(groups.items()):
        ordered = sorted(members, key=lambda e: (to_utc(e.occurred_at), e.event_id))
        for index in range(1, len(ordered)):
            prev = ordered[index - 1]
            cur = ordered[index]
            gap = int(
                (to_utc(cur.occurred_at) - to_utc(prev.occurred_at)).total_seconds()
            )
            if gap <= rule.duplicate_window_seconds:
                duplicates.append(
                    {
                        "event_id": cur.event_id,
                        "duplicate_of": prev.event_id,
                        "gap_seconds": gap,
                    }
                )
    total = len(events)
    unique = total - len(duplicates)
    score = (unique / total) if total else 0.0
    return MetricResult(
        metric="duplicate",
        score=_round6(score),
        numerator=unique,
        denominator=total,
        flagged_event_ids=tuple(item["event_id"] for item in duplicates),
        detail=tuple(duplicates),
    )


def score_conflict(
    events: list[ScoredEvent], rule: RuleDefinition
) -> MetricResult:
    """无冲突率：同一学员重叠的签到区间，或同事件上的矛盾修正。

    两类冲突：
    1. 同一 ``student_id`` 的两条 checkin 时间区间重叠超过
       ``overlap_tolerance_seconds``；
    2. 同一活动（activity_id）被同一学员上报为相互重叠的不同区间时，
       归入第 1 类一并检出。
    """
    by_student: dict[str, list[ScoredEvent]] = {}
    for ev in events:
        by_student.setdefault(ev.student_id, []).append(ev)

    flagged: set[str] = set()
    detail_rows: list[dict[str, Any]] = []
    for student_id, members in sorted(by_student.items()):
        intervals = sorted(
            (
                (to_utc(e.check_in_at), to_utc(e.check_out_at), e.event_id)
                for e in members
                if e.check_in_at is not None and e.check_out_at is not None
            ),
            key=lambda triple: (triple[0], triple[2]),
        )
        for i in range(len(intervals)):
            start_i, end_i, id_i = intervals[i]
            for j in range(i + 1, len(intervals)):
                start_j, end_j, id_j = intervals[j]
                if start_j >= end_i:
                    break
                overlap = int((min(end_i, end_j) - start_j).total_seconds())
                if overlap > rule.overlap_tolerance_seconds:
                    flagged.add(id_i)
                    flagged.add(id_j)
                    detail_rows.append(
                        {
                            "event_id": id_i,
                            "conflicts_with": id_j,
                            "student_id": student_id,
                            "overlap_seconds": overlap,
                        }
                    )
    total = len(events)
    clean = total - len(flagged)
    score = (clean / total) if total else 0.0
    return MetricResult(
        metric="conflict",
        score=_round6(score),
        numerator=clean,
        denominator=total,
        flagged_event_ids=tuple(sorted(flagged)),
        detail=tuple(detail_rows),
    )


# ---------------------------------------------------------------------------
# 学院汇总
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CollegeScore:
    college_id: str
    sample_size: int
    metrics: dict[str, MetricResult]
    composite_score: float
    confidence_flags: dict[str, str]
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "college_id": self.college_id,
            "sample_size": self.sample_size,
            "metrics": {name: self.metrics[name].to_dict() for name in METRICS},
            "composite_score": _round6(self.composite_score),
            "confidence_flags": dict(self.confidence_flags),
            "evidence": self.evidence,
        }


def score_college(
    college_id: str,
    events: list[ScoredEvent],
    rule: RuleDefinition,
) -> CollegeScore:
    """对单个学院窗口内的事件计算三个分项与综合分。"""
    ordered = sorted(events, key=lambda e: (to_utc(e.occurred_at), e.event_id))
    results = {
        "timeliness": score_timeliness(ordered, rule),
        "duplicate": score_duplicate(ordered, rule),
        "conflict": score_conflict(ordered, rule),
    }
    weights = rule.weights
    weight_sum = sum(weights[name] for name in METRICS)
    composite = (
        sum(results[name].score * weights[name] for name in METRICS) / weight_sum
        if weight_sum
        else 0.0
    )
    flags = {name: results[name].confidence(rule.min_sample_size) for name in METRICS}
    sample_size = len(ordered)
    if sample_size == 0:
        overall = "empty_sample"
    elif sample_size < rule.min_sample_size:
        overall = "small_sample"
    else:
        overall = "reliable"
    flags["overall"] = overall

    evidence = {
        "college_id": college_id,
        "sample_event_ids": [e.event_id for e in ordered],
        "by_metric": {name: results[name].to_dict() for name in METRICS},
    }
    return CollegeScore(
        college_id=college_id,
        sample_size=sample_size,
        metrics=results,
        composite_score=_round6(composite),
        confidence_flags=flags,
        evidence=evidence,
    )


def score_window(
    events: Iterable[ScoredEvent],
    window: Window,
    rule: RuleDefinition,
    *,
    excluded_event_ids: Iterable[str] | None = None,
    colleges: Iterable[str] | None = None,
) -> dict[str, CollegeScore]:
    """按学院分组计算窗口内评分。

    ``excluded_event_ids`` 用于申诉核实后排除来源事件重算；空学院（出现在
    ``colleges`` 列表或窗口内无任何事件）同样输出 empty_sample 结果，保证
    排名口径稳定、可复现。
    """
    excluded = set(excluded_event_ids or ())
    by_college: dict[str, list[ScoredEvent]] = {}
    known: set[str] = set(colleges or ())
    for ev in events:
        if ev.event_id in excluded:
            continue
        if not in_window(ev, window):
            continue
        by_college.setdefault(ev.college_id, []).append(ev)
        known.add(ev.college_id)
    return {
        college_id: score_college(
            college_id, by_college.get(college_id, []), rule
        )
        for college_id in sorted(known)
    }
