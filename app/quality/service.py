"""学院数据质量评分的应用服务。

关键不变量：

* 批次一旦开始计算即固定「观察窗口 / 输入游标（事件主键 + 事件 ID）/
  规则版本快照」；规则后续修改或事件迟到都不会改变已存批次，只产生修订版。
* 修订版必须沿用被修订批次的规则版本；原批次被标记为 superseded，但行
  与排名证据原样保留。
* 申诉只有在引用的来源事件经核实存在（且属于该学院、落在窗口样本内）时
  才会把这些事件排除后重算；核实未通过的申诉被拒绝，不影响分数。
* 每个学院的分项结果在独立事务中作为检查点落库，进程崩溃后重入同一批次
  会跳过已完成学院、继续未完成学院。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Callable, Iterable

from sqlalchemy.orm import Session

from . import repo
from .metrics import (
    METRICS,
    RuleDefinition,
    ScoredEvent,
    Window,
    local_window,
    score_college,
)
from ..models import ScoreBatch
from ..repository import get_plan


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return _as_utc(dt).isoformat().replace("+00:00", "Z")


def _as_utc(dt: datetime) -> datetime:
    """SQLite 的 DateTime 列可能返回 naive UTC。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class QualityError(Exception):
    """封装质量评分领域错误。"""


class RuleNotFoundError(QualityError):
    pass


class RuleStateError(QualityError):
    pass


class BatchNotFoundError(QualityError):
    pass


class BatchConflictError(QualityError):
    pass


class AppealNotFoundError(QualityError):
    pass


class AppealStateError(QualityError):
    pass


class CursorNotFoundError(QualityError):
    pass


CheckpointHook = Callable[[str], None]


# ---------------------------------------------------------------------------
# 规则版本（口径）
# ---------------------------------------------------------------------------


def create_rule(
    db: Session,
    *,
    plan_version: str,
    rule_version: str,
    timezone: str,
    deadline_hours: int = 24,
    grace_seconds: int = 0,
    duplicate_window_seconds: int = 120,
    overlap_tolerance_seconds: int = 0,
    min_sample_size: int = 5,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    rule = RuleDefinition(
        rule_version=rule_version,
        timezone=timezone,
        deadline_hours=deadline_hours,
        grace_seconds=grace_seconds,
        duplicate_window_seconds=duplicate_window_seconds,
        overlap_tolerance_seconds=overlap_tolerance_seconds,
        min_sample_size=min_sample_size,
        weights=weights
        or {"timeliness": 1 / 3, "duplicate": 1 / 3, "conflict": 1 / 3},
    )
    rule.validate()
    row = repo.insert_rule(db, rule)
    if row is None:
        raise BatchConflictError(f"规则版本 '{rule_version}' 已存在")
    return _rule_to_dict(row)


def publish_rule(db: Session, *, plan_version: str, rule_version: str) -> dict[str, Any]:
    _require_plan(db, plan_version)
    row = repo.publish_rule(db, rule_version, _utcnow())
    if row is None:
        existing = repo.get_rule_row(db, rule_version)
        if existing is None:
            raise RuleNotFoundError(f"规则版本 '{rule_version}' 不存在")
        raise RuleStateError(
            f"规则版本 '{rule_version}' 当前状态为 {existing.state}，不能发布"
        )
    return _rule_to_dict(row)


def list_rules(db: Session, *, plan_version: str) -> list[dict[str, Any]]:
    _require_plan(db, plan_version)
    return [_rule_to_dict(r) for r in repo.list_rule_versions(db)]


def _rule_to_dict(row: Any) -> dict[str, Any]:
    return {
        "rule_version": row.rule_version,
        "state": row.state,
        "timezone": row.definition["timezone"],
        "definition": row.definition,
        "created_at": _iso(row.created_at) if row.created_at else None,
        "published_at": _iso(row.published_at) if row.published_at else None,
    }


def _require_published_rule(db: Session, rule_version: str) -> RuleDefinition:
    row = repo.get_rule_row(db, rule_version)
    if row is None:
        raise RuleNotFoundError(f"规则版本 '{rule_version}' 不存在")
    if row.state != "published":
        raise RuleStateError(
            f"规则版本 '{rule_version}' 状态为 {row.state}，只有已发布口径可用于计算"
        )
    return RuleDefinition.from_dict(row.definition)


def _require_plan(db: Session, plan_version: str) -> None:
    if get_plan(db, plan_version) is None:
        raise QualityError(f"培养方案 '{plan_version}' 未注册")


# ---------------------------------------------------------------------------
# 批次序列化
# ---------------------------------------------------------------------------


def _ranking(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        items,
        key=lambda it: (-float(it["composite_score"]), it["college_id"]),
    )
    return [
        {"rank": idx + 1, "college_id": it["college_id"], "score": it["composite_score"]}
        for idx, it in enumerate(ordered)
    ]


def serialize_batch(
    db: Session,
    batch: ScoreBatch,
    *,
    outcome: str = "existing",
) -> dict[str, Any]:
    item_rows = repo.list_items(db, batch.plan_version, batch.batch_id)
    items = []
    for row in item_rows:
        metrics = {k: v for k, v in row.metrics.items() if k in METRICS}
        items.append(
            {
                "college_id": row.college_id,
                "sample_size": row.sample_size,
                "metrics": metrics,
                "composite_score": row.metrics.get("_composite", 0.0),
                "confidence_flags": row.confidence_flags,
                "evidence": row.evidence,
            }
        )
    return {
        "outcome": outcome,
        "plan_version": batch.plan_version,
        "batch_id": batch.batch_id,
        "state": batch.state,
        "timezone": batch.timezone,
        "window_start": _iso(batch.window_start),
        "window_end": _iso(batch.window_end),
        "rule_version": batch.rule_version,
        "rule_snapshot": batch.rule_snapshot,
        "cursor_event_id": batch.cursor_event_id,
        "cursor_event_pk": batch.cursor_event_pk,
        "revision_of": batch.revision_of,
        "superseded_by": batch.superseded_by,
        "signed_at": _iso(batch.signed_at) if batch.signed_at else None,
        "signature": batch.signature,
        "manifest": batch.manifest,
        "items": items,
        # 排名是已计算结果的一部分；批次被修订后仍原样保留，不能重写。
        "ranking": _ranking(items) if batch.state != "pending" else [],
    }


# ---------------------------------------------------------------------------
# 计算批次
# ---------------------------------------------------------------------------


def _resolve_cursor(
    db: Session,
    plan_version: str,
    cursor_event_id: str | None,
) -> tuple[str | None, int | None]:
    """把输入游标固定为 (event_id, 事件自增主键)。"""
    pk_map = repo.event_pk_map(db, plan_version)
    if cursor_event_id is not None:
        if cursor_event_id not in pk_map:
            raise CursorNotFoundError(
                f"游标事件 '{cursor_event_id}' 在方案 '{plan_version}' 下不存在"
            )
        return cursor_event_id, pk_map[cursor_event_id]
    if not pk_map:
        return None, None
    max_pk = max(pk_map.values())
    return next(eid for eid, pk in pk_map.items() if pk == max_pk), max_pk


def _input_fingerprint(events: Iterable[ScoredEvent]) -> str:
    material = "\n".join(
        f"{e.event_id}|{e.college_id}|{e.student_id}|{e.event_type}" for e in events
    )
    return sha256(material.encode("utf-8")).hexdigest()


def compute_batch(
    db: Session,
    *,
    plan_version: str,
    batch_id: str,
    window_start: datetime,
    window_end: datetime,
    rule_version: str,
    cursor_event_id: str | None = None,
    colleges: list[str] | None = None,
    revision_of: str | None = None,
    excluded_event_ids: Iterable[str] | None = None,
    checkpoint_hook: CheckpointHook | None = None,
) -> dict[str, Any]:
    """计算（或崩溃后恢复）一个评分批次。幂等。"""
    _require_plan(db, plan_version)

    base: ScoreBatch | None = None
    if revision_of is not None:
        base = repo.get_batch(db, plan_version, revision_of)
        if base is None:
            raise BatchNotFoundError(f"被修订批次 '{revision_of}' 不存在")
        if base.state == "pending":
            raise BatchConflictError(
                f"被修订批次 '{revision_of}' 仍在计算中，不能据此形成修订版"
            )
        if base.rule_version != rule_version:
            raise RuleStateError(
                "修订版必须沿用原批次的规则版本 "
                f"'{base.rule_version}'（口径变化不得重写过去排名）"
            )
        # 修订版固定使用原批次的口径快照；即使该规则版本日后被停用，
        # 修订仍可基于快照继续计算。
        rule = RuleDefinition.from_dict(base.rule_snapshot)
    else:
        rule = _require_published_rule(db, rule_version)

    existing = repo.get_batch(db, plan_version, batch_id)
    if existing is not None and existing.state != "pending":
        return serialize_batch(db, existing, outcome="existing")

    window = local_window(window_start, window_end, rule.timezone)
    pinned_event_id, pinned_pk = _resolve_cursor(db, plan_version, cursor_event_id)
    excluded = sorted(set(excluded_event_ids or ()))

    if existing is None:
        batch_row = repo.insert_batch(
            db,
            batch_id=batch_id,
            plan_version=plan_version,
            window_start=window.start_utc,
            window_end=window.end_utc,
            timezone=rule.timezone,
            rule_version=rule_version,
            rule_snapshot=rule.to_dict(),
            cursor_event_id=pinned_event_id,
            cursor_event_pk=pinned_pk,
            state="pending",
            manifest={
                "college_hints": sorted(set(colleges or [])),
                "excluded_event_ids": excluded,
            },
            revision_of=revision_of,
            superseded_by=None,
        )
        if batch_row is None:
            winner = repo.get_batch(db, plan_version, batch_id)
            assert winner is not None
            return serialize_batch(db, winner, outcome="existing")
        outcome = "created"
    else:  # 崩溃恢复：pending 批次沿用已固定的窗口/游标/规则
        batch_row = existing
        window = local_window(
            _as_utc(batch_row.window_start),
            _as_utc(batch_row.window_end),
            batch_row.timezone,
        )
        rule = RuleDefinition.from_dict(batch_row.rule_snapshot)
        pinned_event_id = batch_row.cursor_event_id
        pinned_pk = batch_row.cursor_event_pk
        excluded = list(batch_row.manifest.get("excluded_event_ids", []))
        colleges = list(batch_row.manifest.get("college_hints", []))
        outcome = "recovered"

    all_events = repo.load_scored_events(
        db, plan_version=plan_version, max_event_pk=pinned_pk
    )
    college_universe = set(colleges or [])
    college_universe.update(e.college_id for e in all_events if e.college_id)
    college_universe.discard("")

    done = {item.college_id for item in repo.list_items(db, plan_version, batch_id)}
    excluded_set = set(excluded)
    for college_id in sorted(college_universe):
        if college_id in done:
            continue
        college_events = [
            e
            for e in all_events
            if e.college_id == college_id and e.event_id not in excluded_set
        ]
        window_events = [e for e in college_events if window.contains(e.occurred_at)]
        result = score_college(college_id, window_events, rule)
        payload = result.to_dict()
        metrics_with_composite = dict(payload["metrics"])
        metrics_with_composite["_composite"] = payload["composite_score"]
        repo.upsert_checkpoint(
            db,
            {
                "plan_version": plan_version,
                "batch_id": batch_id,
                "college_id": college_id,
                "sample_size": payload["sample_size"],
                "metrics": metrics_with_composite,
                "evidence": payload["evidence"],
                "confidence_flags": payload["confidence_flags"],
            },
        )
        if checkpoint_hook is not None:
            checkpoint_hook(college_id)

    manifest = {
        "rule_version": rule_version,
        "window_start_utc": _iso(window.start_utc),
        "window_end_utc": _iso(window.end_utc),
        "timezone": rule.timezone,
        "cursor_event_id": pinned_event_id,
        "cursor_event_pk": pinned_pk,
        "input_event_count": len(all_events),
        "input_fingerprint": _input_fingerprint(all_events),
        "college_count": len(college_universe),
        "excluded_event_ids": excluded,
        "revision_of": revision_of,
        "computed_at": _iso(_utcnow()),
    }
    repo.complete_batch(db, batch_row, manifest)
    if base is not None:
        repo.mark_batch_superseded(db, base, batch_id)

    batch_row = repo.get_batch(db, plan_version, batch_id)
    assert batch_row is not None
    return serialize_batch(db, batch_row, outcome=outcome)


def get_batch(
    db: Session, *, plan_version: str, batch_id: str
) -> dict[str, Any]:
    batch = repo.get_batch(db, plan_version, batch_id)
    if batch is None:
        raise BatchNotFoundError(f"批次 '{batch_id}' 不存在")
    return serialize_batch(db, batch)


def list_batches(db: Session, *, plan_version: str) -> list[dict[str, Any]]:
    return [serialize_batch(db, b) for b in repo.list_batches(db, plan_version)]


# ---------------------------------------------------------------------------
# 签发
# ---------------------------------------------------------------------------


def _canonical_signing_payload(batch: ScoreBatch, items: list[Any]) -> str:
    body = {
        "plan_version": batch.plan_version,
        "batch_id": batch.batch_id,
        "window_start": _iso(batch.window_start),
        "window_end": _iso(batch.window_end),
        "timezone": batch.timezone,
        "rule_version": batch.rule_version,
        "rule_snapshot": batch.rule_snapshot,
        "cursor_event_id": batch.cursor_event_id,
        "cursor_event_pk": batch.cursor_event_pk,
        "revision_of": batch.revision_of,
        "items": [
            {
                "college_id": it.college_id,
                "sample_size": it.sample_size,
                "metrics": it.metrics,
                "confidence_flags": it.confidence_flags,
            }
            for it in sorted(items, key=lambda r: r.college_id)
        ],
    }
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sign_batch(
    db: Session, *, plan_version: str, batch_id: str
) -> dict[str, Any]:
    batch = repo.get_batch(db, plan_version, batch_id)
    if batch is None:
        raise BatchNotFoundError(f"批次 '{batch_id}' 不存在")
    if batch.state == "pending":
        raise BatchConflictError(f"批次 '{batch_id}' 尚未完成计算，不能签发")

    items = repo.list_items(db, plan_version, batch_id)
    signature = sha256(
        _canonical_signing_payload(batch, items).encode("utf-8")
    ).hexdigest()

    if batch.state == "signed":
        # 幂等：重复签发必须得到同一签名。
        if batch.signature != signature:
            raise BatchConflictError("已签发批次的签名载荷不一致，数据可能被篡改")
        return serialize_batch(db, batch, outcome="already_signed")
    if batch.state == "superseded":
        raise BatchConflictError("批次已被修订版取代，不能再签发")

    newly_signed = repo.sign_batch(db, batch, _utcnow(), signature)
    db.expire_all()
    refreshed = repo.get_batch(db, plan_version, batch_id)
    assert refreshed is not None
    if not newly_signed:
        # 并发竞争中落败：赢家已完成签发。
        return serialize_batch(db, refreshed, outcome="already_signed")
    return serialize_batch(db, refreshed, outcome="signed")


# ---------------------------------------------------------------------------
# 申诉
# ---------------------------------------------------------------------------


def create_appeal(
    db: Session,
    *,
    plan_version: str,
    appeal_id: str,
    batch_id: str,
    college_id: str,
    metric: str,
    reason: str,
    event_ids: list[str],
) -> dict[str, Any]:
    if metric not in METRICS:
        raise QualityError(f"未知指标 '{metric}'")
    batch = repo.get_batch(db, plan_version, batch_id)
    if batch is None:
        raise BatchNotFoundError(f"批次 '{batch_id}' 不存在")
    if not reason.strip():
        raise QualityError("申诉必须说明理由")
    if not event_ids:
        raise QualityError("申诉必须引用至少一个来源事件")
    row = repo.insert_appeal(
        db,
        appeal_id=appeal_id,
        plan_version=plan_version,
        batch_id=batch_id,
        college_id=college_id,
        metric=metric,
        reason=reason,
        state="open",
        verified_event_ids=[],
        rejected_event_ids=[],
        resolution_note="",
        revision_batch_id=None,
        reviewer_id="",
    )
    if row is None:
        raise BatchConflictError(f"申诉 '{appeal_id}' 已存在")
    return _appeal_to_dict(row)


def _verify_source_events(
    db: Session,
    *,
    plan_version: str,
    batch: ScoreBatch,
    college_id: str,
    event_ids: list[str],
) -> tuple[list[str], list[str]]:
    """核实来源：事件必须存在、属于该学院、且在批次窗口与游标范围内。"""
    # 批次游标为空意味着计算时输入为空：任何事件都超出该批次的固定输入。
    if batch.cursor_event_pk is None:
        events: dict[str, ScoredEvent] = {}
    else:
        events = {
            e.event_id: e
            for e in repo.load_scored_events(
                db,
                plan_version=plan_version,
                max_event_pk=batch.cursor_event_pk,
            )
        }
    window = Window(
        start_utc=_as_utc(batch.window_start),
        end_utc=_as_utc(batch.window_end),
        timezone=batch.timezone,
    )
    verified: list[str] = []
    rejected: list[str] = []
    for event_id in dict.fromkeys(event_ids):
        event = events.get(event_id)
        if (
            event is not None
            and event.college_id == college_id
            and window.contains(event.occurred_at)
        ):
            verified.append(event_id)
        else:
            rejected.append(event_id)
    return verified, rejected


def resolve_appeal(
    db: Session,
    *,
    plan_version: str,
    appeal_id: str,
    decision: str,
    reviewer_id: str,
    note: str,
    verified_event_ids: list[str] | None = None,
) -> dict[str, Any]:
    appeal = repo.get_appeal(db, plan_version, appeal_id)
    if appeal is None:
        raise AppealNotFoundError(f"申诉 '{appeal_id}' 不存在")
    if appeal.state != "open":
        raise AppealStateError(f"申诉 '{appeal_id}' 已为 {appeal.state} 状态")
    if not reviewer_id.strip() or not note.strip():
        raise QualityError("裁决必须记录复核人和说明")
    if decision not in ("verified", "rejected"):
        raise QualityError("decision 必须是 verified 或 rejected")

    batch = repo.get_batch(db, plan_version, appeal.batch_id)
    assert batch is not None

    if decision == "verified":
        candidates = list(verified_event_ids or [])
        if not candidates:
            raise QualityError("核实通过必须给出经核实的来源事件")
        # 引用中不存在 / 不属于该学院 / 超出窗口与游标的事件一律记为查无实据，
        # 只有核实通过的来源事件会在修订版中被排除。
        verified, rejected = _verify_source_events(
            db,
            plan_version=plan_version,
            batch=batch,
            college_id=appeal.college_id,
            event_ids=candidates,
        )
        if not verified:
            raise QualityError(
                "没有任何引用事件通过来源核实（须存在、属于该学院且落在批次窗口内）"
            )
        repo.resolve_appeal(
            db,
            appeal,
            state="verified",
            verified_event_ids=verified,
            rejected_event_ids=rejected,
            resolution_note=note,
            reviewer_id=reviewer_id,
            resolved_at=_utcnow(),
        )
    else:
        rejected_ids = list(dict.fromkeys(verified_event_ids or []))
        repo.resolve_appeal(
            db,
            appeal,
            state="rejected",
            verified_event_ids=[],
            rejected_event_ids=rejected_ids,
            resolution_note=note,
            reviewer_id=reviewer_id,
            resolved_at=_utcnow(),
        )
    refreshed = repo.get_appeal(db, plan_version, appeal_id)
    assert refreshed is not None
    return _appeal_to_dict(refreshed)


def _appeal_to_dict(row: Any) -> dict[str, Any]:
    return {
        "appeal_id": row.appeal_id,
        "plan_version": row.plan_version,
        "batch_id": row.batch_id,
        "college_id": row.college_id,
        "metric": row.metric,
        "reason": row.reason,
        "state": row.state,
        "verified_event_ids": list(row.verified_event_ids),
        "rejected_event_ids": list(row.rejected_event_ids),
        "resolution_note": row.resolution_note,
        "revision_batch_id": row.revision_batch_id,
        "reviewer_id": row.reviewer_id,
        "created_at": _iso(row.created_at),
        "resolved_at": _iso(row.resolved_at) if row.resolved_at else None,
    }


def list_appeals(
    db: Session, *, plan_version: str, batch_id: str
) -> list[dict[str, Any]]:
    return [
        _appeal_to_dict(a)
        for a in repo.list_appeals_for_batch(db, plan_version, batch_id)
    ]


def create_revision(
    db: Session,
    *,
    plan_version: str,
    new_batch_id: str,
    base_batch_id: str,
    appeal_ids: list[str],
    cursor_event_id: str | None = None,
    colleges: list[str] | None = None,
) -> dict[str, Any]:
    """基于迟到数据与经核实申诉形成修订版（沿用原窗口与口径）。"""
    base = repo.get_batch(db, plan_version, base_batch_id)
    if base is None:
        raise BatchNotFoundError(f"批次 '{base_batch_id}' 不存在")

    excluded: set[str] = set()
    for appeal_id in appeal_ids:
        appeal = repo.get_appeal(db, plan_version, appeal_id)
        if appeal is None:
            raise AppealNotFoundError(f"申诉 '{appeal_id}' 不存在")
        if appeal.batch_id != base_batch_id:
            raise AppealStateError(
                f"申诉 '{appeal_id}' 不属于批次 '{base_batch_id}'"
            )
        if appeal.state != "verified":
            raise AppealStateError(
                f"申诉 '{appeal_id}' 状态为 {appeal.state}，只有已核实申诉可形成修订"
            )
        excluded.update(appeal.verified_event_ids)

    result = compute_batch(
        db,
        plan_version=plan_version,
        batch_id=new_batch_id,
        window_start=_as_utc(base.window_start),
        window_end=_as_utc(base.window_end),
        rule_version=base.rule_version,
        cursor_event_id=cursor_event_id,
        colleges=colleges,
        revision_of=base_batch_id,
        excluded_event_ids=excluded,
    )

    if appeal_ids and result["outcome"] in ("created", "recovered"):
        for appeal_id in appeal_ids:
            appeal = repo.get_appeal(db, plan_version, appeal_id)
            assert appeal is not None
            repo.resolve_appeal(db, appeal, revision_batch_id=new_batch_id)
    return result


# ---------------------------------------------------------------------------
# 趋势查询
# ---------------------------------------------------------------------------


def trends(
    db: Session,
    *,
    plan_version: str,
    college_id: str | None = None,
    rule_version: str | None = None,
    include_unsigned: bool = False,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    batches = repo.list_batches(db, plan_version)
    series: list[dict[str, Any]] = []
    for batch in batches:
        if rule_version is not None and batch.rule_version != rule_version:
            continue
        if not include_unsigned and batch.state not in ("signed", "superseded"):
            continue
        item_rows = repo.list_items(db, plan_version, batch.batch_id)
        points = []
        for item in item_rows:
            if college_id is not None and item.college_id != college_id:
                continue
            points.append(
                {
                    "college_id": item.college_id,
                    "sample_size": item.sample_size,
                    "composite_score": item.metrics.get("_composite", 0.0),
                    "metrics": {k: v for k, v in item.metrics.items() if k in METRICS},
                    "confidence_flags": item.confidence_flags,
                }
            )
        if college_id is not None and not points:
            continue
        series.append(
            {
                "batch_id": batch.batch_id,
                "state": batch.state,
                "rule_version": batch.rule_version,
                "window_start": _iso(batch.window_start),
                "window_end": _iso(batch.window_end),
                "revision_of": batch.revision_of,
                "signed_at": _iso(batch.signed_at) if batch.signed_at else None,
                "points": points,
            }
        )
    return {
        "plan_version": plan_version,
        "college_id": college_id,
        "rule_version": rule_version,
        "batches": series,
    }
