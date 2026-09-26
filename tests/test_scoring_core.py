"""学院数据质量评分：核心领域纯函数测试。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.scoring.engine import (
    apply_source_corrections,
    calculate_batch,
    rank_colleges,
    recompute_metric,
    scorecard_fingerprint,
    window_bounds,
    window_for_span,
)
from app.scoring.metrics import (
    CONFLICT_RATE,
    DUPLICATE_RATE,
    TIMELINESS,
    Incident,
    build_default_specs,
    derive_fingerprint,
)

SPECS = build_default_specs("rv-test")
UTC = timezone.utc


def _inc(
    rid,
    college,
    *,
    category="safety",
    occurred=None,
    reported=None,
    fingerprint=None,
):
    occurred = occurred or datetime(2024, 3, 15, 8, 0, tzinfo=UTC)
    reported = reported if reported is not None else occurred + timedelta(hours=5)
    return Incident(
        report_id=rid,
        college_id=college,
        category=category,
        occurred_at=occurred,
        reported_at=reported,
        fingerprint=fingerprint or f"fp-{rid}",
    )


def test_timeliness_on_time_and_late_split():
    base = datetime(2024, 3, 15, 0, 0, tzinfo=UTC)
    incidents = [
        _inc("R1", "C1", occurred=base, reported=base + timedelta(hours=20)),
        _inc("R2", "C1", occurred=base, reported=base + timedelta(hours=24)),
        _inc("R3", "C1", occurred=base, reported=base + timedelta(hours=30)),
    ]
    from app.scoring.metrics import timeliness

    value, evidence = timeliness(incidents, SPECS[TIMELINESS])
    # 24h 边界内（含）两条，超时一条。
    assert value == pytest.approx(2 / 3)
    assert evidence["on_time"] == 2
    assert evidence["late"] == ["R3"]
    assert evidence["lag_seconds"]["R3"] == 30 * 3600


def test_duplicate_rate_counts_repeats_beyond_first():
    incidents = [
        _inc("R1", "C1", category="safety", fingerprint="F"),
        _inc("R2", "C1", category="safety", fingerprint="F"),
        _inc("R3", "C1", category="safety", fingerprint="F"),
        _inc("R4", "C1", category="safety", fingerprint="OTHER"),
    ]
    from app.scoring.metrics import duplicate_rate

    value, evidence = duplicate_rate(incidents, SPECS[DUPLICATE_RATE])
    assert value == pytest.approx(2 / 4)
    assert evidence["groups"][0]["report_ids"] == ["R1", "R2", "R3"]


def test_conflict_rate_same_fingerprint_different_categories():
    incidents = [
        _inc("R1", "C1", category="safety", fingerprint="F"),
        _inc("R2", "C1", category="facility", fingerprint="F"),
        _inc("R3", "C1", category="safety", fingerprint="SOLO"),
    ]
    from app.scoring.metrics import conflict_rate

    value, evidence = conflict_rate(incidents, SPECS[CONFLICT_RATE])
    # 同指纹两个类别 -> 组内 2 条全部计冲突。
    assert value == pytest.approx(2 / 3)
    assert evidence["groups"][0]["categories"] == ["facility", "safety"]


def test_derive_fingerprint_is_deterministic_and_category_sensitive():
    fp1 = derive_fingerprint("safety", {"a": 1, "b": 2})
    fp2 = derive_fingerprint("safety", {"b": 2, "a": 1})  # 键序不同
    fp3 = derive_fingerprint("facility", {"a": 1, "b": 2})
    assert fp1 == fp2
    assert fp1 != fp3


def test_empty_sample_yields_no_score_and_low_confidence():
    window = window_for_span(date(2024, 3, 15), 7, "Asia/Shanghai")
    batch = calculate_batch(
        batch_id="B-empty",
        incidents=[],
        window=window,
        specs=SPECS,
    )
    assert batch.colleges == {}
    # 窗口内有 1 条记录（低于 min_sample=5）-> 可评分但低置信。
    batch2 = calculate_batch(
        batch_id="B-tiny",
        incidents=[_inc("R1", "C1")],
        window=window,
        specs=SPECS,
    )
    college = batch2.colleges["C1"]
    assert college.composite is not None
    assert college.confident is False
    assert college.metrics[TIMELINESS].sample_size == 1


def test_composite_weights_and_ranking_are_deterministic():
    window = window_for_span(date(2024, 3, 15), 7, "UTC")
    # 学院 A：全部及时、无重复无冲突 -> 满分。
    incidents_a = [
        _inc(f"A{i}", "C-A", fingerprint=f"fa{i}",
             occurred=datetime(2024, 3, 15, 8, tzinfo=UTC))
        for i in range(6)
    ]
    # 学院 B：含重复 -> 重复率指标拉低。
    incidents_b = [
        _inc(f"B{i}", "C-B", fingerprint="fb") for i in range(6)
    ]
    batch = calculate_batch(
        batch_id="B", incidents=incidents_a + incidents_b,
        window=window, specs=SPECS,
    )
    assert batch.colleges["C-A"].composite == pytest.approx(100.0)
    assert batch.colleges["C-B"].composite < 100.0

    rankings = rank_colleges(batch)
    assert [r["college_id"] for r in rankings] == ["C-A", "C-B"]
    assert [r["rank"] for r in rankings] == [1, 2]
    # 重跑确定性：指纹一致。
    fp1 = scorecard_fingerprint(
        batch_id="B", rule_version=batch.rule_version,
        input_cursor=None, window=window, rankings=rankings,
    )
    fp2 = scorecard_fingerprint(
        batch_id="B", rule_version=batch.rule_version,
        input_cursor=None, window=window, rankings=rank_colleges(batch),
    )
    assert fp1 == fp2


def test_tie_broken_by_college_id():
    window = window_for_span(date(2024, 3, 15), 7, "UTC")
    identical = []
    for college in ("C-Z", "C-A", "C-M"):
        for i in range(6):
            identical.append(
                _inc(f"{college}-{i}", college, fingerprint=f"{college}-{i}")
            )
    batch = calculate_batch(
        batch_id="B", incidents=identical, window=window, specs=SPECS
    )
    rankings = rank_colleges(batch)
    assert [r["college_id"] for r in rankings] == ["C-A", "C-M", "C-Z"]
    assert all(r["composite_score"] == 100.0 for r in rankings)


def test_input_cursor_excludes_late_reports():
    window = window_for_span(date(2024, 3, 15), 30, "UTC")
    incidents = [
        _inc("R-001", "C1", fingerprint="F1"),
        _inc("R-002", "C1", category="facility", fingerprint="F1"),  # 冲突
    ]
    fixed = calculate_batch(
        batch_id="B", incidents=incidents, window=window, specs=SPECS,
        input_cursor="R-001",
    )
    # 游标停在 R-001：R-002 尚未进入，冲突率为 0。
    assert fixed.colleges["C1"].metrics[CONFLICT_RATE].value == 0.0
    full = calculate_batch(
        batch_id="B2", incidents=incidents, window=window, specs=SPECS,
        input_cursor="R-002",
    )
    assert full.colleges["C1"].metrics[CONFLICT_RATE].value == 1.0
    assert fixed.input_cursor == "R-001"


def test_window_anchored_to_local_midnight_across_timezones():
    # 上海 2024-03-15 全天 = UTC 03-14 16:00 至 03-15 16:00。
    sh = window_bounds(date(2024, 3, 15), date(2024, 3, 16), "Asia/Shanghai")
    assert sh.start == datetime(2024, 3, 14, 16, 0, tzinfo=UTC)
    assert sh.end == datetime(2024, 3, 15, 16, 0, tzinfo=UTC)
    # 纽约 DST 回退当夜（2024-11-03）本地一天跨 25 个真实小时。
    ny = window_bounds(date(2024, 11, 3), date(2024, 11, 4), "America/New_York")
    assert (ny.end - ny.start) == timedelta(hours=25)
    # 回退重复小时内的事件（01:30 EST = 06:30Z）仍落在窗口内。
    during_fallback = datetime(2024, 11, 3, 6, 30, tzinfo=UTC)
    assert ny.contains(during_fallback)


def test_window_half_open_boundaries():
    window = window_for_span(date(2024, 3, 15), 1, "UTC")
    assert window.contains(datetime(2024, 3, 15, 0, 0, tzinfo=UTC))
    assert not window.contains(datetime(2024, 3, 16, 0, 0, tzinfo=UTC))


def test_recompute_metric_only_touches_verified_sources():
    incidents = [
        _inc("R1", "C1", category="safety", fingerprint="F"),
        _inc("R2", "C1", category="safety", fingerprint="F"),
        _inc("R3", "C1", category="facility", fingerprint="F"),
        _inc("R4", "C1", category="safety", fingerprint="G"),
    ]
    # 核实后剔除误报 R3（解除冲突），并把 R2 重分类（重复组仍在）。
    result = recompute_metric(
        incidents=incidents, metric_code=CONFLICT_RATE,
        spec=SPECS[CONFLICT_RATE],
        excluded_report_ids=["R3"],
    )
    assert result.value == 0.0
    corrected = apply_source_corrections(incidents, recategorized={"R2": "facility"})
    r2 = next(i for i in corrected if i.report_id == "R2")
    assert r2.category == "facility"
    # 未在修正名单内的记录原样保留。
    assert len(corrected) == len(incidents)


def test_revision_batch_links_parent_and_keeps_fixture():
    window = window_for_span(date(2024, 3, 15), 30, "UTC")
    incidents = [_inc("R1", "C1", category="safety", fingerprint="F"),
                _inc("R2", "C1", category="facility", fingerprint="F")]
    parent = calculate_batch(
        batch_id="B1", incidents=incidents, window=window, specs=SPECS,
        input_cursor="R2",
    )
    corrected = apply_source_corrections(incidents, excluded_report_ids=["R2"])
    revision = calculate_batch(
        batch_id="B2", incidents=corrected, window=window, specs=SPECS,
        input_cursor="R2", revision_of="B1",
    )
    assert revision.revision_of == "B1"
    assert revision.window == parent.window
    assert revision.rule_version == parent.rule_version
    assert revision.input_cursor == parent.input_cursor
    # 初版冲突率 1.0，修订版剔除来源后 0.0。
    assert parent.colleges["C1"].metrics[CONFLICT_RATE].value == 1.0
    assert revision.colleges["C1"].metrics[CONFLICT_RATE].value == 0.0
