"""质量评分 API 测试：跨时区窗口、并发签发、空样本、重启恢复、
迟到数据修订版、申诉来源核实、口径版本不可重写与趋势查询。"""

from __future__ import annotations

import threading
from datetime import datetime

import pytest

from app import quality
from app.quality import service
from tests.conftest import TestSessionLocal


def _setup_plan(client, plan_version="P1", tz="Asia/Shanghai", **rule_kw):
    resp = client.post(
        "/api/plans",
        json={"plan_version": plan_version, "iana_timezone": tz, "required_seconds": 0},
    )
    assert resp.status_code == 201, resp.text
    payload = {"rule_version": "v1", "timezone": tz, "min_sample_size": 2}
    payload.update(rule_kw)
    r = client.post(f"/api/plans/{plan_version}/quality/rules", json=payload)
    assert r.status_code == 201, r.text
    r = client.post(f"/api/plans/{plan_version}/quality/rules/v1/publish")
    assert r.status_code == 200, r.text


def _checkin(eid, college, student, start, end, reported=None, activity="A1"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "college_id": college,
        "payload": {
            "activity_id": activity,
            "activity_type": "regular",
            "check_in_at": start,
            "check_out_at": end,
            "occurred_at": start,
            "reported_at": reported or start,
        },
    }


def _post_events(client, plan_version, events):
    r = client.post(f"/api/plans/{plan_version}/events", json={"events": events})
    assert r.status_code == 201, r.text
    return r.json()


def _compute(client, plan_version, batch_id, *, start, end, rule="v1", **extra):
    r = client.post(
        f"/api/plans/{plan_version}/quality/batches/{batch_id}/compute",
        json={
            "window_start": start,
            "window_end": end,
            "rule_version": rule,
            **extra,
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


WINDOW = ("2024-03-01T00:00:00+08:00", "2024-04-01T00:00:00+08:00")


def _items_by_college(batch):
    return {item["college_id"]: item for item in batch["items"]}


# ---------------------------------------------------------------------------
# 跨时区窗口
# ---------------------------------------------------------------------------


def test_window_pinned_in_rule_timezone_and_cursor_fixes_input(client):
    _setup_plan(client)
    pv = "P1"
    # E-LOCAL：上海本地 3/31 18:00（= 3/31 10:00Z）在窗口内；
    # E-UTC：3/31 17:00Z（= 上海 4/1 01:00）已超出本地窗口。
    _post_events(client, pv, [
        _checkin("E1", "C-A", "S1",
                 "2024-03-31T18:00:00+08:00", "2024-03-31T19:00:00+08:00",
                 reported="2024-03-31T20:00:00+08:00"),
        _checkin("E2", "C-A", "S2",
                 "2024-03-31T17:00:00+00:00", "2024-03-31T18:00:00+00:00",
                 reported="2024-03-31T19:00:00+00:00"),
    ])
    batch = _compute(client, pv, "B1", start=WINDOW[0], end=WINDOW[1])
    item = _items_by_college(batch)["C-A"]
    assert item["sample_size"] == 1
    assert item["evidence"]["sample_event_ids"] == ["E1"]
    # 窗口在规则时区解释后固定为 UTC 边界。
    assert batch["window_start"] == "2024-02-29T16:00:00Z"
    assert batch["window_end"] == "2024-03-31T16:00:00Z"

    # 输入游标：批次固定到 E1；之后到达的 E9（落在窗口内）不影响 B1。
    _post_events(client, pv, [
        _checkin("E9", "C-A", "S3",
                 "2024-03-15T09:00:00+08:00", "2024-03-15T10:00:00+08:00")
    ])
    again = client.post(
        f"/api/plans/{pv}/quality/batches/B1/compute",
        json={"window_start": WINDOW[0], "window_end": WINDOW[1], "rule_version": "v1"},
    ).json()
    assert again["outcome"] == "existing"
    assert _items_by_college(again)["C-A"]["sample_size"] == 1
    # 游标按最新事件（E2）固定；E2 虽在窗口之外，仍是输入集的一部分。
    assert again["cursor_event_id"] == "E2"
    assert again["cursor_event_pk"] == batch["cursor_event_pk"] == 2


def test_explicit_cursor_then_revision_advances(client):
    _setup_plan(client)
    pv = "P1"
    _post_events(client, pv, [
        _checkin("E1", "C-A", "S1",
                 "2024-03-10T09:00:00+08:00", "2024-03-10T10:00:00+08:00"),
        _checkin("E2", "C-A", "S2",
                 "2024-03-11T09:00:00+08:00", "2024-03-11T10:00:00+08:00"),
    ])
    b1 = _compute(client, pv, "B1", start=WINDOW[0], end=WINDOW[1],
                  cursor_event_id="E1")
    assert _items_by_college(b1)["C-A"]["sample_size"] == 1
    assert b1["cursor_event_id"] == "E1"

    r = client.post(
        f"/api/plans/{pv}/quality/revisions/B2",
        json={"base_batch_id": "B1", "appeal_ids": []},
    )
    assert r.status_code == 201, r.text
    b2 = r.json()
    assert b2["revision_of"] == "B1"
    assert b2["rule_version"] == "v1"
    assert _items_by_college(b2)["C-A"]["sample_size"] == 2


# ---------------------------------------------------------------------------
# 分项证据与置信标记、排名
# ---------------------------------------------------------------------------


def test_metric_evidence_and_ranking(client):
    _setup_plan(client, duplicate_window_seconds=600)
    pv = "P1"
    _post_events(client, pv, [
        # C-A：一条迟到上报 + 一对重复/冲突签到。
        _checkin("A1", "C-A", "S1",
                 "2024-03-10T09:00:00+08:00", "2024-03-10T10:00:00+08:00",
                 reported="2024-03-10T20:00:00+08:00"),
        _checkin("A2", "C-A", "S1",
                 "2024-03-10T09:02:00+08:00", "2024-03-10T10:05:00+08:00",
                 reported="2024-03-10T20:00:00+08:00"),
        _checkin("A3", "C-A", "S2",
                 "2024-03-12T09:00:00+08:00", "2024-03-12T10:00:00+08:00",
                 reported="2024-03-20T09:00:00+08:00"),
        # C-B：全部干净及时。
        _checkin("B1", "C-B", "S9",
                 "2024-03-10T09:00:00+08:00", "2024-03-10T10:00:00+08:00",
                 reported="2024-03-10T12:00:00+08:00"),
        _checkin("B2", "C-B", "S9",
                 "2024-03-11T09:00:00+08:00", "2024-03-11T10:00:00+08:00",
                 reported="2024-03-11T12:00:00+08:00"),
    ])
    batch = _compute(client, pv, "B1", start=WINDOW[0], end=WINDOW[1])
    items = _items_by_college(batch)
    ca = items["C-A"]
    assert ca["metrics"]["duplicate"]["flagged_event_ids"] == ["A2"]
    assert ca["metrics"]["conflict"]["flagged_event_ids"] == ["A1", "A2"]
    assert ca["metrics"]["timeliness"]["flagged_event_ids"] == ["A3"]
    # 证据内保留样本事件与分项明细。
    assert set(ca["evidence"]["sample_event_ids"]) == {"A1", "A2", "A3"}
    dup_detail = ca["evidence"]["by_metric"]["duplicate"]["detail"]
    assert dup_detail[0]["duplicate_of"] == "A1"
    # C-B 排名高于 C-A。
    assert batch["ranking"][0]["college_id"] == "C-B"
    assert batch["ranking"][1]["college_id"] == "C-A"


# ---------------------------------------------------------------------------
# 并发签发
# ---------------------------------------------------------------------------


def test_concurrent_signing_only_one_wins(client):
    _setup_plan(client)
    pv = "P1"
    _post_events(client, pv, [
        _checkin("E1", "C-A", "S1",
                 "2024-03-10T09:00:00+08:00", "2024-03-10T10:00:00+08:00")
    ])
    _compute(client, pv, "B1", start=WINDOW[0], end=WINDOW[1])

    outcomes: list[str] = []
    signatures: list[str | None] = []
    lock = threading.Lock()

    def _sign():
        session = TestSessionLocal()
        try:
            result = service.sign_batch(session, plan_version=pv, batch_id="B1")
            with lock:
                outcomes.append(result["outcome"])
                signatures.append(result["signature"])
        finally:
            session.close()

    threads = [threading.Thread(target=_sign) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes).count("signed") == 1
    assert sorted(outcomes).count("already_signed") == 5
    assert len(set(signatures)) == 1  # 所有观察者看到同一签名

    stored = client.get(f"/api/plans/{pv}/quality/batches/B1").json()
    assert stored["state"] == "signed"
    assert stored["signature"] == signatures[0]

    # 签发后证据与排名冻结：迟到数据不改变已签发内容。
    _post_events(client, pv, [
        _checkin("E9", "C-A", "S9",
                 "2024-03-15T09:00:00+08:00", "2024-03-15T10:00:00+08:00")
    ])
    stored_again = client.get(f"/api/plans/{pv}/quality/batches/B1").json()
    assert stored_again["signature"] == stored["signature"]
    assert _items_by_college(stored_again)["C-A"]["sample_size"] == 1


# ---------------------------------------------------------------------------
# 空样本
# ---------------------------------------------------------------------------


def test_empty_sample_college_gets_flags_and_zero_scores(client):
    _setup_plan(client, min_sample_size=5)
    pv = "P1"
    _post_events(client, pv, [
        _checkin("E1", "C-A", "S1",
                 "2024-03-10T09:00:00+08:00", "2024-03-10T10:00:00+08:00")
    ])
    batch = _compute(
        client, pv, "B1", start=WINDOW[0], end=WINDOW[1],
        colleges=["C-A", "C-EMPTY"],
    )
    items = _items_by_college(batch)
    empty = items["C-EMPTY"]
    assert empty["sample_size"] == 0
    assert empty["composite_score"] == 0.0
    assert empty["confidence_flags"]["overall"] == "empty_sample"
    for name in ("timeliness", "duplicate", "conflict"):
        assert empty["metrics"][name]["denominator"] == 0
        assert empty["confidence_flags"][name] == "empty_sample"
    small = items["C-A"]
    assert small["confidence_flags"]["overall"] == "small_sample"
    # 空样本学院同样进入排名，保证口径稳定。
    colleges_in_ranking = {row["college_id"] for row in batch["ranking"]}
    assert colleges_in_ranking == {"C-A", "C-EMPTY"}


def test_entirely_empty_plan_batch(client):
    _setup_plan(client)
    pv = "P1"
    batch = _compute(client, pv, "B1", start=WINDOW[0], end=WINDOW[1])
    assert batch["items"] == []
    assert batch["ranking"] == []
    assert batch["cursor_event_id"] is None
    assert batch["manifest"]["input_event_count"] == 0
    # 空批次也可签发。
    sign = client.post(f"/api/plans/{pv}/quality/batches/B1/sign").json()
    assert sign["outcome"] == "signed"


# ---------------------------------------------------------------------------
# 重启恢复
# ---------------------------------------------------------------------------


class _CrashAfterColleges(RuntimeError):
    pass


def test_checkpoint_recovery_after_crash(client):
    _setup_plan(client)
    pv = "P1"
    _post_events(client, pv, [
        _checkin("A1", "C-A", "S1",
                 "2024-03-10T09:00:00+08:00", "2024-03-10T10:00:00+08:00"),
        _checkin("B1", "C-B", "S2",
                 "2024-03-11T09:00:00+08:00", "2024-03-11T10:00:00+08:00"),
        _checkin("C1", "C-C", "S3",
                 "2024-03-12T09:00:00+08:00", "2024-03-12T10:00:00+08:00"),
    ])

    crash_session = TestSessionLocal()
    completed: list[str] = []

    def hook(college_id: str) -> None:
        completed.append(college_id)
        if len(completed) == 1:
            raise _CrashAfterColleges("simulated process crash")

    with pytest.raises(_CrashAfterColleges):
        service.compute_batch(
            crash_session,
            plan_version=pv,
            batch_id="B1",
            window_start=datetime.fromisoformat(WINDOW[0]),
            window_end=datetime.fromisoformat(WINDOW[1]),
            rule_version="v1",
            checkpoint_hook=hook,
        )
    crash_session.close()

    # 崩溃后：批次停留在 pending，已完成学院的检查点已落库（各自独立事务）。
    ro = TestSessionLocal()
    pending = service.get_batch(ro, plan_version=pv, batch_id="B1")
    assert pending["state"] == "pending"
    checkpointed = {item.college_id for item in
                    quality.repo.list_items(ro, pv, "B1")}
    assert checkpointed == {"C-A"}
    ro.close()

    # “重启”后重入：沿用固定窗口/游标/规则，跳过已完成学院，补齐其余学院。
    resumed = _compute(client, pv, "B1", start=WINDOW[0], end=WINDOW[1])
    assert resumed["outcome"] == "recovered"
    assert resumed["state"] == "computed"
    assert {i["college_id"] for i in resumed["items"]} == {"C-A", "C-B", "C-C"}

    # 与一次性计算的对照批次完全一致（确定性重放）。
    clean = _compute(client, pv, "B-REF", start=WINDOW[0], end=WINDOW[1])
    resumed_items = {i["college_id"]: i for i in resumed["items"]}
    for item in clean["items"]:
        ref = resumed_items[item["college_id"]]
        assert ref["metrics"] == item["metrics"]
        assert ref["composite_score"] == item["composite_score"]
        assert ref["confidence_flags"] == item["confidence_flags"]


def test_repeated_crashes_each_call_resumes_from_checkpoints(client):
    _setup_plan(client)
    pv = "P1"
    _post_events(client, pv, [
        _checkin("A1", "C-A", "S1",
                 "2024-03-10T09:00:00+08:00", "2024-03-10T10:00:00+08:00"),
        _checkin("B1", "C-B", "S2",
                 "2024-03-11T09:00:00+08:00", "2024-03-11T10:00:00+08:00"),
        _checkin("C1", "C-C", "S3",
                 "2024-03-12T09:00:00+08:00", "2024-03-12T10:00:00+08:00"),
        _checkin("D1", "C-D", "S4",
                 "2024-03-13T09:00:00+08:00", "2024-03-13T10:00:00+08:00"),
    ])

    def _attempt(crash_count: int) -> list[str]:
        session = TestSessionLocal()
        done: list[str] = []

        def hook(college_id: str) -> None:
            done.append(college_id)
            if len(done) == crash_count:
                raise _CrashAfterColleges("simulated crash")

        try:
            service.compute_batch(
                session,
                plan_version=pv,
                batch_id="B1",
                window_start=datetime.fromisoformat(WINDOW[0]),
                window_end=datetime.fromisoformat(WINDOW[1]),
                rule_version="v1",
                checkpoint_hook=hook,
            )
            return []
        finally:
            session.close()

    # 第一次崩溃在 C-A 后，第二次崩溃在（新算的）C-B 后；检查点逐次累积。
    with pytest.raises(_CrashAfterColleges):
        _attempt(crash_count=1)
    with pytest.raises(_CrashAfterColleges):
        _attempt(crash_count=1)

    ro = TestSessionLocal()
    assert {i.college_id for i in quality.repo.list_items(ro, pv, "B1")} == {
        "C-A", "C-B"
    }
    ro.close()

    # 第三次不再崩溃：只补 C-C、C-D，批次完成。
    final = _compute(client, pv, "B1", start=WINDOW[0], end=WINDOW[1])
    assert final["outcome"] == "recovered"
    assert {i["college_id"] for i in final["items"]} == {
        "C-A", "C-B", "C-C", "C-D"
    }
    assert final["state"] == "computed"


def test_recompute_completed_batch_is_idempotent(client):
    _setup_plan(client)
    pv = "P1"
    _post_events(client, pv, [
        _checkin("E1", "C-A", "S1",
                 "2024-03-10T09:00:00+08:00", "2024-03-10T10:00:00+08:00")
    ])
    first = _compute(client, pv, "B1", start=WINDOW[0], end=WINDOW[1])
    second = _compute(client, pv, "B1", start=WINDOW[0], end=WINDOW[1])
    assert second["outcome"] == "existing"
    assert second["manifest"]["input_fingerprint"] == first["manifest"]["input_fingerprint"]
    assert second["items"] == first["items"]


# ---------------------------------------------------------------------------
# 迟到数据修订版 + 口径不变
# ---------------------------------------------------------------------------


def test_late_data_creates_revision_without_rewriting_ranking(client):
    _setup_plan(client, duplicate_window_seconds=600)
    pv = "P1"
    _post_events(client, pv, [
        _checkin("A1", "C-A", "S1",
                 "2024-03-10T09:00:00+08:00", "2024-03-10T10:00:00+08:00",
                 reported="2024-03-10T20:00:00+08:00"),
        _checkin("A2", "C-A", "S1",
                 "2024-03-10T09:02:00+08:00", "2024-03-10T10:05:00+08:00",
                 reported="2024-03-10T20:00:00+08:00"),
        _checkin("B1", "C-B", "S9",
                 "2024-03-10T09:00:00+08:00", "2024-03-10T10:00:00+08:00",
                 reported="2024-03-10T12:00:00+08:00"),
    ])
    b1 = _compute(client, pv, "B1", start=WINDOW[0], end=WINDOW[1])
    client.post(f"/api/plans/{pv}/quality/batches/B1/sign")
    ranking_v1 = b1["ranking"]
    assert ranking_v1[0]["college_id"] == "C-B"

    # 迟到数据让 C-A 也变得干净及时；只形成修订版 B2。
    _post_events(client, pv, [
        _checkin("A9", "C-A", "S8",
                 "2024-03-20T09:00:00+08:00", "2024-03-20T10:00:00+08:00",
                 reported="2024-03-20T11:00:00+08:00")
    ])
    r = client.post(
        f"/api/plans/{pv}/quality/revisions/B2",
        json={"base_batch_id": "B1", "appeal_ids": []},
    )
    assert r.status_code == 201, r.text
    b2 = r.json()
    assert b2["revision_of"] == "B1"
    assert b2["rule_snapshot"] == b1["rule_snapshot"]
    assert b2["cursor_event_id"] == "A9"
    assert b1["window_start"] == b2["window_start"]
    assert b1["window_end"] == b2["window_end"]

    stored_b1 = client.get(f"/api/plans/{pv}/quality/batches/B1").json()
    assert stored_b1["state"] == "superseded"
    assert stored_b1["superseded_by"] == "B2"
    assert stored_b1["ranking"] == ranking_v1  # 过去排名原样保留
    assert stored_b1["signature"] is not None


def test_new_rule_version_cannot_rewrite_existing_batch(client):
    _setup_plan(client)
    pv = "P1"
    _post_events(client, pv, [
        _checkin("E1", "C-A", "S1",
                 "2024-03-10T09:00:00+08:00", "2024-03-10T10:00:00+08:00")
    ])
    _compute(client, pv, "B1", start=WINDOW[0], end=WINDOW[1])

    # 发布新口径 v2（48 小时）。
    r = client.post(
        f"/api/plans/{pv}/quality/rules",
        json={"rule_version": "v2", "timezone": "Asia/Shanghai", "deadline_hours": 48},
    )
    assert r.status_code == 201
    assert client.post(f"/api/plans/{pv}/quality/rules/v2/publish").status_code == 200

    # 同批次 ID 重算不会改变它引用的规则版本。
    again = _compute(client, pv, "B1", start=WINDOW[0], end=WINDOW[1], rule="v2")
    assert again["rule_version"] == "v1"

    # 草稿口径不能用于计算。
    r = client.post(
        f"/api/plans/{pv}/quality/rules",
        json={"rule_version": "v3", "timezone": "Asia/Shanghai"},
    )
    r = client.post(
        f"/api/plans/{pv}/quality/batches/B3/compute",
        json={"window_start": WINDOW[0], "window_end": WINDOW[1], "rule_version": "v3"},
    )
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# 申诉：只调整经核实的来源
# ---------------------------------------------------------------------------


def _appeal(client, pv, *, appeal_id, batch_id, college, metric, event_ids):
    r = client.post(
        f"/api/plans/{pv}/quality/appeals",
        json={
            "appeal_id": appeal_id,
            "batch_id": batch_id,
            "college_id": college,
            "metric": metric,
            "reason": "source data is wrong",
            "event_ids": event_ids,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_appeal_only_verified_sources_change_revision(client):
    _setup_plan(client, duplicate_window_seconds=600)
    pv = "P1"
    _post_events(client, pv, [
        _checkin("A1", "C-A", "S1",
                 "2024-03-10T09:00:00+08:00", "2024-03-10T10:00:00+08:00",
                 reported="2024-03-10T20:00:00+08:00"),
        _checkin("A2", "C-A", "S1",
                 "2024-03-10T09:02:00+08:00", "2024-03-10T10:05:00+08:00",
                 reported="2024-03-10T20:00:00+08:00"),
    ])
    b1 = _compute(client, pv, "B1", start=WINDOW[0], end=WINDOW[1])
    before = _items_by_college(b1)["C-A"]
    assert before["metrics"]["duplicate"]["score"] == 0.5

    _appeal(client, pv, appeal_id="AP1", batch_id="B1", college="C-A",
            metric="duplicate", event_ids=["A2", "GHOST"])
    r = client.post(
        f"/api/plans/{pv}/quality/appeals/AP1/resolve",
        json={
            "decision": "verified",
            "reviewer_id": "dean",
            "note": "A2 confirmed double-tap; GHOST not found",
            "verified_event_ids": ["A2", "GHOST"],
        },
    )
    assert r.status_code == 200, r.text
    decision = r.json()
    assert decision["state"] == "verified"
    assert decision["verified_event_ids"] == ["A2"]
    assert decision["rejected_event_ids"] == ["GHOST"]

    r = client.post(
        f"/api/plans/{pv}/quality/revisions/B2",
        json={"base_batch_id": "B1", "appeal_ids": ["AP1"]},
    )
    assert r.status_code == 201, r.text
    after = _items_by_college(r.json())["C-A"]
    assert after["sample_size"] == 1
    assert after["metrics"]["duplicate"]["score"] == 1.0
    assert after["evidence"]["sample_event_ids"] == ["A1"]

    linked = client.get(f"/api/plans/{pv}/quality/batches/B2/appeals").json()
    assert linked[0]["revision_batch_id"] == "B2"


def test_rejected_and_cross_college_sources_are_not_excluded(client):
    _setup_plan(client)
    pv = "P1"
    _post_events(client, pv, [
        _checkin("A1", "C-A", "S1",
                 "2024-03-10T09:00:00+08:00", "2024-03-10T10:00:00+08:00",
                 reported="2024-03-20T09:00:00+08:00"),
        _checkin("B1", "C-B", "S2",
                 "2024-03-10T09:00:00+08:00", "2024-03-10T10:00:00+08:00",
                 reported="2024-03-20T09:00:00+08:00"),
    ])
    _compute(client, pv, "B1", start=WINDOW[0], end=WINDOW[1])

    # 引用了别学院的事件：核实不通过来源归属检查。
    _appeal(client, pv, appeal_id="AP1", batch_id="B1", college="C-A",
            metric="timeliness", event_ids=["B1"])
    r = client.post(
        f"/api/plans/{pv}/quality/appeals/AP1/resolve",
        json={
            "decision": "verified",
            "reviewer_id": "dean",
            "note": "attempt",
            "verified_event_ids": ["B1"],
        },
    )
    assert r.status_code == 400

    # 明确驳回的申诉不能驱动修订。
    _appeal(client, pv, appeal_id="AP2", batch_id="B1", college="C-A",
            metric="timeliness", event_ids=["A1"])
    r = client.post(
        f"/api/plans/{pv}/quality/appeals/AP2/resolve",
        json={
            "decision": "rejected",
            "reviewer_id": "dean",
            "note": "SLA was indeed missed",
            "verified_event_ids": ["A1"],
        },
    )
    assert r.status_code == 200
    r = client.post(
        f"/api/plans/{pv}/quality/revisions/B2",
        json={"base_batch_id": "B1", "appeal_ids": ["AP2"]},
    )
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# 趋势查询
# ---------------------------------------------------------------------------


def test_trends_default_only_signed_lineage_and_filters(client):
    _setup_plan(client)
    pv = "P1"
    _post_events(client, pv, [
        _checkin("A1", "C-A", "S1",
                 "2024-03-10T09:00:00+08:00", "2024-03-10T10:00:00+08:00"),
    ])
    _compute(client, pv, "B1", start=WINDOW[0], end=WINDOW[1])
    # 未签发批次默认不出现在趋势中。
    assert client.get(f"/api/plans/{pv}/quality/trends").json()["batches"] == []

    client.post(f"/api/plans/{pv}/quality/batches/B1/sign")
    _post_events(client, pv, [
        _checkin("A2", "C-A", "S2",
                 "2024-03-20T09:00:00+08:00", "2024-03-20T10:00:00+08:00"),
    ])
    client.post(
        f"/api/plans/{pv}/quality/revisions/B2",
        json={"base_batch_id": "B1", "appeal_ids": []},
    )
    # B2 未签发：趋势仍只有 B1（superseded 属已签发血统）。
    signed_only = client.get(f"/api/plans/{pv}/quality/trends").json()
    assert [b["batch_id"] for b in signed_only["batches"]] == ["B1"]
    assert signed_only["batches"][0]["state"] == "superseded"

    # include_unsigned 可看到 B2。
    with_un = client.get(
        f"/api/plans/{pv}/quality/trends?include_unsigned=true"
    ).json()
    assert [b["batch_id"] for b in with_un["batches"]] == ["B1", "B2"]

    # 学院过滤。
    ca = client.get(
        f"/api/plans/{pv}/quality/trends?college_id=C-A&include_unsigned=true"
    ).json()
    assert all(
        {p["college_id"] for p in b["points"]} == {"C-A"} for b in ca["batches"]
    )

    # 规则版本过滤：v2 批次只在 rule_version=v2 下出现。
    client.post(
        f"/api/plans/{pv}/quality/rules",
        json={"rule_version": "v2", "timezone": "Asia/Shanghai"},
    )
    client.post(f"/api/plans/{pv}/quality/rules/v2/publish")
    _compute(
        client, pv, "B3",
        start="2024-04-01T00:00:00+08:00", end="2024-05-01T00:00:00+08:00",
        rule="v2",
    )
    v2_trend = client.get(
        f"/api/plans/{pv}/quality/trends?rule_version=v2&include_unsigned=true"
    ).json()
    assert [b["batch_id"] for b in v2_trend["batches"]] == ["B3"]
