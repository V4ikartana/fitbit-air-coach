from datetime import datetime

import pytest

from fitbit_air.timeutil import (
    UTC, canonical_ts, civil_str, date_str, iso_utc, minutes_between, num, parse_duration_s, parse_ts,
    require_aware,
)


def test_parse_ts_zulu():
    assert parse_ts("2026-10-12T20:00:00Z") == datetime(2026, 10, 12, 20, tzinfo=UTC)


def test_parse_ts_nanoseconds_truncated():
    assert parse_ts("2026-10-12T20:00:00.123456789Z").microsecond == 123456


def test_parse_ts_offset_converted_to_utc():
    assert parse_ts("2026-10-13T05:30:00+09:30") == datetime(2026, 10, 12, 20, tzinfo=UTC)


def test_parse_ts_rejects_garbage():
    with pytest.raises(ValueError):
        parse_ts("yesterday")


def test_iso_utc():
    assert iso_utc(datetime(2026, 10, 12, 20, 5, 9, 999, tzinfo=UTC)) == "2026-10-12T20:05:09Z"


def test_date_str():
    assert date_str({"year": 2026, "month": 3, "day": 7}) == "2026-03-07"


def test_civil_str_defaults_missing_time_parts():
    assert civil_str({"date": {"year": 2026, "month": 3, "day": 7}, "time": {"hours": 6}}) == "2026-03-07T06:00:00"


def test_minutes_between():
    assert minutes_between("2026-10-12T20:00:00Z", "2026-10-12T21:30:00Z") == 90


def test_parse_duration_s():
    assert parse_duration_s("1800s") == 1800.0
    assert parse_duration_s("1.5s") == 1.5


def test_num():
    assert num("58") == 58.0
    assert num(None) is None


def test_iso_utc_rejects_naive():
    with pytest.raises(ValueError, match="naive"):
        iso_utc(datetime(2026, 10, 12, 20))


def test_require_aware():
    aware = datetime(2026, 10, 12, 20, tzinfo=UTC)
    assert require_aware(aware) is aware
    with pytest.raises(ValueError, match="naive"):
        require_aware(datetime(2026, 10, 12, 20))


def test_canonical_ts():
    assert canonical_ts("2026-10-03T01:00:00.000Z") == "2026-10-03T01:00:00Z"
    assert canonical_ts("2026-10-03T10:30:00+09:30") == "2026-10-03T01:00:00Z"
    assert canonical_ts("2026-10-03T01:00:00.25Z") == "2026-10-03T01:00:00.250000Z"
