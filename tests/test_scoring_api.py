"""学院数据质量评分：API 端到端与并发 / 恢复测试。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest

from tests.conftest import TestSessionLocal

RV = "rv-2026-09"


def _configure(client, rule_version=RV, weights=None):
    body = {"created_by": "admin"}
    if weights is not None:
        body["weights"] = weights
    resp = client.post(f"/api/scoring/rule-versions/{rule_version}", json=body)
    assert resp.status_code == 200, resp.text
    pub = client.post(f"/api/scoring/rule-versions/{rule_version}/publish")
    assert pub.status_code == 200, pub.text
    return pub.json()


def _incident(
    rid,
    college,
    *,
    category="safety",
    occurred="2024-03-15T08:00:00+00:00",
    reported=None,
    fingerprint=None,
    payload=None,
):
    if reported is None:
        # 默认在发生后 5 小时上报 -> 及时（24h 内）。
        dt = datetime.fromisoformat(occurred) + timedelta(hours=5)
        reported = dt.isoformat()
    item = {
        "report_id": rid,
        "college_id": college,
        "category": category,
        "occurred_at": occurred,
        "reported_at": reported,
        "payload": payload if payload is not None else {"id": rid},
    }
    if fingerprint is not None:
        item["fingerprint"] = fingerprint
    return item


def _post_incidents(client, *items):
    resp = client.post("/api/scoring/incidents", json={"incidents": list(items)})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _run_batch(client, batch_id, **overrides):
    body = {
        "window_start": "2024-03-15",
        "window_end": "2024-03-16",
        "timezone": "UTC",
        "rule_version": RV,
        "created_by": "admin",
    }
    body.update(overrides)
    resp = client.post(f"/api/scoring/batches/{batch_id}", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- 配置与口径固定 -------------------------------------------------------

def test_rule_version_publish_then_frozen_and_weight_validation(client):
    # 权重之和必须为 1。
    bad = client.post(
        f"/api/scoring/rule-versions/{RV}",
        json={"created_by": "admin", "weights": {
            "timeliness": 0.5, "duplicate_rate": 0.2, "conflict_rate": 0.2}},
    )
    assert bad.status_code == 400, bad.text

    _configure(client)
    # 发布后再改口径应被拒绝（口径变化只能另出新版本）。
    again = client.post(
        f"/api/scoring/rule-versions/{RV}",
        json={"created_by": "admin", "metric_params": {
            "timeliness": {"deadline_hours": 48}}},
    )
    assert again.status_code == 400, again.text

    listed = client.get("/api/scoring/rule-versions").json()
    assert {v["rule_version"]: v["status"] for v in listed}[RV] == "published"


# --- 计算：分项证据与置信标记 ---------------------------------------------

def test_batch_produces_per_metric_evidence_and_confidence_flag(client):
    _configure(client)
    incidents = [
        # C-A：6 条全部及时、指纹唯一 -> 三项满分、高置信。
        *[_incident(f"A{i}", "C-A", fingerprint=f"fa{i}") for i in range(6)],
        # C-B：6 条同指纹同类 -> 5/6 重复；样本达标高置信。
        *[_incident(f"B{i}", "C-B", fingerprint="dup") for i in range(6)],
        # C-C：仅 2 条 -> 低于 min_sample=5，可评分但低置信。
        _incident("C1", "C-C", fingerprint="fc1"),
        _incident("C2", "C-C", fingerprint="fc2"),
    ]
    _post_incidents(client, *incidents)
    batch = _run_batch(client, "B1")

    by_college = {c["college_id"]: c for c in batch["colleges"]}
    assert by_college["C-A"]["composite_score"] == 100.0
    assert by_college["C-A"]["confident"] is True

    dup_metric = next(
        m for m in by_college["C-B"]["metrics"] if m["metric_code"] == "duplicate_rate"
    )
    assert dup_metric["value"] == pytest.approx(5 / 6)
    assert dup_metric["evidence"]["duplicate"] == 5
    assert dup_metric["confident"] is True

    assert by_college["C-C"]["confident"] is False
    assert all(
        m["confident"] is False for m in by_college["C-C"]["metrics"]
    )
    # 输入游标被固定为当前最大 report_id。
    assert batch["input_cursor"] == "C2"


def test_empty_window_batch_has_no_colleges(client):
    _configure(client)
    batch = _run_batch(client, "B-EMPTY")
    assert batch["status"] == "completed"
    assert batch["colleges"] == []
    assert batch["input_cursor"] is None
    card = client.post(
        "/api/scoring/batches/B-EMPTY/issue", json={"issued_by": "admin"}
    )
    assert card.status_code == 201, card.text
    assert card.json()["rankings"] == []


# --- 跨时区窗口 -----------------------------------------------------------

def test_window_anchored_in_new_york_with_dst_fallback(client):
    _configure(client)
    # 2024-11-03 是纽约 DST 回退日。事件发生于本地 01:30（EST, -05:00）
    # = 06:30Z，本地日期仍为 11-03，应被纳入 11-03 的纽约窗口。
    incidents = [
        _incident(
            "N1", "C-NY",
            occurred="2024-11-03T01:30:00-05:00",
            reported="2024-11-03T10:00:00-05:00",
            fingerprint="fn1",
        ),
        # 这条 UTC 06:30Z 但本地已是 11-04 01:30（上海视角），不应入纽约窗口。
        _incident(
            "N2", "C-NY",
            occurred="2024-11-04T01:30:00-05:00",
            reported="2024-11-04T10:00:00-05:00",
            fingerprint="fn2",
        ),
    ]
    _post_incidents(client, *incidents)
    body = {
        "window_start": "2024-11-03",
        "window_end": "2024-11-04",
        "timezone": "America/New_York",
        "rule_version": RV,
        "created_by": "admin",
    }
    resp = client.post("/api/scoring/batches/B-NY", json=body)
    assert resp.status_code == 200, resp.text
    batch = resp.json()
    # 窗口起点 = UTC 11-03 04:00（EDT），终点 = 11-04 05:00（EST），长 25h。
    assert batch["window"]["window_start"] == "2024-11-03T04:00:00Z"
    assert batch["window"]["window_end"] == "2024-11-04T05:00:00Z"
    college = {c["college_id"]: c for c in batch["colleges"]}["C-NY"]
    timeliness = next(
        m for m in college["metrics"] if m["metric_code"] == "timeliness"
    )
    # 只有 N1 落在窗口内。
    assert timeliness["sample_size"] == 1
    assert timeliness["evidence"]["total"] == 1


# --- 不可变签发 + 迟到数据形成修订版 --------------------------------------

def test_issued_scorecard_immutable_late_data_forms_revision(client):
    _configure(client)
    _post_incidents(
        client,
        *[_incident(f"A{i}", "C-A", fingerprint=f"fa{i}") for i in range(6)],
        *[_incident(f"B{i}", "C-B", fingerprint="dup") for i in range(6)],
    )
    batch = _run_batch(client, "B1")
    card1 = client.post(
        "/api/scoring/batches/B1/issue", json={"issued_by": "admin"}
    )
    assert card1.status_code == 201, card1.text
    first = card1.json()
    order = [r["college_id"] for r in first["rankings"]]
    assert order == ["C-A", "C-B"]
    fingerprint_before = first["fingerprint"]

    # 迟到数据：与 C-A 既有指纹 fa0 相同但报成不同类别 facility -> 形成冲突。
    _post_incidents(
        client,
        *[
            _incident(f"L{i}", "C-A", category="facility", fingerprint="fa0")
            for i in range(6)
        ],
    )

    # 重跑 B1 必须返回冻结的旧结果，已签发评分卡不变。
    rerun = _run_batch(client, "B1")
    assert rerun["input_cursor"] == batch["input_cursor"]
    # 再次签发（幂等）：评分卡排名与指纹不变。
    card_get = client.post(
        "/api/scoring/batches/B1/issue", json={"issued_by": "admin"}
    ).json()
    assert card_get["rankings"] == first["rankings"]
    assert card_get["fingerprint"] == fingerprint_before

    # 修订版 B2（revision_of=B1）纳入迟到数据，产生新排名。
    # 迟到记录与既有 A 系列同指纹、但报成不同类别 -> 形成冲突。
    rev = _run_batch(client, "B2", revision_of="B1")
    assert rev["revision_of"] == "B1"
    assert rev["input_cursor"] == "L5"
    ca = {c["college_id"]: c for c in rev["colleges"]}["C-A"]
    conflict = next(
        m for m in ca["metrics"] if m["metric_code"] == "conflict_rate"
    )
    assert conflict["sample_size"] == 12
    assert conflict["value"] > 0


# --- 并发签发：仅一个成功 -------------------------------------------------

def test_concurrent_issue_only_one_wins(client):
    from app import scoring_services

    _configure(client)
    _post_incidents(
        client,
        *[_incident(f"A{i}", "C-A", fingerprint=f"fa{i}") for i in range(6)],
    )
    _run_batch(client, "B-CONC")

    created_flags: list[bool] = []
    lock = threading.Lock()

    def _issue():
        session = TestSessionLocal()
        try:
            _, created = scoring_services.issue_scorecard(
                session, batch_id="B-CONC", issued_by="admin"
            )
            with lock:
                created_flags.append(created)
        finally:
            session.close()

    threads = [threading.Thread(target=_issue) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(created_flags) == 1
    assert len(created_flags) == 5
    stored = client.post(
        "/api/scoring/batches/B-CONC/issue", json={"issued_by": "admin"}
    ).json()
    assert [r["college_id"] for r in stored["rankings"]] == ["C-A"]


# --- 重启恢复：running 残留批次重算覆盖 -----------------------------------

def test_recovered_running_batch_is_recomputed(client, db):
    from app import scoring_repository as repo
    from app import scoring_services
    from app.scoring.engine import window_bounds

    _configure(client)
    _post_incidents(
        client,
        *[_incident(f"A{i}", "C-A", fingerprint=f"fa{i}") for i in range(6)],
    )

    # 模拟上次运行在写入结果前崩溃：只留下 running 批次行。
    window = window_bounds(
        __import__("datetime").date(2024, 3, 15),
        __import__("datetime").date(2024, 3, 16),
        "UTC",
    )
    assert repo.insert_batch(
        db,
        batch_id="B-RECOVER",
        window=window,
        rule_version=RV,
        input_cursor="A5",
        revision_of=None,
        created_by="admin",
    ) is not None
    # 新进程（新会话）看到 running 残留。
    fresh = TestSessionLocal()
    try:
        assert repo.get_batch(fresh, "B-RECOVER").status == "running"
        doc = scoring_services.run_batch(
            fresh,
            batch_id="B-RECOVER",
            window_start="2024-03-15",
            window_end="2024-03-16",
            timezone_name="UTC",
            created_by="admin",
            rule_version=RV,
            input_cursor="A5",
        )
        assert doc["status"] == "completed"
        assert {c["college_id"] for c in doc["colleges"]} == {"C-A"}
        # 再来一次：已完成则直接返回既有结果，幂等。
        again = scoring_services.run_batch(
            fresh,
            batch_id="B-RECOVER",
            window_start="2024-03-15",
            window_end="2024-03-16",
            timezone_name="UTC",
            created_by="admin",
            rule_version=RV,
            input_cursor="A5",
        )
        assert again["colleges"] == doc["colleges"]
    finally:
        fresh.close()


def test_recovered_batch_rejects_changed_fixture(client, db):
    from app import scoring_repository as repo
    from app import scoring_services
    from app.scoring.engine import window_bounds
    import datetime as _dt

    _configure(client)
    window = window_bounds(_dt.date(2024, 3, 15), _dt.date(2024, 3, 16), "UTC")
    repo.insert_batch(
        db, batch_id="B-FIX", window=window, rule_version=RV,
        input_cursor="A5", revision_of=None, created_by="admin",
    )
    fresh = TestSessionLocal()
    try:
        # 恢复时试图更改游标 -> 拒绝。
        try:
            scoring_services.run_batch(
                fresh, batch_id="B-FIX", window_start="2024-03-15",
                window_end="2024-03-16", timezone_name="UTC",
                created_by="admin", rule_version=RV, input_cursor="A9",
            )
        except scoring_services.ScoringError:
            pass
        else:
            raise AssertionError("更改固定游标应当被拒绝")
    finally:
        fresh.close()


# --- 申诉：只调整经核实来源，不改写历史 -----------------------------------

def test_appeal_verified_creates_revision_without_rewriting_rank(client):
    _configure(client)
    # C-A 的 R3 把同指纹 F 报成 facility，造成 3 条冲突；实为误分类。
    incidents = [
        _incident("R1", "C-A", category="safety", fingerprint="F"),
        _incident("R2", "C-A", category="safety", fingerprint="F"),
        _incident("R3", "C-A", category="facility", fingerprint="F"),
        _incident("R4", "C-A", category="safety", fingerprint="G"),
        _incident("R5", "C-A", category="safety", fingerprint="H"),
    ]
    _post_incidents(client, *incidents)
    _run_batch(client, "B1")
    client.post("/api/scoring/batches/B1/issue", json={"issued_by": "admin"})
    before_rankings = client.post(
        "/api/scoring/batches/B1/issue", json={"issued_by": "admin"}
    ).json()["rankings"]

    appeal = client.post(
        "/api/scoring/appeals",
        json={
            "appeal_id": "AP1",
            "batch_id": "B1",
            "college_id": "C-A",
            "metric_code": "conflict_rate",
            "reason": "R3 实为 safety 误填为 facility",
            "created_by": "C-A",
        },
    )
    assert appeal.status_code == 201, appeal.text

    resolved = client.post(
        "/api/scoring/appeals/AP1/resolve",
        json={
            "verified": True,
            "reviewed_by": "auditor",
            "resolution_note": "来源系统已核实并更正类别",
            "recategorized": {"R3": "safety"},
            "revision_batch_id": "B1-R1",
        },
    )
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["status"] == "verified"
    assert body["adjustment"] is not None and body["adjustment"] > 0

    # 历史评分卡排名不变。
    after_rankings = client.post(
        "/api/scoring/batches/B1/issue", json={"issued_by": "admin"}
    ).json()["rankings"]
    assert after_rankings == before_rankings

    # 修订批次反映经核实的来源：冲突清零。
    rev = client.get("/api/scoring/batches/B1-R1").json()
    assert rev["revision_of"] == "B1"
    metric = next(
        m for c in rev["colleges"] if c["college_id"] == "C-A"
        for m in c["metrics"] if m["metric_code"] == "conflict_rate"
    )
    assert metric["value"] == 0.0

    # 被驳回的申诉不产生调整。
    client.post(
        "/api/scoring/appeals",
        json={
            "appeal_id": "AP2", "batch_id": "B1", "college_id": "C-A",
            "metric_code": "duplicate_rate", "reason": "无依据异议",
            "created_by": "C-A",
        },
    )
    rejected = client.post(
        "/api/scoring/appeals/AP2/resolve",
        json={"verified": False, "reviewed_by": "auditor",
              "resolution_note": "来源核实无据"},
    ).json()
    assert rejected["status"] == "rejected"
    assert rejected["adjustment"] is None


def test_appeal_requires_issued_scorecard(client):
    _configure(client)
    _post_incidents(client, _incident("R1", "C-A", fingerprint="F"))
    _run_batch(client, "B1")  # 未签发
    resp = client.post(
        "/api/scoring/appeals",
        json={
            "appeal_id": "AP1", "batch_id": "B1", "college_id": "C-A",
            "metric_code": "timeliness", "reason": "x", "created_by": "C-A",
        },
    )
    assert resp.status_code == 409, resp.text


def test_revision_must_keep_parent_window_and_rule_version(client):
    _configure(client)
    _post_incidents(
        client,
        *[_incident(f"A{i}", "C-A", fingerprint=f"fa{i}") for i in range(6)],
    )
    _run_batch(client, "B1")

    # 配置并发布一个不同的规则版本。
    client.post(
        "/api/scoring/rule-versions/rv-2027-01",
        json={"created_by": "admin", "weights": {
            "timeliness": 0.5, "duplicate_rate": 0.25, "conflict_rate": 0.25}},
    )
    client.post("/api/scoring/rule-versions/rv-2027-01/publish")

    # 口径变化不能借修订版进入同一观察窗口。
    body = {
        "window_start": "2024-03-15",
        "window_end": "2024-03-16",
        "timezone": "UTC",
        "rule_version": "rv-2027-01",
        "revision_of": "B1",
        "created_by": "admin",
    }
    resp = client.post("/api/scoring/batches/B2", json=body)
    assert resp.status_code == 400, resp.text

    # 改变窗口同样被拒绝。
    body.update(rule_version=RV, window_start="2024-03-16")
    resp = client.post("/api/scoring/batches/B3", json=body)
    assert resp.status_code == 400, resp.text


# --- 趋势查询 -------------------------------------------------------------

def test_college_trend_across_issued_batches_and_revisions(client):
    _configure(client)
    _post_incidents(
        client,
        *[_incident(f"A{i}", "C-A", fingerprint=f"fa{i}") for i in range(6)],
    )
    _run_batch(client, "B1")
    client.post("/api/scoring/batches/B1/issue", json={"issued_by": "admin"})

    # 修订版并签发。
    _run_batch(client, "B2", revision_of="B1")
    client.post("/api/scoring/batches/B2/issue", json={"issued_by": "admin"})

    trend = client.get("/api/scoring/colleges/C-A/trend").json()
    assert trend["college_id"] == "C-A"
    assert [p["batch_id"] for p in trend["points"]] == ["B1", "B2"]
    assert trend["points"][0]["revision_of"] is None
    assert trend["points"][1]["revision_of"] == "B1"
    # 指标过滤。
    only_t = client.get(
        "/api/scoring/colleges/C-A/trend?metric_code=timeliness"
    ).json()
    for point in only_t["points"]:
        assert set(point["metrics"]) == {"timeliness"}
