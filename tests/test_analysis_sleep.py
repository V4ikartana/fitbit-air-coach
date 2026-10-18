from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from fitbit_air.analysis.data import Night
from fitbit_air.analysis.evidence import load_registry
from fitbit_air.analysis.sleep import (
    duration_score, efficiency_score, local_minutes, midpoint_minute_of_day, regularity_score, score_night,
    sleep_score, sri, stages_usable, timing_score, trailing_debt,
)

REG = load_registry()
UTC = timezone.utc
LOCAL_OFFSET_S = 34200


def night(wake: str, bed_h: int = 23, asleep_h: float = 7.5, offset: int = LOCAL_OFFSET_S) -> Night:
    wd = date.fromisoformat(wake)
    bed_day = wd - timedelta(days=1) if bed_h >= 12 else wd
    start = datetime(bed_day.year, bed_day.month, bed_day.day, bed_h, tzinfo=UTC) - timedelta(seconds=offset)
    end = start + timedelta(hours=asleep_h)
    return Night(wake, f"s-{wake}", start, end, offset, "STAGES", asleep_h * 60, asleep_h * 60, 1.0, 60.0, 90.0,
                 [("LIGHT", start, end)])


def test_subscores():
    assert duration_score(450, REG) == 100
    assert duration_score(360, REG) == pytest.approx(66.667, abs=1e-3)
    assert duration_score(180, REG) == 0
    assert duration_score(600, REG) == pytest.approx(85)
    assert duration_score(720, REG) == 70
    assert efficiency_score(0.85, REG) == pytest.approx(80)
    assert regularity_score(81, REG) == pytest.approx(70)
    assert timing_score(75, REG) == pytest.approx(50)


def test_sri_known_answers():
    day0 = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp() // 60)
    regular = {day0 + d * 1440 + m for d in range(8) for m in range(0, 420)}  # 00:00-07:00 every day
    assert sri(regular, day0, 7) == pytest.approx(100)
    flip = {day0 + d * 1440 + m for d in range(8) for m in (range(0, 720) if d % 2 == 0 else range(720, 1440))}
    assert sri(flip, day0, 7) == pytest.approx(-100)


def test_local_minutes_and_midpoint():
    n = night("2026-10-02", bed_h=23, asleep_h=8)
    assert len(local_minutes(n)) == 480
    assert midpoint_minute_of_day(n) == pytest.approx(3 * 60)  # 23:00-07:00 local -> 03:00


def test_sleep_score_renormalises_missing_subscores():
    value, weights = sleep_score({"duration": 100, "regularity": None, "efficiency": 80, "timing": None}, REG)
    assert weights == {"duration": pytest.approx(0.35 / 0.55), "efficiency": pytest.approx(0.20 / 0.55)}
    assert value == pytest.approx((0.35 * 100 + 0.20 * 80) / 0.55)


def test_sleep_score_none_when_nothing_available():
    assert sleep_score({"duration": None, "regularity": None, "efficiency": None, "timing": None}, REG) == (None, {})


def test_local_minutes_counts_only_sleep_stages():
    n = night("2026-10-02", asleep_h=8)
    s, e = n.start_utc, n.end_utc
    mixed = replace(n, stages=[("awake", s, s + timedelta(hours=1)), ("deep", s + timedelta(hours=1), e),
                               ("UNKNOWN", e, e + timedelta(hours=1))])
    assert len(local_minutes(mixed)) == 420


def test_trailing_debt():
    nights = {n.date: n for n in (night(f"2026-10-0{i}", asleep_h=6.5) for i in range(1, 8))}
    assert trailing_debt("2026-10-07", nights, 7.5, REG) == pytest.approx(7.0)


def test_trailing_debt_scaled_estimate_and_insufficient():
    nights = {n.date: n for n in (night(f"2026-10-0{i}", asleep_h=6.5) for i in range(1, 8))}
    four = {d: n for d, n in nights.items() if d >= "2026-10-04"}
    assert trailing_debt("2026-10-07", four, 7.5, REG) == pytest.approx(7.0)  # mean 1 h x 7 days
    three = {d: n for d, n in nights.items() if d >= "2026-10-05"}
    assert trailing_debt("2026-10-07", three, 7.5, REG) is None
    assert trailing_debt("2026-10-12", nights, 7.5, REG) is None      # only 10-06 and 10-07 in the window
    assert trailing_debt("2026-10-10", nights, 7.5, REG) is not None  # 10-04..10-07 = 4 nights


def test_trailing_debt_counts_short_staged_nights_only():
    nights = {n.date: n for n in (night(f"2026-10-0{i}", asleep_h=7.5) for i in range(1, 8))}
    nights["2026-10-07"] = night("2026-10-07", asleep_h=2.5)          # short (under 3 h) but staged: counts
    assert trailing_debt("2026-10-07", nights, 7.5, REG) == pytest.approx(5.0)
    nights["2026-10-07"] = replace(nights["2026-10-07"], sleep_type="classic")  # not staged: ignored
    assert trailing_debt("2026-10-07", nights, 7.5, REG) == pytest.approx(0.0)
    nights["2026-10-06"] = replace(nights["2026-10-06"], min_asleep=None)       # unknown minutes: ignored
    assert trailing_debt("2026-10-07", nights, 7.5, REG) == pytest.approx(0.0)


def test_score_night_full_window_and_insufficient():
    wakes = [f"2026-10-{d:02d}" for d in range(1, 10)]
    nights = {w: night(w) for w in wakes}
    out = score_night("2026-10-09", nights, set(nights), 7.5, REG)
    assert out["sri"] == pytest.approx(100) and out["timing_sd_min"] < 1
    assert out["subscores"]["regularity"] == 100 and out["subscores"]["timing"] == 100
    assert out["value"] == pytest.approx(100)
    assert out["debt_h"] == pytest.approx(0)
    early = score_night("2026-10-03", nights, set(nights), 7.5, REG)
    assert early["sri"] is None and early["subscores"]["regularity"] is None
    assert score_night("2026-10-20", nights, set(nights), 7.5, REG) is None


def test_window_needs_exactly_seven_nights_ending_on_day():
    wakes = [f"2026-10-{d:02d}" for d in range(1, 10)]
    nights = {w: night(w) for w in wakes}
    assert score_night("2026-10-09", nights, set(nights) - {"2026-10-02"}, 7.5, REG)["sri"] == pytest.approx(100)
    assert score_night("2026-10-09", nights, set(nights) - {"2026-10-03"}, 7.5, REG)["sri"] is None


def test_last_night_counts_in_sri():
    wakes = [f"2026-10-{d:02d}" for d in range(1, 10)]
    regular = {w: night(w) for w in wakes}
    shifted = dict(regular)
    shifted["2026-10-09"] = night("2026-10-09", bed_h=2, asleep_h=7.5)  # 3 h later than usual
    base = score_night("2026-10-09", regular, set(regular), 7.5, REG)["sri"]
    late = score_night("2026-10-09", shifted, set(shifted), 7.5, REG)["sri"]
    assert base - late > 5


def test_missing_membership_and_no_scorable_data():
    nights = {"2026-10-09": night("2026-10-09")}
    assert score_night("2026-10-09", nights, {"2026-10-09", "2026-10-08"}, 7.5, REG) is not None
    assert score_night("2026-10-08", nights, {"2026-10-08"}, 7.5, REG) is None  # valid but absent from nights
    blank = replace(nights["2026-10-09"], min_asleep=None, efficiency=None)
    assert score_night("2026-10-09", {"2026-10-09": blank}, {"2026-10-09"}, 7.5, REG) is None


def test_night_without_stages_does_not_collapse_sri():
    wakes = [f"2026-10-{d:02d}" for d in range(1, 10)]
    nights = {w: night(w) for w in wakes}
    nights["2026-10-06"] = replace(nights["2026-10-06"], stages=[])
    assert not stages_usable(nights["2026-10-06"])
    out = score_night("2026-10-09", nights, set(nights), 7.5, REG)
    assert out["sri"] is None and out["subscores"]["regularity"] is None and out["subscores"]["timing"] is None


def test_stages_usable_thresholds():
    n = night("2026-10-02", asleep_h=8)
    short = replace(n, stages=[("LIGHT", n.start_utc, n.start_utc + timedelta(hours=6))])  # 360 < 0.8 * 480
    assert not stages_usable(short)
    ok = replace(n, stages=[("LIGHT", n.start_utc, n.start_utc + timedelta(hours=7))])  # 420 >= 384
    assert stages_usable(ok)
    assert stages_usable(replace(n, min_asleep=None))
    assert not stages_usable(replace(n, min_asleep=None, stages=[("LIGHT", n.start_utc, n.start_utc + timedelta(minutes=59))]))


def test_trailing_debt_ignores_short_fragments():
    nights = {n.date: n for n in (night(f"2026-10-0{i}", asleep_h=7.5) for i in range(1, 8))}
    nights["2026-10-07"] = night("2026-10-07", asleep_h=40 / 60)   # a 40-min fragment: not a night's sleep
    assert trailing_debt("2026-10-07", nights, 7.5, REG) == pytest.approx(0.0)
    nights["2026-10-07"] = night("2026-10-07", asleep_h=1.0)       # 60 min in the period: counts
    assert trailing_debt("2026-10-07", nights, 7.5, REG) == pytest.approx(6.5)
