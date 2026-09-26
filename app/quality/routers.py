"""学院数据质量评分 API：配置、计算、申诉、签发与趋势查询。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from . import service
from .schemas import (
    AppealIn,
    AppealOut,
    AppealResolveIn,
    BatchIn,
    BatchOut,
    RevisionIn,
    RuleIn,
    RuleOut,
    SignOut,
    TrendOut,
)

router = APIRouter(prefix="/api/plans/{plan_version}/quality")

_NOT_FOUND = (
    service.BatchNotFoundError,
    service.RuleNotFoundError,
    service.AppealNotFoundError,
)
_CONFLICT = (
    service.BatchConflictError,
    service.RuleStateError,
    service.AppealStateError,
)


def _raise(exc: Exception) -> None:
    if isinstance(exc, _NOT_FOUND):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, _CONFLICT):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, service.CursorNotFoundError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 配置：指标定义 / 规则版本
# ---------------------------------------------------------------------------


@router.post("/rules", response_model=RuleOut, status_code=201)
def create_rule(
    plan_version: str, body: RuleIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return service.create_rule(
            db,
            plan_version=plan_version,
            rule_version=body.rule_version,
            timezone=body.timezone,
            deadline_hours=body.deadline_hours,
            grace_seconds=body.grace_seconds,
            duplicate_window_seconds=body.duplicate_window_seconds,
            overlap_tolerance_seconds=body.overlap_tolerance_seconds,
            min_sample_size=body.min_sample_size,
            weights=body.weights,
        )
    except service.QualityError as exc:
        _raise(exc)


@router.post("/rules/{rule_version}/publish", response_model=RuleOut)
def publish_rule(
    plan_version: str, rule_version: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return service.publish_rule(
            db, plan_version=plan_version, rule_version=rule_version
        )
    except service.QualityError as exc:
        _raise(exc)


@router.get("/rules", response_model=list[RuleOut])
def list_rules(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        return service.list_rules(db, plan_version=plan_version)
    except service.QualityError as exc:
        _raise(exc)


# ---------------------------------------------------------------------------
# 计算批次
# ---------------------------------------------------------------------------


@router.post("/batches/{batch_id}/compute", response_model=BatchOut, status_code=201)
def compute_batch(
    plan_version: str,
    batch_id: str,
    body: BatchIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return service.compute_batch(
            db,
            plan_version=plan_version,
            batch_id=batch_id,
            window_start=body.window_start,
            window_end=body.window_end,
            rule_version=body.rule_version,
            cursor_event_id=body.cursor_event_id,
            colleges=body.colleges,
        )
    except service.QualityError as exc:
        _raise(exc)


@router.get("/batches/{batch_id}", response_model=BatchOut)
def get_batch(
    plan_version: str, batch_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return service.get_batch(db, plan_version=plan_version, batch_id=batch_id)
    except service.QualityError as exc:
        _raise(exc)


@router.get("/batches", response_model=list[BatchOut])
def list_batches(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        return service.list_batches(db, plan_version=plan_version)
    except service.QualityError as exc:
        _raise(exc)


# ---------------------------------------------------------------------------
# 签发
# ---------------------------------------------------------------------------


@router.post("/batches/{batch_id}/sign", response_model=SignOut)
def sign_batch(
    plan_version: str, batch_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        result = service.sign_batch(
            db, plan_version=plan_version, batch_id=batch_id
        )
    except service.QualityError as exc:
        _raise(exc)
    return {
        "outcome": result["outcome"],
        "plan_version": plan_version,
        "batch_id": batch_id,
        "state": result["state"],
        "signed_at": result["signed_at"],
        "signature": result["signature"],
    }


# ---------------------------------------------------------------------------
# 申诉与修订
# ---------------------------------------------------------------------------


@router.post("/appeals", response_model=AppealOut, status_code=201)
def create_appeal(
    plan_version: str, body: AppealIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return service.create_appeal(
            db,
            plan_version=plan_version,
            appeal_id=body.appeal_id,
            batch_id=body.batch_id,
            college_id=body.college_id,
            metric=body.metric,
            reason=body.reason,
            event_ids=body.event_ids,
        )
    except service.QualityError as exc:
        _raise(exc)


@router.post("/appeals/{appeal_id}/resolve", response_model=AppealOut)
def resolve_appeal(
    plan_version: str,
    appeal_id: str,
    body: AppealResolveIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return service.resolve_appeal(
            db,
            plan_version=plan_version,
            appeal_id=appeal_id,
            decision=body.decision,
            reviewer_id=body.reviewer_id,
            note=body.note,
            verified_event_ids=body.verified_event_ids,
        )
    except service.QualityError as exc:
        _raise(exc)


@router.get("/batches/{batch_id}/appeals", response_model=list[AppealOut])
def list_appeals(
    plan_version: str, batch_id: str, db: Session = Depends(get_db)
) -> Any:
    return service.list_appeals(db, plan_version=plan_version, batch_id=batch_id)


@router.post("/revisions/{new_batch_id}", response_model=BatchOut, status_code=201)
def create_revision(
    plan_version: str,
    new_batch_id: str,
    body: RevisionIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return service.create_revision(
            db,
            plan_version=plan_version,
            new_batch_id=new_batch_id,
            base_batch_id=body.base_batch_id,
            appeal_ids=body.appeal_ids,
            cursor_event_id=body.cursor_event_id,
            colleges=body.colleges,
        )
    except service.QualityError as exc:
        _raise(exc)


# ---------------------------------------------------------------------------
# 趋势查询
# ---------------------------------------------------------------------------


@router.get("/trends", response_model=TrendOut)
def get_trends(
    plan_version: str,
    college_id: str | None = None,
    rule_version: str | None = None,
    include_unsigned: bool = False,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return service.trends(
            db,
            plan_version=plan_version,
            college_id=college_id,
            rule_version=rule_version,
            include_unsigned=include_unsigned,
        )
    except service.QualityError as exc:
        _raise(exc)
