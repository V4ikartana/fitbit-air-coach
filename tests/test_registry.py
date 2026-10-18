from datetime import datetime, timedelta, timezone

import pytest

from fitbit_air.registry import BY_NAME, REGISTRY, DataType, Kind
from fitbit_air.timeutil import UTC

START = datetime(2026, 10, 1, 6, tzinfo=UTC)
END = datetime(2026, 10, 2, 6, tzinfo=UTC)


def test_field_is_camel_case_of_name():
    assert BY_NAME["heart-rate"].field == "heartRate"
    assert BY_NAME["daily-vo2-max"].field == "dailyVo2Max"
    assert BY_NAME["irregular-rhythm-notification"].field == "irregularRhythmNotification"


def test_interval_filter():
    assert BY_NAME["steps"].filter_for(START, END) == (
        'steps.interval.start_time >= "2026-10-01T06:00:00Z" AND steps.interval.start_time < "2026-10-02T06:00:00Z"'
    )


def test_sample_filter():
    assert BY_NAME["heart-rate"].filter_for(START, END) == (
        'heart_rate.sample_time.physical_time >= "2026-10-01T06:00:00Z" '
        'AND heart_rate.sample_time.physical_time < "2026-10-02T06:00:00Z"'
    )


def test_daily_filter_is_widened_by_a_day_each_side():
    assert BY_NAME["daily-resting-heart-rate"].filter_for(START, END) == (
        'daily_resting_heart_rate.date >= "2026-09-30" AND daily_resting_heart_rate.date < "2026-10-03"'
    )


def test_session_filter_uses_widened_civil_time():
    assert BY_NAME["exercise"].filter_for(START, END) == (
        'exercise.interval.civil_start_time >= "2026-09-30T06:00:00" '
        'AND exercise.interval.civil_start_time < "2026-10-03T06:00:00"'
    )


def test_sleep_filter_uses_end_time():
    assert BY_NAME["sleep"].filter_for(START, END) == (
        'sleep.interval.end_time >= "2026-10-01T06:00:00Z" AND sleep.interval.end_time < "2026-10-02T06:00:00Z"'
    )


def test_point_keys():
    assert BY_NAME["daily-resting-heart-rate"].point_key({"date": {"year": 2026, "month": 10, "day": 3}}) == "2026-10-03"
    assert BY_NAME["heart-rate"].point_key({"sampleTime": {"physicalTime": "2026-10-03T01:00:00Z"}}) == "2026-10-03T01:00:00Z"
    azm = {"interval": {"startTime": "2026-10-03T01:00:00Z"}, "heartRateZone": "CARDIO"}
    assert BY_NAME["active-zone-minutes"].point_key(azm) == "2026-10-03T01:00:00Z|CARDIO"
    name = "users/me/dataTypes/sleep/dataPoints/abc"
    assert BY_NAME["sleep"].point_key({"interval": {"startTime": "x"}}, name) == name
    # name is ignored for types not keyed by name
    hr = {"sampleTime": {"physicalTime": "2026-10-03T01:00:00Z"}}
    assert BY_NAME["heart-rate"].point_key(hr, "users/me/dataTypes/heart-rate/dataPoints/x") == "2026-10-03T01:00:00Z"
    # name-keyed type without a name falls back to the time key
    assert BY_NAME["sleep"].point_key({"interval": {"startTime": "2026-10-03T01:00:00Z"}}) == "2026-10-03T01:00:00Z"


def test_point_key_timestamps_are_canonical():
    hr = BY_NAME["heart-rate"]

    def key(ts):
        return hr.point_key({"sampleTime": {"physicalTime": ts}})

    assert key("2026-10-03T01:00:00.000Z") == key("2026-10-03T01:00:00Z") == "2026-10-03T01:00:00Z"
    assert key("2026-10-03T10:30:00+09:30") == "2026-10-03T01:00:00Z"
    assert key("2026-10-03T01:00:00.25Z") == "2026-10-03T01:00:00.250000Z"
    assert BY_NAME["steps"].point_key({"interval": {"startTime": "2026-10-03T10:30:00+09:30"}}) == "2026-10-03T01:00:00Z"


def test_missing_discriminator_is_an_error():
    with pytest.raises(KeyError, match="active-zone-minutes: missing heartRateZone"):
        BY_NAME["active-zone-minutes"].point_key({"interval": {"startTime": "2026-10-03T01:00:00Z"}})


def test_filter_rejects_naive_datetimes():
    with pytest.raises(ValueError, match="naive"):
        BY_NAME["steps"].filter_for(datetime(2026, 10, 1, 6), END)
    with pytest.raises(ValueError, match="naive"):
        BY_NAME["steps"].filter_for(START, datetime(2026, 10, 2, 6))


def test_filter_rejects_empty_or_inverted_window():
    with pytest.raises(ValueError):
        BY_NAME["steps"].filter_for(END, START)
    with pytest.raises(ValueError):
        BY_NAME["steps"].filter_for(START, START)


def test_filter_normalises_offset_datetimes_to_utc():
    local = timezone(timedelta(hours=5, minutes=30))
    start, end = START.astimezone(local), END.astimezone(local)
    for dt in REGISTRY:
        assert dt.filter_for(start, end) == dt.filter_for(START, END)


def test_registry_is_consistent():
    names = [dt.name for dt in REGISTRY]
    assert len(names) == len(set(names)) == 27
    assert all(dt.method in ("reconcile", "list") for dt in REGISTRY)
    assert BY_NAME["irregular-rhythm-notification"].method == "list"
    assert BY_NAME["sleep"].page_size == BY_NAME["exercise"].page_size == 25
    assert {dt.name for dt in REGISTRY if dt.keyed_by_name} == {"sleep", "exercise"}
    assert isinstance(BY_NAME["steps"], DataType) and BY_NAME["steps"].kind is Kind.INTERVAL


def test_only_heart_rate_zones_skip_backfill():
    assert [dt.name for dt in REGISTRY if not dt.backfill] == ["daily-heart-rate-zones"]
