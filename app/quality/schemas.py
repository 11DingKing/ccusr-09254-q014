"""学院数据质量评分 API 的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

MetricName = Literal["timeliness", "duplicate", "conflict"]


class RuleIn(BaseModel):
    rule_version: str = Field(..., min_length=1, max_length=64)
    timezone: str = Field(..., min_length=1, max_length=64)
    deadline_hours: int = Field(24, ge=0)
    grace_seconds: int = Field(0, ge=0)
    duplicate_window_seconds: int = Field(120, ge=0)
    overlap_tolerance_seconds: int = Field(0, ge=0)
    min_sample_size: int = Field(5, ge=0)
    weights: dict[str, float] | None = None


class RuleOut(BaseModel):
    rule_version: str
    state: str
    timezone: str
    definition: dict[str, Any]
    created_at: str | None
    published_at: str | None


class BatchIn(BaseModel):
    window_start: datetime
    window_end: datetime
    rule_version: str = Field(..., min_length=1, max_length=64)
    cursor_event_id: str | None = Field(None, max_length=128)
    colleges: list[str] | None = None

    @field_validator("window_start", "window_end")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("窗口边界必须带时区")
        return value


class MetricScore(BaseModel):
    metric: str
    score: float
    numerator: int
    denominator: int
    flagged_event_ids: list[str]
    detail: list[dict[str, Any]]


class CollegeScoreOut(BaseModel):
    college_id: str
    sample_size: int
    metrics: dict[str, MetricScore]
    composite_score: float
    confidence_flags: dict[str, str]
    evidence: dict[str, Any]


class RankRow(BaseModel):
    rank: int
    college_id: str
    score: float


class BatchOut(BaseModel):
    outcome: str
    plan_version: str
    batch_id: str
    state: str
    timezone: str
    window_start: str
    window_end: str
    rule_version: str
    rule_snapshot: dict[str, Any]
    cursor_event_id: str | None
    cursor_event_pk: int | None
    revision_of: str | None
    superseded_by: str | None
    signed_at: str | None
    signature: str | None
    manifest: dict[str, Any]
    items: list[CollegeScoreOut]
    ranking: list[RankRow]


class SignOut(BaseModel):
    outcome: str
    plan_version: str
    batch_id: str
    state: str
    signed_at: str | None
    signature: str | None


class AppealIn(BaseModel):
    appeal_id: str = Field(..., min_length=1, max_length=128)
    batch_id: str = Field(..., min_length=1, max_length=128)
    college_id: str = Field(..., min_length=1, max_length=128)
    metric: MetricName
    reason: str = Field(..., min_length=1, max_length=1024)
    event_ids: list[str] = Field(..., min_length=1)


class AppealResolveIn(BaseModel):
    decision: Literal["verified", "rejected"]
    reviewer_id: str = Field(..., min_length=1, max_length=128)
    note: str = Field(..., min_length=1, max_length=1024)
    # verified：经核实排除的来源事件（必须真实且属于窗口内该学院）；
    # rejected：登记为查无实据的引用事件。
    verified_event_ids: list[str] | None = None


class AppealOut(BaseModel):
    appeal_id: str
    plan_version: str
    batch_id: str
    college_id: str
    metric: str
    reason: str
    state: str
    verified_event_ids: list[str]
    rejected_event_ids: list[str]
    resolution_note: str
    revision_batch_id: str | None
    reviewer_id: str
    created_at: str
    resolved_at: str | None


class RevisionIn(BaseModel):
    base_batch_id: str = Field(..., min_length=1, max_length=128)
    appeal_ids: list[str] = Field(default_factory=list)
    # 迟到数据：显式推进输入游标（缺省推进到当前最新事件）。
    cursor_event_id: str | None = Field(None, max_length=128)
    colleges: list[str] | None = None


class TrendPoint(BaseModel):
    college_id: str
    sample_size: int
    composite_score: float
    metrics: dict[str, Any]
    confidence_flags: dict[str, str]


class TrendBatch(BaseModel):
    batch_id: str
    state: str
    rule_version: str
    window_start: str
    window_end: str
    revision_of: str | None
    signed_at: str | None
    points: list[TrendPoint]


class TrendOut(BaseModel):
    plan_version: str
    college_id: str | None
    rule_version: str | None
    batches: list[TrendBatch]
