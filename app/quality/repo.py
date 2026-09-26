"""学院数据质量评分的持久化访问。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..models import (
    Event as EventModel,
    QualityRule,
    ScoreAppeal,
    ScoreBatch,
    ScoreItem,
)
from .metrics import RuleDefinition, ScoredEvent


# ---------------------------------------------------------------------------
# 事件读取
# ---------------------------------------------------------------------------


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        # SQLite 以 UTC 存储无时区时间戳。
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def row_to_scored_event(row: EventModel) -> ScoredEvent:
    payload = row.payload or {}
    check_in = _parse_dt(payload.get("check_in_at"))
    check_out = _parse_dt(payload.get("check_out_at"))
    created = _parse_dt(
        row.created_at.isoformat() if isinstance(row.created_at, datetime) else row.created_at
    )
    occurred = _parse_dt(payload.get("occurred_at")) or check_in or created
    reported = _parse_dt(payload.get("reported_at")) or created
    return ScoredEvent(
        event_id=row.event_id,
        college_id=row.college_id or "",
        student_id=row.student_id,
        event_type=row.event_type,
        occurred_at=occurred,
        reported_at=reported,
        activity_id=str(payload.get("activity_id", "")),
        check_in_at=check_in,
        check_out_at=check_out,
    )


def load_scored_events(
    db: Session,
    *,
    plan_version: str,
    max_event_pk: int | None = None,
    college_ids: Iterable[str] | None = None,
) -> list[ScoredEvent]:
    """读取计划下的评分事件；``max_event_pk`` 固定输入游标。"""
    stmt = select(EventModel).where(EventModel.plan_version == plan_version)
    if max_event_pk is not None:
        stmt = stmt.where(EventModel.id <= max_event_pk)
    rows = db.execute(stmt.order_by(EventModel.id)).scalars().all()
    events = [row_to_scored_event(r) for r in rows if r.college_id is not None]
    if college_ids is not None:
        wanted = set(college_ids)
        events = [e for e in events if e.college_id in wanted]
    return events


def event_pk_map(db: Session, plan_version: str) -> dict[str, int]:
    rows = db.execute(
        select(EventModel.event_id, EventModel.id).where(
            EventModel.plan_version == plan_version
        )
    ).all()
    return {event_id: pk for event_id, pk in rows}


# ---------------------------------------------------------------------------
# 规则版本
# ---------------------------------------------------------------------------


def insert_rule(db: Session, rule: RuleDefinition) -> QualityRule | None:
    stmt = sqlite_insert(QualityRule).values(
        rule_version=rule.rule_version,
        state="draft",
        definition=rule.to_dict(),
    ).on_conflict_do_nothing(index_elements=["rule_version"])
    inserted = db.execute(stmt).rowcount
    db.commit()
    if not inserted:
        return None
    return db.get(QualityRule, rule.rule_version)


def get_rule_row(db: Session, rule_version: str) -> QualityRule | None:
    return db.get(QualityRule, rule_version)


def publish_rule(
    db: Session, rule_version: str, published_at: datetime
) -> QualityRule | None:
    row = db.get(QualityRule, rule_version)
    if row is None or row.state != "draft":
        return None
    row.state = "published"
    row.published_at = published_at
    db.commit()
    return row


def list_rule_versions(db: Session) -> list[QualityRule]:
    return list(
        db.execute(select(QualityRule).order_by(QualityRule.rule_version)).scalars()
    )


def load_rule(db: Session, rule_version: str) -> RuleDefinition | None:
    row = db.get(QualityRule, rule_version)
    if row is None:
        return None
    return RuleDefinition.from_dict(row.definition)


# ---------------------------------------------------------------------------
# 计算批次
# ---------------------------------------------------------------------------


def insert_batch(db: Session, **values: Any) -> ScoreBatch | None:
    stmt = sqlite_insert(ScoreBatch).values(**values).on_conflict_do_nothing(
        index_elements=["plan_version", "batch_id"]
    )
    inserted = db.execute(stmt).rowcount
    db.commit()
    if not inserted:
        return None
    return db.get(ScoreBatch, (values["plan_version"], values["batch_id"]))


def get_batch(db: Session, plan_version: str, batch_id: str) -> ScoreBatch | None:
    return db.get(ScoreBatch, (plan_version, batch_id))


def list_batches(db: Session, plan_version: str) -> list[ScoreBatch]:
    stmt = (
        select(ScoreBatch)
        .where(ScoreBatch.plan_version == plan_version)
        .order_by(ScoreBatch.window_start, ScoreBatch.batch_id)
    )
    return list(db.execute(stmt).scalars())


def upsert_checkpoint(db: Session, item: dict[str, Any]) -> bool:
    """写入学院分项检查点；已存在（恢复时）则不覆盖。返回是否新写入。"""
    stmt = sqlite_insert(ScoreItem).values(**item).on_conflict_do_nothing(
        index_elements=["plan_version", "batch_id", "college_id"]
    )
    inserted = db.execute(stmt).rowcount
    db.commit()
    return bool(inserted)


def list_items(db: Session, plan_version: str, batch_id: str) -> list[ScoreItem]:
    stmt = (
        select(ScoreItem)
        .where(ScoreItem.plan_version == plan_version)
        .where(ScoreItem.batch_id == batch_id)
        .order_by(ScoreItem.college_id)
    )
    return list(db.execute(stmt).scalars())


def complete_batch(db: Session, batch: ScoreBatch, manifest: dict[str, Any]) -> None:
    batch.state = "computed"
    batch.manifest = manifest
    db.commit()


def mark_batch_superseded(
    db: Session, batch: ScoreBatch, superseding_batch_id: str
) -> None:
    # 已签发批次被修订时状态同样翻转为 superseded（签名与证据原样保留）。
    batch.state = "superseded"
    batch.superseded_by = superseding_batch_id
    db.commit()


def sign_batch(
    db: Session, batch: ScoreBatch, signed_at: datetime, signature: str
) -> bool:
    """签发。仅 computed 批次可翻转为 signed，保证并发下只有一个事务成功。

    已签发（重复签发）走服务层幂等分支；pending/superseded 不匹配条件。
    """
    result = db.execute(
        ScoreBatch.__table__.update()
        .where(ScoreBatch.plan_version == batch.plan_version)
        .where(ScoreBatch.batch_id == batch.batch_id)
        .where(ScoreBatch.state == "computed")
        .values(state="signed", signed_at=signed_at, signature=signature)
    )
    db.commit()
    return (result.rowcount or 0) > 0


# ---------------------------------------------------------------------------
# 申诉
# ---------------------------------------------------------------------------


def insert_appeal(db: Session, **values: Any) -> ScoreAppeal | None:
    stmt = sqlite_insert(ScoreAppeal).values(**values).on_conflict_do_nothing(
        index_elements=["plan_version", "appeal_id"]
    )
    inserted = db.execute(stmt).rowcount
    db.commit()
    if not inserted:
        return None
    return db.get(ScoreAppeal, (values["plan_version"], values["appeal_id"]))


def get_appeal(db: Session, plan_version: str, appeal_id: str) -> ScoreAppeal | None:
    return db.get(ScoreAppeal, (plan_version, appeal_id))


def list_appeals_for_batch(
    db: Session, plan_version: str, batch_id: str
) -> list[ScoreAppeal]:
    """批次的申诉：直接提起的，或经核实后并入该修订版的。"""
    from sqlalchemy import or_

    stmt = (
        select(ScoreAppeal)
        .where(ScoreAppeal.plan_version == plan_version)
        .where(
            or_(
                ScoreAppeal.batch_id == batch_id,
                ScoreAppeal.revision_batch_id == batch_id,
            )
        )
        .order_by(ScoreAppeal.appeal_id)
    )
    return list(db.execute(stmt).scalars())


def resolve_appeal(db: Session, appeal: ScoreAppeal, **changes: Any) -> None:
    for key, value in changes.items():
        setattr(appeal, key, value)
    db.commit()


def verified_exclusions_for_batch(
    db: Session, plan_version: str, batch_id: str
) -> set[str]:
    """批次的全部已核实申诉所排除的来源事件集合。"""
    rows = list_appeals_for_batch(db, plan_version, batch_id)
    excluded: set[str] = set()
    for row in rows:
        if row.state == "verified":
            excluded.update(row.verified_event_ids)
    return excluded


__all__ = [
    "event_pk_map",
    "get_appeal",
    "get_batch",
    "get_rule_row",
    "insert_appeal",
    "insert_batch",
    "insert_rule",
    "list_appeals_for_batch",
    "list_batches",
    "list_items",
    "list_rule_versions",
    "load_rule",
    "load_scored_events",
    "mark_batch_superseded",
    "publish_rule",
    "resolve_appeal",
    "row_to_scored_event",
    "sign_batch",
    "complete_batch",
    "upsert_checkpoint",
    "verified_exclusions_for_batch",
]
