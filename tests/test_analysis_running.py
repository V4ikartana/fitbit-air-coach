import math
from datetime import datetime, timedelta, timezone

import pytest

from fitbit_air.timeutil import parse_ts as parse

from fitbit_air.analysis.evidence import load_registry
from fitbit_air.analysis.running import (hrmax, intensity_balance, is_run, run_metrics, spike, weekly_progression,
                                         zone_bounds)

REG = load_registry()
UTC = timezone.utc


def session(sid="r1", start="2026-10-14T01:00:00Z", minutes=30, km=5.0, avg_hr=150.0, etype="RUNNING"):
    s = datetime.fromisoformat(start.replace("Z", "+00:00"))
    return {"id": sid, "exercise_type": etype, "start_utc": start,
            "end_utc": (s + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"), "local_date": "2026-10-14",
            "duration_min": float(minutes), "distance_m": km * 1000, "avg_hr": avg_hr}


def minutes_hr(start, n, first_bpm, second_bpm):
    s = datetime.fromisoformat(start.replace("Z", "+00:00"))
    return [(s + timedelta(minutes=i), first_bpm if i < n // 2 else second_bpm) for i in range(n)]


def hr_series(start, values):
    s = datetime.fromisoformat(start.replace("Z", "+00:00"))
    return [(s + timedelta(minutes=i), float(v)) for i, v in enumerate(values)]


def test_is_run_and_hrmax():
    assert is_run({"exercise_type": "RUNNING"}) and is_run({"exercise_type": "TREADMILL_RUNNING"})
    assert not is_run({"exercise_type": "WALKING"}) and not is_run({"exercise_type": None})
    assert is_run({"exercise_type": "RUN"}) and is_run({"exercise_type": "running"})
    assert not is_run({"exercise_type": "CRUNCH"}) and not is_run({"exercise_type": "CRUNCHES"})
    assert hrmax(26, None, REG) == pytest.approx(189.8)
    assert hrmax(26, 195, REG) == 195


def test_karvonen_zone_bounds():
    b = zone_bounds(190.0, 90.0)
    assert b == pytest.approx([140.0, 150.0, 160.0, 170.0, 180.0, 190.0])


def test_run_metrics_zones_pace_decoupling_economy():
    # 40 min: HR 140 for 25 min then 152. The first 10 min are warm-up; the remaining 30 min split at minute 25.
    s = session(minutes=40, km=5.0, avg_hr=145.0)
    hr = hr_series(s["start_utc"], [140.0] * 25 + [152.0] * 15)
    m = run_metrics(s, hr, zone_bounds(190.0, 90.0), REG)
    assert m["distance_km"] == 5.0 and m["pace_min_km"] == pytest.approx(8.0)
    assert m["z0_min"] == 0 and m["z1_min"] == 25 and m["z2_min"] == 15 and m["z3_min"] == 0
    assert m["decoupling_approx"] == 1 and m["decoupling_pct"] == pytest.approx((1 - 140 / 152) * 100)
    assert m["economy_m_per_beat"] == pytest.approx(5000 / (145.0 * 40))
    short = run_metrics(session(minutes=25), hr_series("2026-10-14T01:00:00Z", [140] * 12 + [150] * 13),
                        zone_bounds(190, 90), REG)  # only 15 min after warm-up
    assert short["decoupling_pct"] is None and short["decoupling_approx"] is None
    hard = run_metrics(session(avg_hr=175.0), [], zone_bounds(190.0, 90.0), REG)
    assert hard["economy_m_per_beat"] is None
    assert all(hard[f"z{z}_min"] is None for z in range(0, 6))  # no HR samples: unknown, not zero
    assert hard["decoupling_pct"] is None and hard["decoupling_approx"] is None


def test_zero_minutes_below_z1_counted():
    s = session(minutes=10)
    m = run_metrics(s, hr_series(s["start_utc"], [100] * 4 + [145] * 6), zone_bounds(190.0, 90.0), REG)
    assert m["z0_min"] == 4 and m["z1_min"] == 6


def test_zero_duration_guard():
    s = session(minutes=30)
    s["duration_min"] = 0.0
    s["end_utc"] = s["start_utc"]
    m = run_metrics(s, hr_series(s["start_utc"], [140] * 5), zone_bounds(190.0, 90.0), REG)
    assert m["pace_min_km"] is None and m["economy_m_per_beat"] is None and m["decoupling_pct"] is None
    s["duration_min"] = -5.0
    m = run_metrics(s, [], zone_bounds(190.0, 90.0), REG)
    assert m["pace_min_km"] is None and m["economy_m_per_beat"] is None


def test_decoupling_ignores_warmup_ramp():
    s = session(minutes=40)
    ramp = [100 + 60 * i / 7 for i in range(8)]  # 100 -> 160 over 8 min, then flat
    m = run_metrics(s, hr_series(s["start_utc"], ramp + [160] * 32), zone_bounds(190.0, 90.0), REG)
    assert m["decoupling_pct"] == pytest.approx(0.0, abs=1e-9)


def test_decoupling_none_for_run_walk():
    s = session(minutes=40)
    m = run_metrics(s, hr_series(s["start_utc"], [120, 160] * 20), zone_bounds(190.0, 90.0), REG)
    assert m["decoupling_pct"] is None and m["decoupling_approx"] is None


def test_decoupling_splits_by_time_not_sample_count():
    s = session(minutes=40)
    # minutes 10-14 missing (dropout): remaining samples are 15..39; the time midpoint is minute 25
    hr = [x for x in hr_series(s["start_utc"], [140] * 25 + [152] * 15)
          if not 10 <= (x[0] - parse(s["start_utc"])).seconds // 60 < 15]
    m = run_metrics(s, hr, zone_bounds(190.0, 90.0), REG)
    assert m["decoupling_pct"] == pytest.approx((1 - 140 / 152) * 100)


def test_spike_rule_vs_longest_run_in_previous_30_days():
    prior = [{"start_utc": "2026-10-01T01:00:00Z", "distance_km": 4.0},
             {"start_utc": "2026-09-01T01:00:00Z", "distance_km": 9.0}]  # outside 30 days
    assert spike(4.4, "2026-10-14T01:00:00Z", prior, REG) == (None, None)            # exactly +10%
    pct, cls = spike(4.41, "2026-10-14T01:00:00Z", prior, REG)
    assert cls == "small" and pct == pytest.approx(10.25)
    assert spike(6.0, "2026-10-14T01:00:00Z", prior, REG)[1] == "moderate"
    assert spike(9.0, "2026-10-14T01:00:00Z", prior, REG)[1] == "large"
    assert spike(3.0, "2026-10-14T01:00:00Z", [], REG) == (None, None)               # first run


def test_weekly_progression_flags_compounding_short_runs_the_per_session_rule_misses():
    runs, flagged_at = [], None
    for i in range(12):  # three runs a week, each +10% on the previous one
        start = (datetime(2026, 9, 1, 1, tzinfo=UTC) + timedelta(days=i * 7 / 3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        km = 3.0 * 1.1 ** i
        assert spike(km, start, runs, REG) == (None, None)  # each run is only +10% on the previous one
        if flagged_at is None and weekly_progression(km, start, runs, REG) is not None:
            flagged_at = i
        runs.append({"start_utc": start, "distance_km": km})
    assert flagged_at is not None and flagged_at <= 4  # within the first 2-3 weeks


def test_weekly_progression_steady_ten_percent_per_week_not_flagged():
    runs = []
    for w in range(10):
        start = (datetime(2026, 9, 1, 1, tzinfo=UTC) + timedelta(days=7 * w)).strftime("%Y-%m-%dT%H:%M:%SZ")
        km = 3.0 * 1.1 ** w
        assert weekly_progression(km, start, runs, REG) is None
        runs.append({"start_utc": start, "distance_km": km})


def test_weekly_progression_tolerates_earlier_start_time_than_last_week():
    prior = [{"start_utc": "2026-10-03T08:00:00Z", "distance_km": 3.0},
             {"start_utc": "2026-10-10T08:00:00Z", "distance_km": 3.3}]
    # started 30 minutes earlier than a week after the previous long run: still compared with last week's 3.3 km
    assert weekly_progression(3.63, "2026-10-17T07:30:00Z", prior, REG) is None


def test_weekly_progression_window_and_value():
    prior = [{"start_utc": "2026-10-10T01:00:00Z", "distance_km": 9.0},   # only 4 days earlier: ignored
             {"start_utc": "2026-10-01T01:00:00Z", "distance_km": 4.0},
             {"start_utc": "2026-09-01T01:00:00Z", "distance_km": 8.0}]   # older than 30 days: ignored
    assert weekly_progression(5.0, "2026-10-14T01:00:00Z", prior, REG) == pytest.approx(25.0)
    assert weekly_progression(4.4, "2026-10-14T01:00:00Z", prior, REG) is None  # exactly +10%
    assert weekly_progression(5.0, "2026-10-14T01:00:00Z", prior[:1] + prior[2:], REG) is None  # no run 7-30 days back
    assert weekly_progression(None, "2026-10-14T01:00:00Z", prior, REG) is None


def test_intensity_balance():
    runs = [{"local_date": "2026-10-10", "z0_min": 10, "z1_min": 10, "z2_min": 20, "z3_min": 10, "z4_min": 0,
             "z5_min": 0},
            {"local_date": "2026-08-01", "z1_min": 0, "z2_min": 0, "z3_min": 50, "z4_min": 0, "z5_min": 0}]
    assert intensity_balance(runs, "2026-10-14", REG) == pytest.approx(0.8)  # walk breaks (z0) count as easy
    assert intensity_balance([], "2026-10-14", REG) is None
    assert intensity_balance([{"local_date": "2026-10-10", "z0_min": None, "z1_min": None}], "2026-10-14", REG) is None


from fitbit_air.analysis.running import forecast, riegel_estimate, uth_vo2max


def test_forecast_statuses():
    runs = [{"local_date": "2026-10-10", "distance_km": 5.0}]
    f = forecast(runs, "2026-10-14", "2027-02-07", 10.0, REG)
    # 5 -> 10 km needs ceil(ln2/ln1.1) = 8 build weeks; a cutback after every 3 adds (8-1)//3 = 2 -> 10 weeks
    assert f["longest_km"] == 5.0 and f["increments"] == math.ceil(math.log(2) / math.log(1.1)) == 8
    # 116 days = 16 weeks left; 10 <= 16 - 2
    assert f["weeks_needed"] == 10 and f["weeks_left"] == 16 and f["status"] == "on_track"
    assert f["latest_start"] == "2026-11-15"  # race 2027-02-07 - (10 + 2) weeks = 84 days
    # 16 weeks - 2 taper = 14 -> 14 - 14//4 = 11 build weeks -> 5 x 1.1^11 = 14.27, capped at the race distance
    assert f["safe_by_race_km"] == 10.0
    # 2027-01-03 to 2027-02-07 is 35 days = 5 weeks left, needs 10
    behind = forecast([{"local_date": "2026-12-30", "distance_km": 5.0}], "2027-01-03", "2027-02-07", 10.0, REG)
    assert behind["status"] == "behind" and behind["weeks_left"] == 5
    assert behind["safe_by_race_km"] == pytest.approx(5.0 * 1.1 ** 3)  # (5 - 2) = 3 build weeks, no cutback yet
    # 2026-11-16 to 2027-02-07 is 83 days = 11 weeks: 10 <= 11 but 10 > 11 - 2
    tight = forecast([{"local_date": "2026-11-12", "distance_km": 5.0}], "2026-11-16", "2027-02-07", 10.0, REG)
    assert tight["weeks_left"] == 11 and tight["status"] == "tight"
    # 2026-12-13 to 2027-02-07 is 56 days = 8 weeks, now behind (needs 10)
    edge = forecast([{"local_date": "2026-12-08", "distance_km": 5.0}], "2026-12-13", "2027-02-07", 10.0, REG)
    assert edge["weeks_left"] == 8 and edge["status"] == "behind"
    done = forecast([{"local_date": "2026-10-10", "distance_km": 10.5}], "2026-10-14", "2027-02-07", 10.0, REG)
    assert done["increments"] == 0 and done["weeks_needed"] == 0 and done["status"] == "on_track"
    assert done["safe_by_race_km"] == 10.0


def test_forecast_cutback_week_arithmetic():
    def needed(longest, race_km):
        longest *= 1.001  # keep clear of float ties at exact +10% steps
        f = forecast([{"local_date": "2026-10-10", "distance_km": longest}], "2026-10-14", "2027-06-30", race_km, REG)
        return f["increments"], f["weeks_needed"]
    assert needed(10 / 1.1, 10.0) == (1, 1)
    assert needed(10 / 1.1 ** 3, 10.0) == (3, 3)   # three build weeks: no cutback yet
    assert needed(10 / 1.1 ** 4, 10.0) == (4, 5)   # build x3, cutback, build
    assert needed(10 / 1.1 ** 7, 10.0) == (7, 9)   # two cutbacks


def test_forecast_no_runs():
    on_time = forecast([], "2026-06-01", "2027-02-07", 10.0, REG)
    # 3 -> 10 km: ceil(ln(10/3)/ln1.1) = 13 build weeks + (13-1)//3 = 4 cutbacks = 17, + 2 taper = 19 weeks before race
    # (2027-02-07 minus 133 days = 2026-09-27)
    assert on_time["status"] == "no_runs" and on_time["first_run_km"] == 3.0
    assert on_time["weeks_needed"] == 17 and on_time["first_run_by"] == "2026-09-27" and on_time["weeks_short"] is None
    late = forecast([], "2026-10-14", "2027-02-07", 10.0, REG)  # first_run_by 2026-09-27 is already past
    assert late["status"] == "no_runs_behind" and late["first_run_by"] == "2026-09-27"
    assert late["weeks_short"] == math.ceil(17 / 7) == 3  # 2026-09-27 to 2026-10-14 is 17 days
    # 16 weeks left - 2 taper = 14 -> 14 - 14//4 = 11 build weeks from a 3 km start
    assert late["safe_by_race_km"] == pytest.approx(3.0 * 1.1 ** 11)
    assert late["longest_km"] is None and late["increments"] is None


def test_riegel_estimate_uses_fastest_qualifying_run():
    runs = [{"local_date": "2026-10-10", "distance_km": 5.0, "duration_min": 30.0},
            {"local_date": "2026-10-12", "distance_km": 3.0, "duration_min": 15.0},   # faster but under half the race
            {"local_date": "2026-10-13", "distance_km": 2.0, "duration_min": 10.0}]  # too short
    est = riegel_estimate(runs, "2026-10-14", 10.0, REG)
    assert est == pytest.approx(30.0 * (10 / 5) ** 1.06)
    assert riegel_estimate(runs[1:], "2026-10-14", 10.0, REG) is None
    # 3 km is >= half of 5
    assert riegel_estimate(runs[1:2], "2026-10-14", 5.0, REG) == pytest.approx(15.0 * (5 / 3) ** 1.06)


def test_uth_vo2max():
    assert uth_vo2max(190.0, 90.0, REG) == pytest.approx(15.3 * 190 / 90)
    assert uth_vo2max(None, 90.0, REG) is None


def test_run_metrics_without_end_time_uses_duration_or_returns_unknown_zones():
    bounds = zone_bounds(190, 60)
    s = {**session(minutes=30), "end_utc": None}
    hr = hr_series("2026-10-14T01:00:00Z", [150] * 40)  # 10 minutes past the 30-minute run
    row = run_metrics(s, hr, bounds, REG)
    assert sum(row[f"z{z}_min"] for z in range(6)) == 30 and row["duration_min"] == 30.0
    row = run_metrics({**s, "duration_min": None}, hr, bounds, REG)
    assert all(row[f"z{z}_min"] is None for z in range(6)) and row["duration_min"] is None
    assert row["decoupling_pct"] is None and row["pace_min_km"] is None


def test_forecast_a_spike_cannot_improve_the_plan():
    runs = [{"local_date": "2026-12-15", "distance_km": 5.0}, {"local_date": "2026-12-18", "distance_km": 9.0}]
    f = forecast(runs, "2026-12-20", "2027-02-07", 10.0, REG)  # 49 days = 7 weeks left
    assert f["longest_km"] == 9.0 and f["planning_km"] == pytest.approx(5.5)  # capped at 1.1 x the previous 5 km
    assert f["status"] == "behind"  # planning from 9 km would have said on_track
    assert f["safe_by_race_km"] == pytest.approx(min(10.0, 5.5 * 1.1 ** 4))  # 5 available weeks - 1 cutback
    single = forecast([{"local_date": "2026-12-18", "distance_km": 9.0}], "2026-12-20", "2027-02-07", 10.0, REG)
    assert single["planning_km"] == 9.0  # no earlier run to compare with
    repeat = forecast(runs + [{"local_date": "2026-12-19", "distance_km": 9.0}], "2026-12-20", "2027-02-07", 10.0, REG)
    assert repeat["planning_km"] == pytest.approx(5.5 * 1.1)  # repeating the spike earns one +10% step, not 9 km
    # 6 steps + 1 cutback = 7 weeks: 7 > 7 - 2 but 7 <= 7 -> tight; 9 km said on_track
    assert repeat["status"] == "tight"
