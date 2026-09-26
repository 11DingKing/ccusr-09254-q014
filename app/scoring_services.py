"""学院数据质量评分：配置、计算、申诉、签发与趋势编排。

关键不变量：
- 计算批次固定观察窗口、输入游标与规则版本；
- 已完成批次与已签发评分卡不可变，口径变化只能另出新批次 / 修订版；
- 申诉只调整经核实的来源记录，并以修订批次体现，不回写历史排名。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from . import scoring_repository as repo
from .scoring.engine import (
    ObservationWindow,
    calculate_batch,
    recompute_metric,
    scorecard_fingerprint,
    window_bounds,
)
from .scoring.metrics import (
    DEFAULT_RULE_VERSION,
    METRIC_CODES,
    build_default_specs,
    derive_fingerprint,
)


class ScoringError(ValueError):
    """封装领域状态与业务约束。"""


class RuleVersionNotFound(ScoringError):
    pass


class BatchNotFound(ScoringError):
    pass


class BatchNotCompleted(ScoringError):
    pass


class BatchAlreadyIssued(ScoringError):
    pass


class AppealNotFound(ScoringError):
    pass


class AppealNotOpen(ScoringError):
    pass


# --- 配置：规则版本与指标定义 ---------------------------------------------

def configure_rule_version(
    db: Session,
    *,
    rule_version: str,
    created_by: str,
    weights: dict[str, float] | None = None,
    metric_params: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """创建或更新草稿态规则版本及其指标口径定义。"""
    weights = weights or {}
    missing = set(METRIC_CODES) - set(weights)
    if missing:
        default_weights = {
            "timeliness": 0.4,
            "duplicate_rate": 0.3,
            "conflict_rate": 0.3,
        }
        weights = {**default_weights, **weights}
    if any(not 0 <= w <= 1 for w in weights.values()):
        raise ScoringError("指标权重必须落在 [0,1]")
    total = sum(weights[c] for c in METRIC_CODES)
    if abs(total - 1.0) > 1e-9:
        raise ScoringError(f"指标权重之和必须为 1，当前为 {total}")

    row = repo.upsert_rule_version(
        db,
        rule_version=rule_version,
        weights=weights,
        created_by=created_by,
    )
    if row.status == "published":
        raise ScoringError("规则版本已发布，口径不可修改；请发布新版本")

    specs = build_default_specs(
        rule_version, weights=weights, params=metric_params
    )
    for spec in specs.values():
        repo.upsert_metric_definition(db, spec=spec)
    db.commit()
    return {
        "rule_version": rule_version,
        "status": row.status,
        "weights": weights,
        "metrics": [specs[c].to_document() for c in METRIC_CODES],
    }


def publish_rule_version(
    db: Session, *, rule_version: str
) -> dict[str, Any]:
    specs = repo.get_metric_specs(db, rule_version)
    if specs is None:
        raise RuleVersionNotFound(f"规则版本 '{rule_version}' 未配置指标定义")
    row = repo.publish_rule_version(db, rule_version=rule_version)
    if row is None:
        existing = repo.get_rule_version(db, rule_version)
        if existing is None:
            raise RuleVersionNotFound(f"规则版本 '{rule_version}' 不存在")
        raise ScoringError(f"规则版本当前状态为 {existing.status}，无法发布")
    return {
        "rule_version": rule_version,
        "status": row.status,
        "published_at": _iso_utc(row.published_at),
        "metrics": [specs[c].to_document() for c in METRIC_CODES],
    }


def list_rule_versions(db: Session) -> list[dict[str, Any]]:
    result = []
    for row in repo.list_rule_versions(db):
        result.append(
            {
                "rule_version": row.rule_version,
                "status": row.status,
                "weights": row.weights,
                "published_at": _iso_utc(row.published_at),
            }
        )
    return result


def _require_published_specs(db: Session, rule_version: str):
    row = repo.get_rule_version(db, rule_version)
    if row is None:
        raise RuleVersionNotFound(f"规则版本 '{rule_version}' 不存在")
    if row.status != "published":
        raise ScoringError(f"规则版本 '{rule_version}' 状态为 {row.status}，须先发布")
    specs = repo.get_metric_specs(db, rule_version)
    assert specs is not None
    return specs


# --- 输入：上报事件 -------------------------------------------------------

def import_incidents(
    db: Session, incidents: list[dict[str, Any]]
) -> dict[str, Any]:
    normalized: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item in incidents:
        try:
            occurred_at = _parse_aware(item["occurred_at"])
            reported_at = _parse_aware(item["reported_at"])
            category = str(item["category"])
            payload = item.get("payload", {})
            fingerprint = item.get("fingerprint") or derive_fingerprint(
                category, payload
            )
            normalized.append(
                {
                    "report_id": str(item["report_id"]),
                    "college_id": str(item["college_id"]),
                    "category": category,
                    "occurred_at": occurred_at,
                    "reported_at": reported_at,
                    "fingerprint": fingerprint,
                    "payload": payload,
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            rejected.append({"report_id": item.get("report_id"), "reason": str(exc)})
    accepted, duplicates = repo.insert_incidents(db, normalized)
    return {
        "accepted": len(accepted),
        "duplicates": duplicates,
        "rejected": rejected,
    }


def _parse_aware(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区 (RFC 3339)")
    return parsed.astimezone(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """SQLite 读回的时间可能丢失 tzinfo；写入时已为 UTC，读回按 UTC 解释。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _window_from_row(row) -> ObservationWindow:
    return ObservationWindow(
        start=_as_utc(row.window_start),
        end=_as_utc(row.window_end),
        timezone=row.timezone,
    )


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat().replace("+00:00", "Z")


# --- 计算批次 -------------------------------------------------------------

def _resolve_window(
    window_start: str, window_end: str, timezone_name: str
) -> ObservationWindow:
    try:
        return window_bounds(
            date.fromisoformat(window_start),
            date.fromisoformat(window_end),
            timezone_name,
        )
    except (ValueError, KeyError) as exc:
        raise ScoringError(f"观察窗口非法: {exc}") from exc


def run_batch(
    db: Session,
    *,
    batch_id: str,
    window_start: str,
    window_end: str,
    timezone_name: str,
    created_by: str,
    rule_version: str = DEFAULT_RULE_VERSION,
    input_cursor: str | None = None,
    revision_of: str | None = None,
) -> dict[str, Any]:
    """创建并计算批次；对同一 batch_id 幂等。

    - 已完成批次直接返回既有结果（口径 / 窗口 / 游标固定不变）；
    - running 批次（上次计算中断）会重算覆盖，实现重启恢复。
    """
    specs = _require_published_specs(db, rule_version)
    window = _resolve_window(window_start, window_end, timezone_name)

    parent = None
    if revision_of is not None:
        parent = repo.get_batch(db, revision_of)
        if parent is None:
            raise BatchNotFound(f"被修订批次 '{revision_of}' 不存在")
        # 修订版沿用父批次的观察窗口、时区与规则版本；只有输入游标推进，
        # 以纳入迟到数据或经核实的来源修正。
        if parent.rule_version != rule_version:
            raise ScoringError("修订版必须沿用父批次的规则版本")
        if (
            _as_utc(parent.window_start) != window.start
            or _as_utc(parent.window_end) != window.end
            or parent.timezone != timezone_name
        ):
            raise ScoringError("修订版必须沿用父批次的观察窗口与时区")

    # 输入游标在创建批次时即固定：显式传入优先，否则取当前最大 report_id。
    fixed_cursor = input_cursor or repo.max_report_cursor(db)

    existing = repo.get_batch(db, batch_id)
    if existing is not None:
        if existing.status == "completed":
            return _batch_document(existing, repo.load_score_results(db, batch_id))
        # running 残留：校验三元组一致后重算（重启恢复）。
        _assert_same_fixture(existing, window, rule_version, fixed_cursor)
    else:
        created = repo.insert_batch(
            db,
            batch_id=batch_id,
            window=window,
            rule_version=rule_version,
            input_cursor=fixed_cursor,
            revision_of=revision_of,
            created_by=created_by,
        )
        if created is None:
            # 并发下另一线程刚插入；回到既有分支。
            existing = repo.get_batch(db, batch_id)
            assert existing is not None
            if existing.status == "completed":
                return _batch_document(
                    existing, repo.load_score_results(db, batch_id)
                )
            _assert_same_fixture(existing, window, rule_version, fixed_cursor)

    incidents = repo.load_incidents(
        db, window_start=window.start, window_end=window.end
    )
    computation = calculate_batch(
        batch_id=batch_id,
        incidents=incidents,
        window=window,
        specs=specs,
        input_cursor=fixed_cursor,
        revision_of=revision_of,
    )
    manifest = computation.to_manifest()
    repo.replace_score_results(db, batch_id=batch_id, manifest=manifest)
    row = repo.get_batch(db, batch_id)
    assert row is not None
    return _batch_document(row, repo.load_score_results(db, batch_id))


def _assert_same_fixture(
    row, window: ObservationWindow, rule_version: str, input_cursor: str | None
) -> None:
    if row.rule_version != rule_version:
        raise ScoringError("批次规则版本已固定，不可更改")
    if row.input_cursor != input_cursor:
        raise ScoringError("批次输入游标已固定，不可更改")
    if _as_utc(row.window_start) != window.start or _as_utc(row.window_end) != window.end:
        raise ScoringError("批次观察窗口已固定，不可更改")


def get_batch(db: Session, batch_id: str) -> dict[str, Any]:
    row = repo.get_batch(db, batch_id)
    if row is None:
        raise BatchNotFound(f"批次 '{batch_id}' 不存在")
    return _batch_document(row, repo.load_score_results(db, batch_id))


def list_batches(db: Session, *, revision_of: str | None = None) -> list[dict[str, Any]]:
    return [
        {
            "batch_id": r.batch_id,
            "status": r.status,
            "rule_version": r.rule_version,
            "window_start": _iso_utc(r.window_start),
            "window_end": _iso_utc(r.window_end),
            "timezone": r.timezone,
            "input_cursor": r.input_cursor,
            "revision_of": r.revision_of,
        }
        for r in repo.list_batches(db, revision_of=revision_of)
    ]


def _batch_document(row, results) -> dict[str, Any]:
    """组装批次文档。

    已完成批次的综合分 / 置信标记直接取自计算时冻结的 manifest（口径的
    确定性产物）；分项结果表提供逐指标证据。running 残留批次可能尚无
    manifest，则综合分留空，等待重算覆盖。
    """
    manifest_colleges = {c["college_id"]: c for c in (row.manifest or {}).get("colleges", [])}
    colleges: dict[str, dict[str, Any]] = {}
    for result in results:
        college = colleges.setdefault(
            result.college_id,
            {
                "college_id": result.college_id,
                "composite_score": manifest_colleges.get(
                    result.college_id, {}
                ).get("composite_score"),
                "confident": manifest_colleges.get(
                    result.college_id, {}
                ).get("confident", False),
                "metrics": [],
            },
        )
        college["metrics"].append(
            {
                "metric_code": result.metric_code,
                "value": None if result.value < 0 else result.value,
                "score": None if result.score < 0 else result.score,
                "sample_size": result.sample_size,
                "confident": result.confident,
                "evidence": result.evidence,
            }
        )
    for college in colleges.values():
        college["metrics"].sort(key=lambda m: METRIC_CODES.index(m["metric_code"]))
    # manifest 中存在、但结果表尚未写完（极端中断）的学院也补入。
    for college_id, stored in manifest_colleges.items():
        if college_id not in colleges:
            colleges[college_id] = {
                "college_id": college_id,
                "composite_score": stored.get("composite_score"),
                "confident": stored.get("confident", False),
                "metrics": stored.get("metrics", []),
            }
    return {
        "batch_id": row.batch_id,
        "status": row.status,
        "rule_version": row.rule_version,
        "revision_of": row.revision_of,
        "window": {
            "window_start": _iso_utc(row.window_start),
            "window_end": _iso_utc(row.window_end),
            "timezone": row.timezone,
        },
        "input_cursor": row.input_cursor,
        "colleges": [colleges[cid] for cid in sorted(colleges)],
    }


# --- 签发 -----------------------------------------------------------------

def issue_scorecard(
    db: Session, *, batch_id: str, issued_by: str
) -> tuple[dict[str, Any], bool]:
    """签发不可变评分卡；并发签发同一批次仅一个成功。"""
    row = repo.get_batch(db, batch_id)
    if row is None:
        raise BatchNotFound(f"批次 '{batch_id}' 不存在")
    if row.status != "completed":
        raise BatchNotCompleted(f"批次 '{batch_id}' 尚未完成计算")

    existing = repo.get_scorecard(db, batch_id)
    if existing is not None:
        return _scorecard_document(existing), False

    # 排名是批次冻结 manifest 的确定性产物；修订版的来源修正已含在其
    # manifest 中，故不再从原始事件重算（否则会丢失申诉修正）。
    rankings = _rankings_from_manifest(row.manifest)
    window = _window_from_row(row)
    fingerprint = scorecard_fingerprint(
        batch_id=batch_id,
        rule_version=row.rule_version,
        input_cursor=row.input_cursor,
        window=window,
        rankings=rankings,
    )
    inserted = repo.insert_scorecard(
        db,
        batch_id=batch_id,
        rule_version=row.rule_version,
        window=window,
        input_cursor=row.input_cursor,
        rankings=rankings,
        issued_by=issued_by,
        fingerprint=fingerprint,
    )
    if inserted is None:
        # 并发签发落败：返回既有的那张。
        winner = repo.get_scorecard(db, batch_id)
        assert winner is not None
        return _scorecard_document(winner), False
    return _scorecard_document(inserted), True


def _rankings_from_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """从批次冻结的 manifest 确定性生成排名（与 rank_colleges 口径一致）。"""
    colleges = manifest.get("colleges", [])
    scorable = [c for c in colleges if c.get("composite_score") is not None]
    unranked = [c for c in colleges if c.get("composite_score") is None]
    ordered = sorted(
        scorable, key=lambda c: (-c["composite_score"], c["college_id"])
    )
    rankings: list[dict[str, Any]] = [
        {
            "rank": position,
            "college_id": c["college_id"],
            "composite_score": c["composite_score"],
            "confident": bool(c.get("confident", False)),
        }
        for position, c in enumerate(ordered, start=1)
    ]
    for c in sorted(unranked, key=lambda x: x["college_id"]):
        rankings.append(
            {
                "rank": None,
                "college_id": c["college_id"],
                "composite_score": None,
                "confident": False,
            }
        )
    return rankings


def get_scorecard(db: Session, batch_id: str) -> dict[str, Any]:
    row = repo.get_scorecard(db, batch_id)
    if row is None:
        if repo.get_batch(db, batch_id) is None:
            raise BatchNotFound(f"批次 '{batch_id}' 不存在")
        raise BatchNotCompleted(f"批次 '{batch_id}' 尚未签发")
    return _scorecard_document(row)


def _scorecard_document(row) -> dict[str, Any]:
    return {
        "batch_id": row.batch_id,
        "rule_version": row.rule_version,
        "window": {
            "window_start": _iso_utc(row.window_start),
            "window_end": _iso_utc(row.window_end),
            "timezone": row.timezone,
        },
        "input_cursor": row.input_cursor,
        "rankings": row.rankings,
        "issued_by": row.issued_by,
        "issued_at": _iso_utc(row.issued_at),
        "fingerprint": row.fingerprint,
    }


# --- 申诉 -----------------------------------------------------------------

def open_appeal(
    db: Session,
    *,
    appeal_id: str,
    batch_id: str,
    college_id: str,
    metric_code: str,
    reason: str,
    created_by: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if metric_code not in METRIC_CODES:
        raise ScoringError(f"未知指标 {metric_code}")
    scorecard = repo.get_scorecard(db, batch_id)
    if scorecard is None:
        raise BatchNotCompleted("只能对已签发评分卡提出申诉")
    if not reason.strip():
        raise ScoringError("申诉理由不能为空")
    row = repo.insert_appeal(
        db,
        appeal_id=appeal_id,
        batch_id=batch_id,
        college_id=college_id,
        metric_code=metric_code,
        reason=reason,
        evidence=evidence or {},
        created_by=created_by,
    )
    if row is None:
        raise ScoringError(f"申诉 '{appeal_id}' 已存在")
    return _appeal_document(row)


def resolve_appeal(
    db: Session,
    *,
    appeal_id: str,
    verified: bool,
    reviewed_by: str,
    resolution_note: str,
    # 经核实的来源修正：
    excluded_report_ids: list[str] | None = None,
    recategorized: dict[str, str] | None = None,
    revision_batch_id: str | None = None,
) -> dict[str, Any]:
    """核实申诉。仅 verified 且给出来源修正时，重算指标并产出修订批次。"""
    appeal = repo.get_appeal(db, appeal_id)
    if appeal is None:
        raise AppealNotFound(f"申诉 '{appeal_id}' 不存在")
    if appeal.status != "open":
        raise AppealNotOpen(f"申诉 '{appeal_id}' 已为 {appeal.status} 状态")

    adjustment: float | None = None
    if verified:
        batch = repo.get_batch(db, appeal.batch_id)
        assert batch is not None
        specs = _require_published_specs(db, batch.rule_version)
        window = _window_from_row(batch)
        excluded = excluded_report_ids or []
        recategorized = recategorized or {}

        # 只在父批次固定的输入（游标内）上评估核实前后差异。
        own_incidents = [
            incident
            for incident in repo.load_incidents(
                db,
                window_start=window.start,
                window_end=window.end,
                college_id=appeal.college_id,
            )
            if batch.input_cursor is None or incident.report_id <= batch.input_cursor
        ]
        before = recompute_metric(
            incidents=own_incidents,
            metric_code=appeal.metric_code,
            spec=specs[appeal.metric_code],
        )
        after = recompute_metric(
            incidents=own_incidents,
            metric_code=appeal.metric_code,
            spec=specs[appeal.metric_code],
            excluded_report_ids=excluded,
            recategorized=recategorized,
        )
        if before.score is not None and after.score is not None:
            adjustment = round(after.score - before.score, 6)

        if revision_batch_id and (excluded or recategorized):
            # 修订版固定与父批次相同的窗口、游标与规则版本；仅替换申诉
            # 学院经核实的来源记录，其他学院输入不变。历史排名保持不变。
            all_incidents = repo.load_incidents(
                db, window_start=window.start, window_end=window.end
            )
            from .scoring.engine import apply_source_corrections

            other_incidents = [
                incident
                for incident in all_incidents
                if incident.college_id != appeal.college_id
            ]
            corrected_own = apply_source_corrections(
                own_incidents,
                excluded_report_ids=excluded,
                recategorized=recategorized,
            )
            _create_revision(
                db,
                revision_batch_id=revision_batch_id,
                parent=batch,
                window=window,
                specs=specs,
                incidents=other_incidents + corrected_own,
                created_by=reviewed_by,
            )

    status = "verified" if verified else "rejected"
    row = repo.resolve_appeal(
        db,
        appeal_id=appeal_id,
        status=status,
        resolution_note=resolution_note,
        adjustment=adjustment,
        reviewed_by=reviewed_by,
    )
    assert row is not None
    return _appeal_document(row)


def _create_revision(
    db: Session,
    *,
    revision_batch_id: str,
    parent,
    window: ObservationWindow,
    specs,
    incidents,
    created_by: str,
) -> None:
    existing = repo.get_batch(db, revision_batch_id)
    if existing is not None and existing.status == "completed":
        return
    if existing is None:
        created = repo.insert_batch(
            db,
            batch_id=revision_batch_id,
            window=window,
            rule_version=parent.rule_version,
            input_cursor=parent.input_cursor,
            revision_of=parent.batch_id,
            created_by=created_by,
        )
        if created is None:
            return
    computation = calculate_batch(
        batch_id=revision_batch_id,
        incidents=incidents,
        window=window,
        specs=specs,
        input_cursor=parent.input_cursor,
        revision_of=parent.batch_id,
    )
    repo.replace_score_results(
        db, batch_id=revision_batch_id, manifest=computation.to_manifest()
    )


def get_appeal(db: Session, appeal_id: str) -> dict[str, Any]:
    row = repo.get_appeal(db, appeal_id)
    if row is None:
        raise AppealNotFound(f"申诉 '{appeal_id}' 不存在")
    return _appeal_document(row)


def list_appeals(db: Session, *, batch_id: str | None = None) -> list[dict[str, Any]]:
    return [_appeal_document(r) for r in repo.list_appeals(db, batch_id=batch_id)]


def _appeal_document(row) -> dict[str, Any]:
    return {
        "appeal_id": row.appeal_id,
        "batch_id": row.batch_id,
        "college_id": row.college_id,
        "metric_code": row.metric_code,
        "reason": row.reason,
        "evidence": row.evidence,
        "status": row.status,
        "resolution_note": row.resolution_note,
        "adjustment": row.adjustment,
        "created_by": row.created_by,
        "reviewed_by": row.reviewed_by,
        "resolved_at": _iso_utc(row.resolved_at),
    }


# --- 趋势查询 -------------------------------------------------------------

def college_trend(
    db: Session, *, college_id: str, metric_code: str | None = None
) -> dict[str, Any]:
    """跨已签发评分卡返回某学院（某指标）随观察窗口的趋势。

    只统计正式签发的批次，修订版（revision_of 非空）单独标注，避免把
    同一观察窗口的初版与修订版重复计入主线。
    """
    scorecards = repo.load_scorecards_for_college(db, college_id=college_id)
    points: list[dict[str, Any]] = []
    for card in sorted(scorecards, key=lambda c: (c.window_start, c.batch_id)):
        batch = repo.get_batch(db, card.batch_id)
        entry = next(
            e for e in card.rankings if e["college_id"] == college_id
        )
        point: dict[str, Any] = {
            "batch_id": card.batch_id,
            "revision_of": batch.revision_of if batch else None,
            "window_start": _iso_utc(card.window_start),
            "window_end": _iso_utc(card.window_end),
            "rank": entry["rank"],
            "composite_score": entry["composite_score"],
            "confident": entry["confident"],
            "metrics": {},
        }
        results = repo.load_score_results(db, card.batch_id)
        for result in results:
            if result.college_id != college_id:
                continue
            if metric_code and result.metric_code != metric_code:
                continue
            point["metrics"][result.metric_code] = {
                "value": None if result.value < 0 else result.value,
                "score": None if result.score < 0 else result.score,
                "sample_size": result.sample_size,
                "confident": result.confident,
            }
        points.append(point)
    return {"college_id": college_id, "metric_code": metric_code, "points": points}
