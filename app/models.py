"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Plan(Base):
    __tablename__ = "plans"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    iana_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    required_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("required_seconds >= 0", name="ck_plans_required_nonneg"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    college_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("event_id", "plan_version", name="uq_events_event_id_plan"),
        Index("ix_events_plan_student", "plan_version", "student_id"),
        Index("ix_events_plan_college", "plan_version", "college_id"),
    )


class Freeze(Base):
    __tablename__ = "freezes"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    freeze_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    event_cutoff_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# 学院数据质量评分：规则版本、计算批次、分项检查点与申诉
# ---------------------------------------------------------------------------


class QualityRule(Base):
    """评分口径版本；一经发布不可修改，计算批次永久引用其快照。"""

    __tablename__ = "quality_rules"

    rule_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    definition: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "state in ('draft', 'published', 'retired')",
            name="ck_quality_rules_state",
        ),
    )


class ScoreBatch(Base):
    """一次评分计算：固定观察窗口、输入游标与规则版本。"""

    __tablename__ = "score_batches"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    cursor_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cursor_event_pk: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="computed")
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    revision_of: Mapped[str | None] = mapped_column(String(128), nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    signed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    signature: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("window_end > window_start", name="ck_score_batches_window"),
        CheckConstraint(
            "state in ('pending', 'computed', 'signed', 'superseded')",
            name="ck_score_batches_state",
        ),
    )


class ScoreItem(Base):
    """批次内单个学院的分项评分与证据，同时充当崩溃恢复检查点。"""

    __tablename__ = "score_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(128), nullable=False)
    college_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False)
    confidence_flags: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "plan_version", "batch_id", "college_id",
            name="uq_score_items_batch_college",
        ),
    )


class ScoreAppeal(Base):
    """学院申诉；只有核实的来源事件被排除后才形成修订版。"""

    __tablename__ = "score_appeals"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    appeal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(128), nullable=False)
    college_id: Mapped[str] = mapped_column(String(128), nullable=False)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    verified_event_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    rejected_event_ids: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    resolution_note: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    revision_batch_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    reviewer_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "metric in ('timeliness', 'duplicate', 'conflict')",
            name="ck_score_appeals_metric",
        ),
        CheckConstraint(
            "state in ('open', 'verified', 'rejected')",
            name="ck_score_appeals_state",
        ),
    )
