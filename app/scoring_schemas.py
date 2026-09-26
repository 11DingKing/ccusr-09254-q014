"""学院数据质量评分的请求 / 响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

MetricCode = Literal["timeliness", "duplicate_rate", "conflict_rate"]


class MetricSpecIn(BaseModel):
    deadline_hours: int | None = Field(default=None, ge=1)
    min_sample: int | None = Field(default=None, ge=0)


class RuleVersionIn(BaseModel):
    weights: dict[str, float] | None = None
    metric_params: dict[str, dict[str, Any]] | None = None
    created_by: str = Field(..., min_length=1, max_length=128)


class RuleVersionOut(BaseModel):
    rule_version: str
    status: str
    weights: dict[str, float] | None = None
    published_at: str | None = None
    metrics: list[dict[str, Any]] | None = None


class IncidentIn(BaseModel):
    report_id: str = Field(..., min_length=1, max_length=128)
    college_id: str = Field(..., min_length=1, max_length=128)
    category: str = Field(..., min_length=1, max_length=64)
    occurred_at: datetime
    reported_at: datetime
    fingerprint: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at", "reported_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("时间必须包含时区 (RFC 3339)")
        return v


class IncidentBatchIn(BaseModel):
    incidents: list[IncidentIn]


class IncidentImportResult(BaseModel):
    accepted: int
    duplicates: list[str]
    rejected: list[dict[str, Any]]


class BatchIn(BaseModel):
    window_start: str = Field(..., description="观察窗口起始本地日期 ISO，含当日")
    window_end: str = Field(..., description="观察窗口结束本地日期 ISO（半开，不含当日）")
    timezone: str = Field(..., min_length=1, max_length=64)
    rule_version: str = Field("rv-2026-09", min_length=1, max_length=64)
    input_cursor: str | None = Field(
        default=None, description="固定输入游标（最大 report_id）；缺省取当前最大值"
    )
    revision_of: str | None = None
    created_by: str = Field(..., min_length=1, max_length=128)


class BatchOut(BaseModel):
    batch_id: str
    status: str
    rule_version: str
    revision_of: str | None
    window: dict[str, str]
    input_cursor: str | None
    colleges: list[dict[str, Any]]


class BatchSummaryOut(BaseModel):
    batch_id: str
    status: str
    rule_version: str
    window_start: str
    window_end: str
    timezone: str
    input_cursor: str | None
    revision_of: str | None


class IssueIn(BaseModel):
    issued_by: str = Field(..., min_length=1, max_length=128)


class RankEntry(BaseModel):
    rank: int | None
    college_id: str
    composite_score: float | None
    confident: bool


class ScorecardOut(BaseModel):
    batch_id: str
    rule_version: str
    window: dict[str, str]
    input_cursor: str | None
    rankings: list[RankEntry]
    issued_by: str
    issued_at: str
    fingerprint: str


class AppealIn(BaseModel):
    appeal_id: str = Field(..., min_length=1, max_length=128)
    batch_id: str = Field(..., min_length=1, max_length=128)
    college_id: str = Field(..., min_length=1, max_length=128)
    metric_code: MetricCode
    reason: str = Field(..., min_length=1)
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_by: str = Field(..., min_length=1, max_length=128)


class AppealResolveIn(BaseModel):
    verified: bool
    reviewed_by: str = Field(..., min_length=1, max_length=128)
    resolution_note: str = Field(..., min_length=1)
    excluded_report_ids: list[str] = Field(default_factory=list)
    recategorized: dict[str, str] = Field(default_factory=dict)
    revision_batch_id: str | None = None


class AppealOut(BaseModel):
    appeal_id: str
    batch_id: str
    college_id: str
    metric_code: str
    reason: str
    evidence: dict[str, Any]
    status: str
    resolution_note: str | None
    adjustment: float | None
    created_by: str
    reviewed_by: str | None
    resolved_at: str | None


class TrendPoint(BaseModel):
    batch_id: str
    revision_of: str | None
    window_start: str
    window_end: str
    rank: int | None
    composite_score: float | None
    confident: bool
    metrics: dict[str, dict[str, Any]]


class TrendOut(BaseModel):
    college_id: str
    metric_code: str | None
    points: list[TrendPoint]
