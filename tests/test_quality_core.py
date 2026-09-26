"""质量指标纯函数测试：跨时区/DST 窗口、迟到、重复、冲突与置信标记。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.quality.metrics import (
    RuleDefinition,
    ScoredEvent,
    local_window,
    score_college,
    score_conflict,
    score_duplicate,
    score_timeliness,
    score_window,
)

SH = "Asia/Shanghai"
NY = "America/New_York"


def _ev(
    eid,
    *,
    college="C1",
    student="S1",
    occurred,
    reported=None,
    event_type="checkin",
    activity="A1",
    check_in=None,
    check_out=None,
):
    occurred = datetime.fromisoformat(occurred)
    reported = datetime.fromisoformat(reported) if reported else occurred
    return ScoredEvent(
        event_id=eid,
        college_id=college,
        student_id=student,
        event_type=event_type,
        occurred_at=occurred,
        reported_at=reported,
        activity_id=activity,
        check_in_at=datetime.fromisoformat(check_in) if check_in else None,
        check_out_at=datetime.fromisoformat(check_out) if check_out else None,
    )


def _rule(tz=SH, **kw):
    defaults = dict(
        rule_version="r1",
        timezone=tz,
        deadline_hours=24,
        grace_seconds=0,
        duplicate_window_seconds=300,
        overlap_tolerance_seconds=0,
        min_sample_size=3,
    )
    defaults.update(kw)
    return RuleDefinition(**defaults)


def test_timeliness_deadline_is_end_of_occurrence_local_day_plus_hours():
    # 事件发生在 3/10 09:00 (+08:00)；截止 = 3/12 00:00 +08:00（本地次日午夜 + 24h）。
    on_time = _ev("E1", occurred="2024-03-10T09:00:00+08:00",
                  reported="2024-03-11T23:59:00+08:00")
    late = _ev("E2", occurred="2024-03-10T09:00:00+08:00",
               reported="2024-03-12T00:01:00+08:00")
    result = score_timeliness([on_time, late], _rule())
    assert result.denominator == 2
    assert result.numerator == 1
    assert result.score == 0.5
    assert result.flagged_event_ids == ("E2",)
    detail = result.detail[0]
    assert detail["deadline_utc"] == "2024-03-11T16:00:00Z"
    assert detail["lateness_seconds"] == 60


def test_timeliness_grace_absorbs_small_delay():
    ev = _ev("E1", occurred="2024-03-10T09:00:00+08:00",
             reported="2024-03-12T00:00:30+08:00")
    assert score_timeliness([ev], _rule(grace_seconds=60)).score == 1.0
    assert score_timeliness([ev], _rule(grace_seconds=0)).score == 0.0


def test_timeliness_new_york_spring_forward_day():
    # 2024-03-10 是纽约春令时切换日（02:00 -> 03:00）。
    # 事件 20:00 EDT (UTC-4)；次日午夜已处 EDT，午夜 = 04:00Z，截止 +24h。
    ev = _ev(
        "E1",
        college=NY,
        occurred="2024-03-10T20:00:00-04:00",
        reported="2024-03-12T04:00:00+00:00",
    )
    result = score_timeliness([ev], _rule(NY))
    assert result.numerator == 1
    assert result.detail == ()

    one_second_late = _ev(
        "E2",
        college=NY,
        occurred="2024-03-10T20:00:00-04:00",
        reported="2024-03-12T04:00:01+00:00",
    )
    late_result = score_timeliness([one_second_late], _rule(NY))
    assert late_result.numerator == 0
    assert late_result.detail[0]["deadline_utc"] == "2024-03-12T04:00:00Z"


def test_timeliness_new_york_fallback_day():
    # 2024-11-03 秋令时回退（02:00 重演，日长 25 小时）。
    # 事件在 EDT 段 01:30(-04)；11/4 午夜处 EST = 05:00Z，截止再 +24h = 11/5 05:00Z。
    ev = _ev(
        "E1",
        college=NY,
        occurred="2024-11-03T01:30:00-04:00",
        reported="2024-11-05T05:00:00+00:00",
    )
    result = score_timeliness([ev], _rule(NY))
    assert result.numerator == 1
    assert result.detail == ()
    late = _ev(
        "E2",
        college=NY,
        occurred="2024-11-03T01:30:00-04:00",
        reported="2024-11-05T05:00:01+00:00",
    )
    assert score_timeliness([late], _rule(NY)).detail[0][
        "deadline_utc"
    ] == "2024-11-05T05:00:00Z"


def test_window_interpreted_in_rule_timezone():
    # 上海 4/1 01:00（= 3/31 17:00Z）落在 3 月本地窗口之外。
    w = local_window(
        datetime.fromisoformat("2024-03-01T00:00:00+08:00"),
        datetime.fromisoformat("2024-04-01T00:00:00+08:00"),
        SH,
    )
    assert w.start_utc == datetime(2024, 2, 29, 16, tzinfo=timezone.utc)
    assert w.end_utc == datetime(2024, 3, 31, 16, tzinfo=timezone.utc)
    inside = _ev("E1", occurred="2024-03-31T23:59:00+08:00")
    outside = _ev("E2", occurred="2024-03-31T17:30:00+00:00")
    scores = score_window([inside, outside], w, _rule())
    [college] = scores.values()
    assert college.sample_size == 1
    assert college.evidence["sample_event_ids"] == ["E1"]


def test_window_naive_bounds_localized_to_rule_zone():
    w = local_window(
        datetime.fromisoformat("2024-03-01T00:00:00"),
        datetime.fromisoformat("2024-04-01T00:00:00"),
        NY,
    )
    # 纽约 3/1 午夜处 EST（UTC-5）。
    assert w.start_utc == datetime(2024, 3, 1, 5, tzinfo=timezone.utc)


def test_duplicate_clustering_flags_only_later_copies():
    events = [
        _ev("E1", student="S1", activity="A1",
             occurred="2024-03-10T09:00:00+08:00"),
        _ev("E2", student="S1", activity="A1",
             occurred="2024-03-10T09:02:00+08:00"),
        _ev("E3", student="S1", activity="A1",
             occurred="2024-03-10T09:30:00+08:00"),
        _ev("E4", student="S2", activity="A1",
             occurred="2024-03-10T09:01:00+08:00"),
    ]
    result = score_duplicate(events, _rule(duplicate_window_seconds=300))
    assert result.numerator == 3
    assert set(result.flagged_event_ids) == {"E2"}
    assert result.detail[0]["duplicate_of"] == "E1"
    assert result.detail[0]["gap_seconds"] == 120


def test_conflict_pairwise_overlapping_intervals():
    events = [
        _ev("E1", student="S1",
             check_in="2024-03-10T09:00:00+08:00",
             check_out="2024-03-10T10:00:00+08:00",
             occurred="2024-03-10T09:00:00+08:00"),
        _ev("E2", student="S1",
             check_in="2024-03-10T09:30:00+08:00",
             check_out="2024-03-10T10:30:00+08:00",
             occurred="2024-03-10T09:30:00+08:00"),
        _ev("E3", student="S2",  # 不同学员重叠不算冲突
             check_in="2024-03-10T09:30:00+08:00",
             check_out="2024-03-10T10:30:00+08:00",
             occurred="2024-03-10T09:30:00+08:00"),
        _ev("E4", student="S1",  # 与 E2 恰好相接，容忍 0 秒时不冲突
             check_in="2024-03-10T10:30:00+08:00",
             check_out="2024-03-10T11:00:00+08:00",
             occurred="2024-03-10T10:30:00+08:00"),
    ]
    result = score_conflict(events, _rule())
    assert set(result.flagged_event_ids) == {"E1", "E2"}
    assert result.numerator == 2

    tolerant = score_conflict(events, _rule(overlap_tolerance_seconds=1800))
    assert tolerant.flagged_event_ids == ()


def test_empty_sample_flags_empty_and_scores_zero():
    result = score_college("C1", [], _rule())
    assert result.sample_size == 0
    assert result.composite_score == 0.0
    assert result.confidence_flags["overall"] == "empty_sample"
    for name in ("timeliness", "duplicate", "conflict"):
        assert result.metrics[name].score == 0.0
        assert result.metrics[name].denominator == 0
        assert result.confidence_flags[name] == "empty_sample"
    assert result.evidence["sample_event_ids"] == []


def test_small_sample_flag_and_per_metric_confidence():
    events = [
        _ev("E1", occurred="2024-03-10T09:00:00+08:00",
            reported="2024-03-10T10:00:00+08:00"),
        _ev("E2", occurred="2024-03-11T09:00:00+08:00",
            reported="2024-03-11T10:00:00+08:00"),
    ]
    result = score_college("C1", events, _rule(min_sample_size=5))
    assert result.confidence_flags["overall"] == "small_sample"
    assert result.confidence_flags["timeliness"] == "small_sample"


def test_composite_uses_weights():
    events = [
        _ev("E1", student="S1",
             occurred="2024-03-10T09:00:00+08:00",
             reported="2024-03-20T09:00:00+08:00"),
    ]
    rule = _rule(
        weights={"timeliness": 1.0, "duplicate": 0.0, "conflict": 0.0},
        min_sample_size=1,
    )
    result = score_college("C1", events, rule)
    # 迟到 -> timeliness 0；权重全部压在及时性上 -> 综合分 0。
    assert result.composite_score == 0.0
    assert result.metrics["duplicate"].score == 1.0
    assert result.metrics["conflict"].score == 1.0


def test_rule_rejects_unknown_timezone():
    with pytest.raises(Exception):
        _rule(tz="Mars/Olympus").validate()


def test_deterministic_identical_inputs_identical_outputs():
    events = [
        _ev("E1", student="S1", occurred="2024-03-10T09:00:00+08:00"),
        _ev("E2", student="S1", occurred="2024-03-10T09:01:00+08:00"),
    ]
    w = local_window(
        datetime.fromisoformat("2024-03-01T00:00:00+08:00"),
        datetime.fromisoformat("2024-04-01T00:00:00+08:00"),
        SH,
    )
    first = score_college("C1", list(reversed(events)), _rule()).to_dict()
    second = score_college("C1", events, _rule()).to_dict()
    assert first == second
    windowed_1 = score_window(events, w, _rule())["C1"].to_dict()
    windowed_2 = score_window(list(reversed(events)), w, _rule())["C1"].to_dict()
    assert windowed_1 == windowed_2
