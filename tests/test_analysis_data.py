from datetime import datetime, timedelta, timezone

import pytest
from analysis_seed import LOCAL_OFFSET_S, history, seed_hr_minutes, seed_night, seed_sedentary
from fitbit_air.analysis.data import load_inputs
from fitbit_air.analysis.evidence import load_registry
from fitbit_air.analysis.quality import day_quality, night_valid

REG = load_registry()


def test_load_inputs_assembles_series_nights_and_minutes(store):
    history(store, "2026-10-01", 3, rhr=61)
    seed_sedentary(store, "2026-10-02", 10, 2)
    store.save_snapshot("profile", {"age": 26})
    inp = load_inputs(store, "2026-10-01", "2026-10-03")
    assert inp.daily["resting_hr"] == {"2026-10-01": 61.0, "2026-10-02": 61.0, "2026-10-03": 61.0}
    night = inp.nights["2026-10-02"]
    assert night.offset_s == LOCAL_OFFSET_S and night.sleep_type == "STAGES" and night.min_asleep == 450
    assert [s[0] for s in night.stages] == ["AWAKE", "LIGHT"]
    assert len(inp.hr_minutes["2026-10-02"]) == 1440
    assert len(inp.sedentary["2026-10-02"]) == 1
    assert inp.age == 26


def test_minutes_start_limits_only_minute_level_data(store):
    history(store, "2026-10-01", 3)
    for d in ("2026-10-01", "2026-10-03"):
        seed_sedentary(store, d, 10, 2)
    inp = load_inputs(store, "2026-10-01", "2026-10-03", minutes_start="2026-10-03")
    assert set(inp.hr_minutes) == {"2026-10-03"} and set(inp.sedentary) == {"2026-10-03"}
    assert set(inp.nights) == {"2026-10-01", "2026-10-02", "2026-10-03"}
    assert set(inp.daily["resting_hr"]) == {"2026-10-01", "2026-10-02", "2026-10-03"}


def test_day_quality_thresholds():
    assert day_quality(1440, REG) == "good"
    assert day_quality(1008, REG) == "good"      # exactly 70%
    assert day_quality(1000, REG) == "partial"
    assert day_quality(400, REG) == "invalid"


def test_night_validity(store):
    seed_night(store, "2026-10-05")
    seed_night(store, "2026-10-06", asleep_h=2.0)            # too short
    seed_night(store, "2026-10-07", sleep_type="CLASSIC")    # no stages
    seed_hr_minutes(store, "2026-10-05")
    nights = load_inputs(store, "2026-10-05", "2026-10-07").nights
    assert night_valid(nights["2026-10-05"], REG)
    assert not night_valid(nights["2026-10-06"], REG)
    assert not night_valid(nights["2026-10-07"], REG)
    assert not night_valid(None, REG)


def test_missing_offset_inherits_most_common(store, caplog):
    history(store, "2026-10-01", 3)
    store.conn.execute("UPDATE raw_points SET utc_offset = NULL WHERE point_key = 's-2026-10-02'")
    store.conn.commit()
    with caplog.at_level("WARNING"):
        nights = load_inputs(store, "2026-10-01", "2026-10-03").nights
    assert nights["2026-10-02"].offset_s == LOCAL_OFFSET_S
    assert any("2026-10-02" in m for m in caplog.messages)


def test_unparseable_offset_with_no_other_night_falls_back_to_zero(store, caplog):
    seed_night(store, "2026-10-02")
    store.conn.execute("UPDATE raw_points SET utc_offset = 'garbage' WHERE point_key = 's-2026-10-02'")
    store.conn.commit()
    with caplog.at_level("WARNING"):
        nights = load_inputs(store, "2026-10-02", "2026-10-02").nights
    assert nights["2026-10-02"].offset_s == 0
    assert any("2026-10-02" in m for m in caplog.messages)


@pytest.mark.parametrize("raw, expected", [("26.0", 26), (26, 26), ("", None), ("abc", None), (5, None),
                                           (200, None), (None, None)])
def test_age_parsed_defensively(store, raw, expected):
    store.save_snapshot("profile", {"age": raw})
    assert load_inputs(store, "2026-10-01", "2026-10-03").age == expected


def test_age_missing(store):
    assert load_inputs(store, "2026-10-01", "2026-10-03").age is None
    store.save_snapshot("profile", {"name": "x"})
    assert load_inputs(store, "2026-10-01", "2026-10-03").age is None


def test_day_quality_partial_boundary():
    assert day_quality(432, REG) == "partial"   # exactly 30%
    assert day_quality(431, REG) == "invalid"


def test_hr_minutes_are_utc(store):
    seed_hr_minutes(store, "2026-10-02")
    minutes = load_inputs(store, "2026-10-02", "2026-10-02").hr_minutes["2026-10-02"]
    assert minutes[0][0] == datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc)  # local midnight at +09:30
    assert minutes[0][0].utcoffset() == timedelta(0)


def test_keeps_longest_main_sleep_per_wake_date(store):
    seed_night(store, "2026-10-05", asleep_h=7.5)
    start = "2026-10-05T01:00:00Z"
    store.replace_clean("sleep-extra", ["2026-10-05"], {"sleep_sessions": [
        {"id": "s-short", "start_utc": start, "end_utc": "2026-10-05T03:00:00Z", "local_date": "2026-10-05",
         "is_main": 1, "is_nap": 0, "sleep_type": "STAGES", "min_asleep": 100.0, "min_in_period": 120.0}]})
    nights = load_inputs(store, "2026-10-05", "2026-10-05").nights
    assert nights["2026-10-05"].session_id == "s-2026-10-05"
    # and when the longer one is listed second
    store.replace_clean("sleep-extra", ["2026-10-05"], {"sleep_sessions": [
        {"id": "s-long", "start_utc": "2026-10-05T06:00:00Z", "end_utc": "2026-10-05T16:00:00Z",
         "local_date": "2026-10-05", "is_main": 1, "is_nap": 0, "sleep_type": "STAGES", "min_asleep": 580.0,
         "min_in_period": 600.0}]})
    assert load_inputs(store, "2026-10-05", "2026-10-05").nights["2026-10-05"].session_id == "s-long"
