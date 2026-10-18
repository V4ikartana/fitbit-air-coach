from datetime import datetime, timedelta, timezone

import pytest

from fitbit_air.analysis.activity import (
    SEDENTARY_METRIC, STRENGTH_TYPES, cadence_mvpa_daily, daily_totals, mvpa_week, sedentary_hours, steps_average,
    strength_week, worn_day_mean,
)
from fitbit_air.analysis.evidence import load_registry

REG = load_registry()
UTC = timezone.utc


WEEK = [f"2026-10-{d:02d}" for d in range(8, 15)]  # the 7 days ending 2026-10-14


def test_daily_totals():
    rows = [("2026-10-14", "steps", "a", "b", 4000.0), ("2026-10-14", "steps", "c", "d", 1000.0),
            ("2026-10-13", "steps", "a", "b", 6000.0), ("2026-10-13", "azm_cardio", "a", "b", 10.0)]
    totals = daily_totals(rows)
    assert totals["steps"] == {"2026-10-14": 5000.0, "2026-10-13": 6000.0}
    assert totals["azm_cardio"] == {"2026-10-13": 10.0}


def test_steps_average_counts_worn_days_without_rows_as_zero():
    steps = {d: 7000.0 for d in WEEK[:4]}
    assert steps_average(steps, "2026-10-14", set(WEEK), REG) == pytest.approx(4000.0)  # 28000 / 7 worn days
    assert steps_average({d: 7000.0 for d in WEEK}, "2026-10-14", set(WEEK), REG) == 7000.0
    assert steps_average({"2026-10-14": 5000.0}, "2026-10-14", {"2026-10-14"}, REG) is None  # < min_activity_days


def test_steps_average_ignores_unworn_days_and_falls_back_before_device_history():
    worn = set(WEEK[3:])  # 4 worn days; the earlier steps rows are on days the band was off
    steps = {WEEK[0]: 9999.0, WEEK[3]: 8000.0}
    assert steps_average(steps, "2026-10-14", worn, REG) == pytest.approx(2000.0)  # 8000 / 4
    legacy = {d: 6000.0 for d in WEEK[:5]}
    assert steps_average(legacy, "2026-10-14", set(), REG) == 6000.0  # no wear days: days with step data
    assert steps_average({WEEK[0]: 6000.0}, "2026-10-14", set(), REG) is None


def test_worn_day_mean_any_window_same_rule_as_steps_average():
    month = [f"2026-09-{d:02d}" for d in range(15, 31)] + [f"2026-10-{d:02d}" for d in range(1, 15)]  # 30 days
    steps = {d: 8000.0 for d in month[::2]}  # every other day has rows; all 30 days were worn
    assert worn_day_mean(steps, "2026-10-14", 30, set(month), 7) == pytest.approx(4000.0)
    assert worn_day_mean(steps, "2026-10-14", 30, set(), 7) == 8000.0  # pre-device: days with data
    assert worn_day_mean(steps, "2026-10-14", 30, {"2026-10-14"}, 7) is None  # too few eligible days
    week = {d: 7000.0 for d in WEEK[:4]}
    assert worn_day_mean(week, "2026-10-14", 7, set(WEEK), 4) == steps_average(week, "2026-10-14", set(WEEK), REG)


def test_mvpa_week_accepts_a_longer_window():
    month = [f"2026-09-{d:02d}" for d in range(15, 31)] + [f"2026-10-{d:02d}" for d in range(1, 15)]
    azm = {"azm_cardio": {"2026-09-24": 60.0}}  # 20 days before 2026-10-14: outside the 7-day window
    assert mvpa_week(azm, "2026-10-14", set(month), REG) == 0.0
    assert mvpa_week(azm, "2026-10-14", set(month), REG, window_days=30, min_days=7) == pytest.approx(14.0)
    assert mvpa_week(azm, "2026-10-14", {"2026-10-14"}, REG, window_days=30, min_days=7) is None


def test_mvpa_week_sums_all_zones_scaled_to_seven_days():
    azm = {"azm_fat_burn": {d: 10.0 for d in WEEK}, "azm_cardio": {d: 10.0 for d in WEEK}}
    assert mvpa_week(azm, "2026-10-14", set(WEEK), REG) == pytest.approx(140.0)


def test_mvpa_week_worn_days_without_azm_count_zero():
    four = {"azm_cardio": {d: 30.0 for d in WEEK[:4]}}
    assert mvpa_week(four, "2026-10-14", set(WEEK), REG) == pytest.approx(120.0)  # 120 over 7 worn days
    three = {"azm_cardio": {d: 30.0 for d in WEEK[:3]}}
    worn = set(WEEK[:7])
    assert mvpa_week(three, "2026-10-14", worn, REG) == pytest.approx(90.0)  # not None: 7 worn days
    assert mvpa_week(three, "2026-10-14", set(WEEK[:3]), REG) is None  # only 3 eligible days
    assert mvpa_week(three, "2026-10-14", set(), REG) is None  # no wear days: 3 days with data
    legacy = {"azm_cardio": {d: 20.0 for d in WEEK[:4]}}
    assert mvpa_week(legacy, "2026-10-14", set(), REG) == pytest.approx(140.0)  # 80 over 4 days with data


def test_sedentary_hours_exclude_sleep():
    t0 = datetime(2026, 10, 14, 0, tzinfo=UTC)
    rows = [("2026-10-14", SEDENTARY_METRIC, "2026-10-14T00:00:00Z", "2026-10-14T10:00:00Z", 600.0)]
    sleep = [(t0, t0 + timedelta(hours=4))]
    assert SEDENTARY_METRIC == "level_sedentary_min"
    assert sedentary_hours(rows, sleep)["2026-10-14"] == pytest.approx(6.0)


def test_sedentary_hours_overlapping_sleep_never_subtracted_twice():
    t0 = datetime(2026, 10, 14, 0, tzinfo=UTC)
    rows = [("2026-10-14", SEDENTARY_METRIC, "2026-10-14T00:00:00Z", "2026-10-14T10:00:00Z", 600.0)]
    sleep = [(t0, t0 + timedelta(hours=4)), (t0 + timedelta(hours=2), t0 + timedelta(hours=5)),
             (t0 + timedelta(days=3), t0 + timedelta(days=3, hours=8))]  # last one does not touch the row
    assert sedentary_hours(rows, sleep)["2026-10-14"] == pytest.approx(5.0)


def test_sedentary_hours_row_crossing_midnight_and_other_metrics_ignored():
    sleep = [(datetime(2026, 10, 14, 23, 30, tzinfo=UTC), datetime(2026, 10, 15, 6, tzinfo=UTC))]
    rows = [("2026-10-14", SEDENTARY_METRIC, "2026-10-14T23:00:00Z", "2026-10-15T01:00:00Z", 120.0),
            ("2026-10-14", "sedentary_min", "2026-10-14T00:00:00Z", "2026-10-14T10:00:00Z", 600.0),
            ("2026-10-14", SEDENTARY_METRIC, "2026-10-14T12:00:00Z", "2026-10-14T13:00:00Z", 60.0)]
    # (2h - 1.5h asleep) + 1h awake, all booked to the row's local_date
    assert sedentary_hours(rows, sleep) == {"2026-10-14": pytest.approx(1.5)}


def test_strength_week_counts_distinct_days():
    sessions = [{"exercise_type": "WEIGHTS", "local_date": "2026-10-10", "duration_min": 40.0},
                {"exercise_type": "WEIGHTS", "local_date": "2026-10-10", "duration_min": 20.0},
                {"exercise_type": "RUNNING", "local_date": "2026-10-11", "duration_min": 30.0},
                {"exercise_type": "WEIGHTS", "local_date": "2026-09-01", "duration_min": 60.0}]
    assert "WEIGHTS" in STRENGTH_TYPES
    minutes, days_n = strength_week(sessions, "2026-10-14", 30)
    assert minutes == pytest.approx(60.0 * 7 / 30) and days_n == pytest.approx(1 * 7 / 30)  # two sessions, one day


def test_strength_week_matches_display_name():
    sessions = [{"exercise_type": "CARDIO_WORKOUT", "display_name": "Heavy Weight session", "local_date": "2026-10-12",
                 "duration_min": 30.0},
                {"exercise_type": None, "display_name": "STRENGTH", "local_date": "2026-10-13", "duration_min": 20.0},
                {"exercise_type": "WALKING", "display_name": "Walk", "local_date": "2026-10-13", "duration_min": 45.0},
                {"exercise_type": "CARDIO_WORKOUT", "display_name": None, "local_date": "2026-10-14",
                 "duration_min": 25.0}]
    minutes, days_n = strength_week(sessions, "2026-10-14", 7)
    assert minutes == pytest.approx(50.0) and days_n == pytest.approx(2.0)


def test_sedentary_hours_is_fast_on_a_year_of_minute_rows():
    import time

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for i in range(100_000):
        s = t0 + timedelta(minutes=i)
        rows.append((s.date().isoformat(), SEDENTARY_METRIC, s.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     (s + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), 1.0))
    sleep = [(t0 + timedelta(days=n, hours=-1), t0 + timedelta(days=n, hours=7)) for n in range(400)]
    start = time.perf_counter()
    out = sedentary_hours(rows, sleep)
    assert time.perf_counter() - start < 2.0
    # a full day loses 00:00-07:00 (last night's sleep) and 23:00-24:00 (tonight's) = 8 h
    assert out["2026-01-02"] == pytest.approx(24 - 7 - 1)


def _steps_row(day, hh, mm, value, minutes=1, metric="steps"):
    s = datetime(*map(int, day.split("-")), hh, mm, tzinfo=UTC)
    iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    return (day, metric, iso(s), iso(s + timedelta(minutes=minutes)), value)


def test_cadence_mvpa_counts_brisk_minutes_and_vigorous_counts_double():
    d = "2026-10-14"
    rows = [_steps_row(d, 8, 0, 99), _steps_row(d, 8, 1, 100), _steps_row(d, 8, 2, 129), _steps_row(d, 8, 3, 130),
            _steps_row(d, 8, 10, 600, minutes=5),  # 120 steps/min sustained for 5 minutes
            _steps_row(d, 8, 20, 300, metric="azm_fat_burn")]  # other metrics ignored
    assert cadence_mvpa_daily(rows, [], REG) == {d: pytest.approx(1 + 1 + 2 + 5)}


def test_cadence_mvpa_zero_on_days_with_steps_but_nothing_brisk_and_ignores_aggregated_rows():
    rows = [_steps_row("2026-10-13", 8, 0, 50), _steps_row("2026-10-14", 3, 0, 8000)]  # 8000 in a minute: a daily total
    assert cadence_mvpa_daily(rows, [], REG) == {"2026-10-13": 0.0, "2026-10-14": 0.0}


def test_cadence_mvpa_adds_workouts_without_step_data_minus_brisk_overlap():
    d = "2026-10-14"
    rows = [_steps_row(d, 9, 5, 110)]  # one brisk minute inside the workout
    no_steps = {"local_date": d, "start_utc": f"{d}T09:00:00Z", "end_utc": f"{d}T09:30:00Z", "steps": None,
                "duration_min": 30.0, "exercise_type": "CARDIO_WORKOUT"}
    walk = {**no_steps, "start_utc": f"{d}T12:00:00Z", "end_utc": f"{d}T12:30:00Z", "steps": 3000.0}  # cadence covers
    open_ended = {**no_steps, "start_utc": f"{d}T15:00:00Z", "end_utc": None}
    assert cadence_mvpa_daily(rows, [no_steps, walk, open_ended], REG) == {d: pytest.approx(1 + 29)}
