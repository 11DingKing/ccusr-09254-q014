"""学院数据质量评分 HTTP 接口：配置、计算、申诉、签发、趋势查询。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import scoring_services as services
from .db import get_db
from .scoring_schemas import (
    AppealIn,
    AppealOut,
    AppealResolveIn,
    BatchIn,
    BatchOut,
    BatchSummaryOut,
    IncidentBatchIn,
    IncidentImportResult,
    IssueIn,
    RuleVersionIn,
    RuleVersionOut,
    ScorecardOut,
    TrendOut,
)

router = APIRouter(prefix="/api/scoring")


def _raise(exc: Exception) -> HTTPException:
    mapping = {
        services.RuleVersionNotFound: 404,
        services.BatchNotFound: 404,
        services.AppealNotFound: 404,
        services.BatchNotCompleted: 409,
        services.BatchAlreadyIssued: 409,
        services.AppealNotOpen: 409,
        services.ScoringError: 400,
    }
    for exc_type, code in mapping.items():
        if isinstance(exc, exc_type):
            return HTTPException(status_code=code, detail=str(exc))
    raise exc


# --- 配置 -----------------------------------------------------------------

@router.post(
    "/rule-versions/{rule_version}",
    response_model=RuleVersionOut,
)
def put_rule_version(
    rule_version: str, body: RuleVersionIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.configure_rule_version(
            db,
            rule_version=rule_version,
            created_by=body.created_by,
            weights=body.weights,
            metric_params=body.metric_params,
        )
    except services.ScoringError as exc:
        raise _raise(exc) from exc


@router.post(
    "/rule-versions/{rule_version}/publish",
    response_model=RuleVersionOut,
)
def publish_rule_version(
    rule_version: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.publish_rule_version(db, rule_version=rule_version)
    except services.ScoringError as exc:
        raise _raise(exc) from exc


@router.get("/rule-versions", response_model=list[RuleVersionOut])
def list_rule_versions(db: Session = Depends(get_db)) -> Any:
    return services.list_rule_versions(db)


# --- 输入 -----------------------------------------------------------------

@router.post(
    "/incidents",
    response_model=IncidentImportResult,
    status_code=status.HTTP_201_CREATED,
)
def post_incidents(
    body: IncidentBatchIn, db: Session = Depends(get_db)
) -> Any:
    return services.import_incidents(
        db, [inc.model_dump() for inc in body.incidents]
    )


# --- 计算 -----------------------------------------------------------------

@router.post("/batches/{batch_id}", response_model=BatchOut)
def post_batch(
    batch_id: str, body: BatchIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.run_batch(
            db,
            batch_id=batch_id,
            window_start=body.window_start,
            window_end=body.window_end,
            timezone_name=body.timezone,
            created_by=body.created_by,
            rule_version=body.rule_version,
            input_cursor=body.input_cursor,
            revision_of=body.revision_of,
        )
    except services.ScoringError as exc:
        raise _raise(exc) from exc


@router.get("/batches", response_model=list[BatchSummaryOut])
def get_batches(
    revision_of: str | None = None, db: Session = Depends(get_db)
) -> Any:
    return services.list_batches(db, revision_of=revision_of)


@router.get("/batches/{batch_id}", response_model=BatchOut)
def get_batch(batch_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services.get_batch(db, batch_id)
    except services.ScoringError as exc:
        raise _raise(exc) from exc


# --- 签发 -----------------------------------------------------------------

@router.post(
    "/batches/{batch_id}/issue",
    response_model=ScorecardOut,
    status_code=status.HTTP_201_CREATED,
)
def post_issue(
    batch_id: str, body: IssueIn, db: Session = Depends(get_db)
) -> Any:
    try:
        doc, created = services.issue_scorecard(
            db, batch_id=batch_id, issued_by=body.issued_by
        )
        return doc
    except services.ScoringError as exc:
        raise _raise(exc) from exc


# --- 申诉 -----------------------------------------------------------------

@router.post(
    "/appeals",
    response_model=AppealOut,
    status_code=status.HTTP_201_CREATED,
)
def post_appeal(body: AppealIn, db: Session = Depends(get_db)) -> Any:
    try:
        return services.open_appeal(
            db,
            appeal_id=body.appeal_id,
            batch_id=body.batch_id,
            college_id=body.college_id,
            metric_code=body.metric_code,
            reason=body.reason,
            evidence=body.evidence,
            created_by=body.created_by,
        )
    except services.ScoringError as exc:
        raise _raise(exc) from exc


@router.get("/appeals", response_model=list[AppealOut])
def get_appeals(
    batch_id: str | None = None, db: Session = Depends(get_db)
) -> Any:
    return services.list_appeals(db, batch_id=batch_id)


@router.get("/appeals/{appeal_id}", response_model=AppealOut)
def get_appeal(appeal_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services.get_appeal(db, appeal_id)
    except services.ScoringError as exc:
        raise _raise(exc) from exc


@router.post("/appeals/{appeal_id}/resolve", response_model=AppealOut)
def post_resolve_appeal(
    appeal_id: str, body: AppealResolveIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.resolve_appeal(
            db,
            appeal_id=appeal_id,
            verified=body.verified,
            reviewed_by=body.reviewed_by,
            resolution_note=body.resolution_note,
            excluded_report_ids=body.excluded_report_ids,
            recategorized=body.recategorized,
            revision_batch_id=body.revision_batch_id,
        )
    except services.ScoringError as exc:
        raise _raise(exc) from exc


# --- 趋势查询 -------------------------------------------------------------

@router.get("/colleges/{college_id}/trend", response_model=TrendOut)
def get_trend(
    college_id: str,
    metric_code: str | None = None,
    db: Session = Depends(get_db),
) -> Any:
    return services.college_trend(
        db, college_id=college_id, metric_code=metric_code
    )
