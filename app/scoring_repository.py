"""学院数据质量评分的持久化访问。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import (
    IncidentReport,
    IssuedScorecard,
    MetricDefinition,
    RuleVersion,
    ScoreAppeal,
    ScoreResult,
    ScoringBatch,
)
from .scoring.engine import ObservationWindow
from .scoring.metrics import Incident, MetricSpec


# --- 规则版本与指标定义 ---------------------------------------------------

def upsert_rule_version(
    db: Session,
    *,
    rule_version: str,
    weights: dict[str, float],
    created_by: str,
) -> RuleVersion:
    """草稿态规则版本可反复写入；发布后调用方不应再修改。"""
    existing = db.get(RuleVersion, rule_version)
    if existing is not None and existing.status == "published":
        return existing
    if existing is None:
        row = RuleVersion(
            rule_version=rule_version,
            status="draft",
            weights=weights,
            created_by=created_by,
        )
        db.add(row)
    else:
        existing.weights = weights
    db.commit()
    got = db.get(RuleVersion, rule_version)
    assert got is not None
    return got


def publish_rule_version(
    db: Session, *, rule_version: str, now: datetime | None = None
) -> RuleVersion | None:
    row = db.get(RuleVersion, rule_version)
    if row is None or row.status != "draft":
        return None
    row.status = "published"
    row.published_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    db.commit()
    return db.get(RuleVersion, rule_version)


def get_rule_version(db: Session, rule_version: str) -> RuleVersion | None:
    return db.get(RuleVersion, rule_version)


def list_rule_versions(db: Session) -> list[RuleVersion]:
    return list(
        db.execute(
            select(RuleVersion).order_by(RuleVersion.rule_version)
        ).scalars()
    )


def upsert_metric_definition(
    db: Session, *, spec: MetricSpec
) -> None:
    stmt = sqlite_insert(MetricDefinition).values(
        metric_code=spec.code,
        rule_version=spec.rule_version,
        display_name=spec.display_name,
        direction=spec.direction,
        params={
            **dict(spec.params),
            "weight": spec.weight,
        },
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["metric_code", "rule_version"],
        set_={
            "display_name": spec.display_name,
            "direction": spec.direction,
            "params": {**dict(spec.params), "weight": spec.weight},
        },
    )
    db.execute(stmt)


def get_metric_specs(
    db: Session, rule_version: str
) -> dict[str, MetricSpec] | None:
    rows = db.execute(
        select(MetricDefinition).where(
            MetricDefinition.rule_version == rule_version
        )
    ).scalars().all()
    if not rows:
        return None
    specs: dict[str, MetricSpec] = {}
    for row in rows:
        params = dict(row.params)
        weight = float(params.pop("weight", 0.0))
        specs[row.metric_code] = MetricSpec(
            code=row.metric_code,
            rule_version=row.rule_version,
            display_name=row.display_name,
            direction=row.direction,
            weight=weight,
            params=params,
        )
    return specs


# --- 上报事件输入 ---------------------------------------------------------

def insert_incidents(
    db: Session, incidents: Iterable[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """幂等导入；按 (report_id, college_id) 去重。"""
    accepted: list[str] = []
    duplicates: list[str] = []
    for item in incidents:
        stmt = sqlite_insert(IncidentReport).values(
            report_id=item["report_id"],
            college_id=item["college_id"],
            category=item["category"],
            occurred_at=item["occurred_at"],
            reported_at=item["reported_at"],
            fingerprint=item["fingerprint"],
            payload=item.get("payload", {}),
        ).on_conflict_do_nothing(
            index_elements=["report_id", "college_id"]
        ).returning(IncidentReport.id)
        inserted = db.execute(stmt).scalar_one_or_none()
        if inserted is not None:
            accepted.append(item["report_id"])
        else:
            duplicates.append(item["report_id"])
    db.commit()
    return accepted, duplicates


def _to_incident(row: IncidentReport) -> Incident:
    # SQLite 不保留 tzinfo；写入时已统一转为 UTC，读回按 UTC 解释。
    occurred = row.occurred_at
    reported = row.reported_at
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)
    if reported.tzinfo is None:
        reported = reported.replace(tzinfo=timezone.utc)
    return Incident(
        report_id=row.report_id,
        college_id=row.college_id,
        category=row.category,
        occurred_at=occurred,
        reported_at=reported,
        fingerprint=row.fingerprint,
    )


def _naive_utc(value: datetime) -> datetime:
    """SQLite 以 naive UTC 存储 DATETIME；查询参数统一去掉 tzinfo。"""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def load_incidents(
    db: Session,
    *,
    window_start: datetime,
    window_end: datetime,
    college_id: str | None = None,
) -> list[Incident]:
    stmt = (
        select(IncidentReport)
        .where(IncidentReport.occurred_at >= _naive_utc(window_start))
        .where(IncidentReport.occurred_at < _naive_utc(window_end))
        .order_by(IncidentReport.report_id)
    )
    if college_id is not None:
        stmt = stmt.where(IncidentReport.college_id == college_id)
    rows = db.execute(stmt).scalars().all()
    return [_to_incident(r) for r in rows]


def max_report_cursor(db: Session) -> str | None:
    return db.execute(
        select(IncidentReport.report_id)
        .order_by(IncidentReport.report_id.desc())
        .limit(1)
    ).scalar_one_or_none()


# --- 计算批次与结果 -------------------------------------------------------

def insert_batch(
    db: Session,
    *,
    batch_id: str,
    window: ObservationWindow,
    rule_version: str,
    input_cursor: str | None,
    revision_of: str | None,
    created_by: str,
) -> ScoringBatch | None:
    """插入 running 批次；已存在（含并发）时返回 None。"""
    stmt = sqlite_insert(ScoringBatch).values(
        batch_id=batch_id,
        window_start=window.start,
        window_end=window.end,
        timezone=window.timezone,
        rule_version=rule_version,
        input_cursor=input_cursor,
        status="running",
        revision_of=revision_of,
        manifest={},
        created_by=created_by,
    ).on_conflict_do_nothing(index_elements=["batch_id"]).returning(
        ScoringBatch.batch_id
    )
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is None:
        return None
    return db.get(ScoringBatch, batch_id)


def get_batch(db: Session, batch_id: str) -> ScoringBatch | None:
    return db.get(ScoringBatch, batch_id)


def list_batches(db: Session, *, revision_of: str | None = None) -> list[ScoringBatch]:
    stmt = select(ScoringBatch).order_by(ScoringBatch.started_at)
    if revision_of is not None:
        stmt = stmt.where(ScoringBatch.revision_of == revision_of)
    return list(db.execute(stmt).scalars())


def replace_score_results(
    db: Session, *, batch_id: str, manifest: dict[str, Any]
) -> None:
    """在同一事务内清空并写入批次结果，然后置批次为 completed。

    重启恢复时重算会再次调用，幂等覆盖未完成批次。
    """
    db.query(ScoreResult).filter(ScoreResult.batch_id == batch_id).delete()
    for college in manifest["colleges"]:
        for metric in college["metrics"]:
            db.add(
                ScoreResult(
                    batch_id=batch_id,
                    college_id=college["college_id"],
                    metric_code=metric["metric_code"],
                    value=metric["value"] if metric["value"] is not None else -1.0,
                    score=metric["score"] if metric["score"] is not None else -1.0,
                    sample_size=metric["sample_size"],
                    confident=bool(metric["confident"]),
                    evidence=metric["evidence"],
                )
            )
    batch = db.get(ScoringBatch, batch_id)
    assert batch is not None
    batch.manifest = manifest
    batch.status = "completed"
    batch.finished_at = datetime.now(timezone.utc)
    db.commit()


def load_score_results(db: Session, batch_id: str) -> list[ScoreResult]:
    return list(
        db.execute(
            select(ScoreResult)
            .where(ScoreResult.batch_id == batch_id)
            .order_by(ScoreResult.college_id, ScoreResult.metric_code)
        ).scalars()
    )


# --- 签发 -----------------------------------------------------------------

def insert_scorecard(
    db: Session,
    *,
    batch_id: str,
    rule_version: str,
    window: ObservationWindow,
    input_cursor: str | None,
    rankings: list[dict[str, Any]],
    issued_by: str,
    fingerprint: str,
) -> IssuedScorecard | None:
    """幂等签发；并发时仅一个写入成功，其余返回 None。"""
    stmt = sqlite_insert(IssuedScorecard).values(
        batch_id=batch_id,
        rule_version=rule_version,
        window_start=window.start,
        window_end=window.end,
        timezone=window.timezone,
        input_cursor=input_cursor,
        rankings=rankings,
        issued_by=issued_by,
        fingerprint=fingerprint,
    ).on_conflict_do_nothing(index_elements=["batch_id"]).returning(
        IssuedScorecard.batch_id
    )
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is None:
        return None
    return db.get(IssuedScorecard, batch_id)


def get_scorecard(db: Session, batch_id: str) -> IssuedScorecard | None:
    return db.get(IssuedScorecard, batch_id)


def load_scorecards_for_college(
    db: Session, *, college_id: str
) -> list[IssuedScorecard]:
    rows = list(
        db.execute(
            select(IssuedScorecard).order_by(IssuedScorecard.window_start)
        ).scalars()
    )
    matching: list[IssuedScorecard] = []
    for row in rows:
        if any(
            entry.get("college_id") == college_id and entry.get("rank") is not None
            for entry in row.rankings
        ):
            matching.append(row)
    return matching


# --- 申诉 -----------------------------------------------------------------

def insert_appeal(
    db: Session,
    *,
    appeal_id: str,
    batch_id: str,
    college_id: str,
    metric_code: str,
    reason: str,
    evidence: dict[str, Any],
    created_by: str,
) -> ScoreAppeal | None:
    stmt = sqlite_insert(ScoreAppeal).values(
        appeal_id=appeal_id,
        batch_id=batch_id,
        college_id=college_id,
        metric_code=metric_code,
        reason=reason,
        evidence=evidence,
        status="open",
        created_by=created_by,
    ).on_conflict_do_nothing(index_elements=["appeal_id"]).returning(
        ScoreAppeal.appeal_id
    )
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is None:
        return None
    return db.get(ScoreAppeal, appeal_id)


def get_appeal(db: Session, appeal_id: str) -> ScoreAppeal | None:
    return db.get(ScoreAppeal, appeal_id)


def list_appeals(db: Session, *, batch_id: str | None = None) -> list[ScoreAppeal]:
    stmt = select(ScoreAppeal).order_by(ScoreAppeal.created_at)
    if batch_id is not None:
        stmt = stmt.where(ScoreAppeal.batch_id == batch_id)
    return list(db.execute(stmt).scalars())


def resolve_appeal(
    db: Session,
    *,
    appeal_id: str,
    status: str,
    resolution_note: str,
    adjustment: float | None,
    reviewed_by: str,
) -> ScoreAppeal | None:
    row = db.get(ScoreAppeal, appeal_id)
    if row is None or row.status != "open":
        return None
    row.status = status
    row.resolution_note = resolution_note
    row.adjustment = adjustment
    row.reviewed_by = reviewed_by
    row.resolved_at = datetime.now(timezone.utc)
    db.commit()
    return db.get(ScoreAppeal, appeal_id)
