import logging

import pytest

import samples
from fitbit_air import normalize
from fitbit_air.errors import NormalizeError
from fitbit_air.normalize import MALFORMED, NORMALIZERS, rebuild, to_raw_row
from fitbit_air.registry import BY_NAME


def ingest(store, name, *points):
    dt = BY_NAME[name]
    dates = store.upsert_raw([to_raw_row(dt, p) for p in points])
    rebuild(store, dt, dates)


def q(store, sql, *args):
    return [dict(r) for r in store.conn.execute(sql, args)]


# ---- raw rows ----

def test_raw_row_for_sample_uses_civil_date():
    row = to_raw_row(BY_NAME["heart-rate"], samples.heart_rate("2026-10-12T16:00:00Z"))
    assert row.point_key == "2026-10-12T16:00:00Z"
    assert row.start_utc == "2026-10-12T16:00:00Z" and row.end_utc is None
    assert row.local_date == "2026-10-13"  # 01:30 local
    assert row.utc_offset == "34200s" and row.source == "reconciled"


def test_raw_row_for_daily():
    row = to_raw_row(BY_NAME["daily-resting-heart-rate"], samples.daily_rhr("2026-10-13"))
    assert (row.point_key, row.local_date, row.start_utc) == ("2026-10-13", "2026-10-13", None)


def test_raw_row_source_from_data_source():
    point = samples.weight("2026-10-12T22:00:00Z")
    point["dataSource"] = {"platform": "FITBIT", "device": {"displayName": "Fitbit Air"}}
    assert to_raw_row(BY_NAME["weight"], point).source == "FITBIT/Fitbit Air"


# ---- heart & samples ----

def test_heart_rate(store):
    ingest(store, "heart-rate", samples.heart_rate("2026-10-12T16:00:00Z", 61))
    assert q(store, "SELECT ts_utc, local_ts, local_date, bpm, motion, src_type FROM heart_rate") == [{
        "ts_utc": "2026-10-12T16:00:00Z", "local_ts": "2026-10-13T01:30:00", "local_date": "2026-10-13",
        "bpm": 61, "motion": "SEDENTARY", "src_type": "heart-rate"}]


def test_hrv_samples(store):
    ingest(store, "heart-rate-variability", samples.hrv_sample("2026-10-12T16:00:00Z"))
    got = {r["metric"]: r["value"] for r in q(store, "SELECT metric, value FROM samples")}
    assert got == {"hrv_rmssd_ms": 40.0, "hrv_sdnn_ms": 55.0}


def test_weight_in_kg(store):
    ingest(store, "weight", samples.weight("2026-10-12T22:00:00Z", 72500))
    assert q(store, "SELECT metric, value FROM samples") == [{"metric": "weight_kg", "value": 72.5}]


# ---- daily ----

def test_daily_hrv_and_rhr_in_wide_view(store):
    ingest(store, "daily-heart-rate-variability", samples.daily_hrv("2026-10-13"))
    ingest(store, "daily-resting-heart-rate", samples.daily_rhr("2026-10-13", 57))
    row = q(store, "SELECT * FROM daily_wide")[0]
    assert row["local_date"] == "2026-10-13"
    assert row["hrv_rmssd_ms"] == 42.0 and row["hrv_deep_rmssd_ms"] == 48.0 and row["resting_hr"] == 57.0
    assert row["non_rem_hr"] == 55.0


def test_daily_temperature_deviation(store):
    ingest(store, "daily-sleep-temperature-derivations", samples.daily_temp("2026-10-13"))
    got = {r["metric"]: r["value"] for r in q(store, "SELECT metric, value FROM daily_metrics")}
    assert got["sleep_temp_dev_c"] == pytest.approx(0.3)
    assert got["sleep_temp_baseline_c"] == 34.6 and got["sleep_temp_sd30_c"] == 0.2


def test_daily_hr_zones(store):
    ingest(store, "daily-heart-rate-zones", samples.daily_hr_zones("2026-10-13"))
    got = {r["metric"]: r["value"] for r in q(store, "SELECT metric, value FROM daily_metrics")}
    assert got == {"hr_zone_light_lo_bpm": 95.0, "hr_zone_light_hi_bpm": 114.0,
                   "hr_zone_peak_lo_bpm": 162.0, "hr_zone_peak_hi_bpm": 220.0}


# ---- rebuild behaviour ----

def test_rebuild_is_idempotent(store):
    point = samples.heart_rate("2026-10-12T16:00:00Z")
    ingest(store, "heart-rate", point)
    ingest(store, "heart-rate", point)
    assert store.table_counts()["heart_rate"] == 1
    assert store.raw_count("heart-rate") == 1


def test_malformed_point_is_kept_raw_but_skipped(store, caplog):
    bad = samples.heart_rate("2026-10-12T16:00:00Z")
    del bad["heartRate"]["beatsPerMinute"]
    ingest(store, "heart-rate", bad, samples.heart_rate("2026-10-12T16:00:01Z"))
    assert store.raw_count("heart-rate") == 2
    assert store.table_counts()["heart_rate"] == 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "skipping 1 of 2 malformed heart-rate points" in warnings[0]
    assert "2026-10-12T16:00:00Z" in warnings[0] and "KeyError" in warnings[0]


def test_wrong_shaped_payload_is_skipped_not_fatal(store):
    # `.get` on a str raises AttributeError, which must count as malformed too.
    weird = samples.heart_rate("2026-10-12T16:00:00Z")
    weird["heartRate"]["metadata"] = "bad"
    ingest(store, "heart-rate", weird, samples.heart_rate("2026-10-12T16:00:01Z"))
    assert store.raw_count("heart-rate") == 2 and store.table_counts()["heart_rate"] == 1
    assert AttributeError in MALFORMED


def test_all_points_failing_raises_and_keeps_old_clean_rows(store):
    ingest(store, "heart-rate", samples.heart_rate("2026-10-12T16:00:00Z", 70))
    bad = samples.heart_rate("2026-10-12T16:00:00Z")
    del bad["heartRate"]["beatsPerMinute"]
    dt = BY_NAME["heart-rate"]
    dates = store.upsert_raw([to_raw_row(dt, bad)])
    with pytest.raises(NormalizeError, match=r"heart-rate: all 1 points failed to normalize, e\.g\. 2026-10-12T16:00:00Z"):
        rebuild(store, dt, dates)
    assert [r["bpm"] for r in q(store, "SELECT bpm FROM heart_rate")] == [70]  # not wiped


def test_rebuild_batches_dates_without_losing_rows(store, monkeypatch):
    monkeypatch.setattr(normalize, "REBUILD_BATCH_DAYS", 3)
    pts = [samples.heart_rate(f"2026-10-{day:02d}T06:00:00Z", 60 + day) for day in range(1, 11)]
    dt = BY_NAME["heart-rate"]
    dates = store.upsert_raw([to_raw_row(dt, p) for p in pts])
    batches = []
    real = store.replace_clean
    monkeypatch.setattr(store, "replace_clean", lambda src, ds, rows: (batches.append(list(ds)), real(src, ds, rows))[1])
    assert rebuild(store, dt, dates) == 10
    assert [len(b) for b in batches] == [3, 3, 3, 1]  # 10 dates, boundaries at every third
    assert store.table_counts()["heart_rate"] == 10
    assert [r["bpm"] for r in q(store, "SELECT bpm FROM heart_rate ORDER BY ts_utc")] == [60 + d for d in range(1, 11)]


# ---- local date fallback ----

def test_missing_civil_time_uses_utc_offset_for_local_date(store):
    point = samples.heart_rate("2026-10-12T16:00:00Z")  # 01:30 local the next day
    del point["heartRate"]["sampleTime"]["civilTime"]
    assert to_raw_row(BY_NAME["heart-rate"], point).local_date == "2026-10-13"
    ingest(store, "heart-rate", point)
    assert q(store, "SELECT local_date FROM heart_rate") == [{"local_date": "2026-10-13"}]


def test_missing_civil_time_and_offset_falls_back_to_utc_date_with_warning(caplog):
    point = samples.heart_rate("2026-10-12T16:00:00Z")
    del point["heartRate"]["sampleTime"]["civilTime"], point["heartRate"]["sampleTime"]["utcOffset"]
    with caplog.at_level(logging.WARNING):
        row = to_raw_row(BY_NAME["heart-rate"], point)
    assert row.local_date == "2026-10-12" and row.utc_offset is None
    assert "no civil time or UTC offset" in caplog.text


def test_missing_civil_time_for_interval_and_sleep_uses_offset():
    st = samples.steps("2026-10-12T14:25:00Z", "2026-10-12T14:35:00Z")  # 23:55 local on the 12th
    del st["steps"]["interval"]["civilStartTime"]
    assert to_raw_row(BY_NAME["steps"], st).local_date == "2026-10-12"
    stages = [("LIGHT", "2026-10-12T13:00:00Z", "2026-10-12T15:00:00Z")]
    sl = samples.sleep("n", "2026-10-12T13:00:00Z", "2026-10-12T15:00:00Z", stages)
    del sl["sleep"]["interval"]["civilEndTime"]  # wakes 00:30 local on the 13th
    assert to_raw_row(BY_NAME["sleep"], sl).local_date == "2026-10-13"


def test_raw_row_for_sleep_uses_wake_date_and_end_offset():
    row = to_raw_row(BY_NAME["sleep"], samples.sleep("n1", "2026-10-12T13:00:00Z", "2026-10-12T21:00:00Z", samples.NIGHT_STAGES))
    assert row.point_key == "users/me/dataTypes/sleep/dataPoints/n1"
    assert (row.start_utc, row.end_utc) == ("2026-10-12T13:00:00Z", "2026-10-12T21:00:00Z")
    assert row.local_date == "2026-10-13" and row.utc_offset == "34200s"


def test_raw_row_for_interval_uses_start_date_and_start_offset():
    row = to_raw_row(BY_NAME["steps"], samples.steps("2026-10-12T14:25:00Z", "2026-10-12T14:35:00Z"))  # 23:55 local
    assert row.point_key == "2026-10-12T14:25:00Z"
    assert (row.start_utc, row.end_utc) == ("2026-10-12T14:25:00Z", "2026-10-12T14:35:00Z")
    assert row.local_date == "2026-10-12" and row.utc_offset == "34200s"


# ---- activity, exercise, alerts ----

def test_steps_and_daily_total(store):
    ingest(store, "steps",
           samples.steps("2026-10-12T23:00:00Z", "2026-10-12T23:01:00Z", 100),
           samples.steps("2026-10-12T23:01:00Z", "2026-10-12T23:02:00Z", 150))
    assert q(store, "SELECT local_date, metric, value FROM activity_daily") == [
        {"local_date": "2026-10-13", "metric": "steps", "value": 250.0}]


def test_active_zone_minutes_keeps_each_zone(store):
    ingest(store, "active-zone-minutes",
           samples.azm("2026-10-12T23:00:00Z", "2026-10-12T23:01:00Z", "CARDIO", 2),
           samples.azm("2026-10-12T23:00:00Z", "2026-10-12T23:01:00Z", "PEAK", 2))
    got = sorted((r["metric"], r["value"]) for r in q(store, "SELECT metric, value FROM activity_intervals"))
    assert got == [("azm_cardio", 2.0), ("azm_peak", 2.0)]


def test_active_minutes_by_level(store):
    ingest(store, "active-minutes", samples.active_minutes("2026-10-12T23:00:00Z", "2026-10-13T00:00:00Z"))
    got = {r["metric"]: r["value"] for r in q(store, "SELECT metric, value FROM activity_intervals")}
    assert got == {"active_min_light": 10.0, "active_min_vigorous": 5.0}


def test_time_in_zone_is_interval_length(store):
    ingest(store, "time-in-heart-rate-zone", samples.time_in_zone("2026-10-12T23:00:00Z", "2026-10-12T23:01:30Z"))
    assert q(store, "SELECT metric, value FROM activity_intervals") == [{"metric": "hr_zone_moderate_minutes", "value": 1.5}]


def test_exercise_session(store):
    ingest(store, "exercise", samples.exercise("run1", "2026-10-12T21:00:00Z", "2026-10-12T21:35:00Z"))
    row = q(store, "SELECT * FROM exercise_sessions")[0]
    assert row["id"] == "users/me/dataTypes/exercise/dataPoints/run1"
    assert row["exercise_type"] == "RUNNING" and row["local_date"] == "2026-10-13"
    assert row["duration_min"] == 35.0 and row["active_min"] == 30.0
    assert row["avg_hr"] == 148.0 and row["calories_kcal"] == 320.5
    assert row["steps"] == 4100.0 and row["distance_m"] == 5000.0 and row["azm"] == 28.0


def test_irregular_rhythm_alert(store):
    ingest(store, "irregular-rhythm-notification", samples.irn("2026-10-12T21:00:00Z", "2026-10-12T22:00:00Z"))
    row = q(store, "SELECT kind, local_date, start_utc FROM alerts")[0]
    assert row == {"kind": "irregular_rhythm", "local_date": "2026-10-13", "start_utc": "2026-10-12T21:00:00Z"}


# ---- sleep ----

def test_sleep_session_and_stages(store):
    ingest(store, "sleep", samples.sleep("n1", "2026-10-12T13:00:00Z", "2026-10-12T21:00:00Z", samples.NIGHT_STAGES))
    s = q(store, "SELECT * FROM sleep_sessions")[0]
    assert s["id"] == "users/me/dataTypes/sleep/dataPoints/n1"
    assert s["local_date"] == "2026-10-13"  # wake date
    assert s["is_main"] == 1 and s["is_nap"] == 0 and s["sleep_type"] == "stages"
    assert s["min_asleep"] == 450.0 and s["min_in_period"] == 480.0 and s["efficiency"] == 0.9375
    assert (s["min_light"], s["min_deep"], s["min_rem"]) == (270.0, 90.0, 90.0)
    stages = q(store, "SELECT stage, local_date FROM sleep_stages ORDER BY start_utc")
    assert [r["stage"] for r in stages] == ["LIGHT", "DEEP", "REM", "AWAKE", "LIGHT"]
    assert {r["local_date"] for r in stages} == {"2026-10-13"}


def test_reprocessed_overlapping_sleep_keeps_newest(store):
    old = samples.sleep("old", "2026-10-12T13:00:00Z", "2026-10-12T21:00:00Z", samples.NIGHT_STAGES,
                        update_time="2026-10-13T02:00:00Z")
    new = samples.sleep("new", "2026-10-12T13:10:00Z", "2026-10-12T21:00:00Z", samples.NIGHT_STAGES[1:],
                        update_time="2026-10-13T09:00:00Z")
    ingest(store, "sleep", old, new)
    assert [r["id"] for r in q(store, "SELECT id FROM sleep_sessions")] == ["users/me/dataTypes/sleep/dataPoints/new"]
    assert {r["session_id"] for r in q(store, "SELECT session_id FROM sleep_stages")} == {
        "users/me/dataTypes/sleep/dataPoints/new"}


def test_every_registry_type_has_a_normalizer():
    assert set(NORMALIZERS) == set(BY_NAME)


def test_sleep_rebuild_window_removes_older_session_on_adjacent_date(store):
    # Reprocessing moved the wake time across local midnight: old wakes 23:50 local (10-13), new 00:10 local (10-14).
    old = samples.sleep("old", "2026-10-13T06:00:00Z", "2026-10-13T14:20:00Z",
                        [("LIGHT", "2026-10-13T06:00:00Z", "2026-10-13T14:20:00Z")], update_time="2026-10-13T19:00:00Z")
    new = samples.sleep("new", "2026-10-13T06:10:00Z", "2026-10-13T14:40:00Z",
                        [("DEEP", "2026-10-13T06:10:00Z", "2026-10-13T14:40:00Z")], update_time="2026-10-13T20:00:00Z")
    ingest(store, "sleep", old)
    assert q(store, "SELECT local_date FROM sleep_sessions") == [{"local_date": "2026-10-13"}]
    dt = BY_NAME["sleep"]
    touched = store.upsert_raw([to_raw_row(dt, new)])
    assert touched == {"2026-10-14"}  # only the new point's date; the old row lives on 10-13
    rebuild(store, dt, touched)
    assert q(store, "SELECT id, local_date FROM sleep_sessions") == [
        {"id": "users/me/dataTypes/sleep/dataPoints/new", "local_date": "2026-10-14"}]
    assert {r["session_id"] for r in q(store, "SELECT session_id FROM sleep_stages")} == {
        "users/me/dataTypes/sleep/dataPoints/new"}


def test_sleep_update_time_compared_as_instants_not_strings(store):
    stages = samples.NIGHT_STAGES
    a = samples.sleep("a", "2026-10-12T13:00:00Z", "2026-10-12T21:00:00Z", stages, update_time="2026-10-13T09:00:00Z")
    b = samples.sleep("b", "2026-10-12T13:10:00Z", "2026-10-12T21:00:00Z", stages, update_time="2026-10-13T09:00:00.500Z")
    ingest(store, "sleep", a, b)
    got = q(store, "SELECT id, update_time FROM sleep_sessions")
    assert got == [{"id": "users/me/dataTypes/sleep/dataPoints/b", "update_time": "2026-10-13T09:00:00.500000Z"}]


def test_sleep_dedupe_breaks_ties_and_missing_update_time_by_later_end(store):
    stages = samples.NIGHT_STAGES
    a = samples.sleep("a", "2026-10-12T13:00:00Z", "2026-10-12T20:50:00Z", stages, update_time="2026-10-13T09:00:00Z")
    b = samples.sleep("b", "2026-10-12T13:10:00Z", "2026-10-12T21:00:00Z", stages, update_time="2026-10-13T09:00:00Z")
    c = samples.sleep("c", "2026-10-12T13:00:00Z", "2026-10-12T21:30:00Z", stages)
    del c["sleep"]["updateTime"]
    ingest(store, "sleep", a, b)
    assert [r["id"][-1] for r in q(store, "SELECT id FROM sleep_sessions")] == ["b"]  # equal update times: later end wins
    ingest(store, "sleep", c)
    assert [r["id"][-1] for r in q(store, "SELECT id FROM sleep_sessions")] == ["b"]  # no update time loses to any
    assert q(store, "SELECT update_time FROM sleep_sessions") == [{"update_time": "2026-10-13T09:00:00Z"}]


def test_sleep_dedupe_without_any_update_times_prefers_later_end(store):
    stages = samples.NIGHT_STAGES
    a = samples.sleep("a", "2026-10-12T13:00:00Z", "2026-10-12T20:50:00Z", stages)
    b = samples.sleep("b", "2026-10-12T13:10:00Z", "2026-10-12T21:00:00Z", stages)
    for p in (a, b):
        del p["sleep"]["updateTime"]
    ingest(store, "sleep", a, b)
    assert [r["id"][-1] for r in q(store, "SELECT id FROM sleep_sessions")] == ["b"]
    assert q(store, "SELECT update_time FROM sleep_sessions") == [{"update_time": None}]


# ---- untested normalizers, one table-driven test ----

T = "2026-10-12T23:00:00Z"  # 08:30 local, local date 2026-10-13
T2 = "2026-10-13T00:30:00Z"
COVERAGE = [
    ("oxygen-saturation", samples.sample_point("oxygenSaturation", T, percentage=96.5),
     "samples", {"spo2_pct": 96.5}),
    ("respiratory-rate-sleep-summary",
     samples.sample_point("respiratoryRateSleepSummary", T, fullSleepStats={"breathsPerMinute": 14.5},
                          deepSleepStats={"breathsPerMinute": "13"}, remSleepStats={}),
     "samples", {"resp_full_brpm": 14.5, "resp_deep_brpm": 13.0}),
    ("vo2-max", samples.sample_point("vo2Max", T, vo2Max=42.5), "samples", {"vo2_max": 42.5}),
    ("run-vo2-max", samples.sample_point("runVo2Max", T, runVo2Max=44.0), "samples", {"run_vo2_max": 44.0}),
    ("body-fat", samples.sample_point("bodyFat", T, percentage=18.2), "samples", {"body_fat_pct": 18.2}),
    ("height", samples.sample_point("height", T, heightMillimeters="1801"), "samples", {"height_m": 1.801}),
    ("daily-oxygen-saturation",
     samples.daily_point("dailyOxygenSaturation", "2026-10-13", averagePercentage=95.5,
                         standardDeviationPercentage=1.2, lowerBoundPercentage=93.0, upperBoundPercentage=97.0),
     "daily_metrics", {"spo2_avg_pct": 95.5, "spo2_sd_pct": 1.2, "spo2_lower_pct": 93.0, "spo2_upper_pct": 97.0}),
    ("daily-respiratory-rate", samples.daily_point("dailyRespiratoryRate", "2026-10-13", breathsPerMinute=14.2),
     "daily_metrics", {"resp_rate": 14.2}),
    ("daily-vo2-max",
     samples.daily_point("dailyVo2Max", "2026-10-13", vo2Max=41.0, estimated=True, vo2MaxCovariance=0.1),
     "daily_metrics", {"vo2_max": 41.0, "vo2_max_estimated": 1.0, "vo2_max_covariance": 0.1}),
    ("distance", samples.interval_point("distance", T, T2, millimeters="1001"),
     "activity_intervals", {"distance_m": 1.001}),
    ("sedentary-period", samples.interval_point("sedentaryPeriod", T, T2), "activity_intervals", {"sedentary_min": 90.0}),
    ("activity-level", samples.interval_point("activityLevel", T, "2026-10-12T23:10:00Z", activityLevelType="MODERATE"),
     "activity_intervals", {"level_moderate_min": 10.0}),
    ("active-energy-burned", samples.interval_point("activeEnergyBurned", T, T2, kcal=12.5),
     "activity_intervals", {"active_kcal": 12.5}),
]


@pytest.mark.parametrize("name,point,table,expected", COVERAGE, ids=[c[0] for c in COVERAGE])
def test_normalizer_produces_expected_metrics(store, name, point, table, expected):
    ingest(store, name, point)
    rows = q(store, f"SELECT metric, value, local_date, src_type FROM {table}")
    assert {r["metric"]: r["value"] for r in rows} == expected
    assert {(r["local_date"], r["src_type"]) for r in rows} == {("2026-10-13", name)}


def test_coverage_table_covers_every_previously_untested_normalizer():
    assert len(COVERAGE) == 13 and all(c[0] in NORMALIZERS for c in COVERAGE)


def test_sleep_rebuild_dedupes_against_neighbours_outside_the_written_dates(store):
    # stale wakes 00:10 local on 10-14, newer wakes 23:50 local on 10-13 and overlaps it.
    stale = samples.sleep("stale", "2026-10-13T06:10:00Z", "2026-10-13T14:40:00Z",
                          [("DEEP", "2026-10-13T06:10:00Z", "2026-10-13T14:40:00Z")], update_time="2026-10-13T19:00:00Z")
    newer = samples.sleep("newer", "2026-10-13T06:00:00Z", "2026-10-13T14:20:00Z",
                          [("LIGHT", "2026-10-13T06:00:00Z", "2026-10-13T14:20:00Z")], update_time="2026-10-13T20:00:00Z")
    dt = BY_NAME["sleep"]
    dates = store.upsert_raw([to_raw_row(dt, stale), to_raw_row(dt, newer)])
    assert dates == {"2026-10-13", "2026-10-14"}
    rebuild(store, dt, dates)
    newer_id = "users/me/dataTypes/sleep/dataPoints/newer"
    assert [r["id"] for r in q(store, "SELECT id FROM sleep_sessions")] == [newer_id]

    rebuild(store, dt, ["2026-10-15"])  # writes 14..16, so 10-14 must still see 10-13 to stay de-duplicated
    assert [r["id"] for r in q(store, "SELECT id FROM sleep_sessions")] == [newer_id]
    assert [r["session_id"] for r in q(store, "SELECT session_id FROM sleep_stages")] == [newer_id]


def test_list_method_keys_include_the_source():
    irn = BY_NAME["irregular-rhythm-notification"]
    a = samples.irn("2026-10-12T16:00:00Z", "2026-10-12T16:30:00Z")
    b = samples.irn("2026-10-12T16:00:00Z", "2026-10-12T16:30:00Z")
    a["dataSource"] = {"platform": "FITBIT", "device": {"displayName": "Fitbit Air"}}
    b["dataSource"] = {"platform": "GOOGLE_WEB_API"}
    ka, kb = to_raw_row(irn, a).point_key, to_raw_row(irn, b).point_key
    assert ka == "2026-10-12T16:00:00Z|FITBIT/Fitbit Air" and kb == "2026-10-12T16:00:00Z|GOOGLE_WEB_API"


def test_reconcile_keys_do_not_include_the_source():
    p = samples.heart_rate("2026-10-12T16:00:00Z")
    p["dataSource"] = {"platform": "FITBIT"}
    assert to_raw_row(BY_NAME["heart-rate"], p).point_key == "2026-10-12T16:00:00Z"


def test_data_point_name_is_still_accepted_as_a_fallback():
    p = samples.sleep("n1", "2026-10-12T13:00:00Z", "2026-10-12T21:00:00Z", samples.NIGHT_STAGES)
    p["dataPointName"] = p.pop("name")
    assert to_raw_row(BY_NAME["sleep"], p).point_key == "users/me/dataTypes/sleep/dataPoints/n1"
