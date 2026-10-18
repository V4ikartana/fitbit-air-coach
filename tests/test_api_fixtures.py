"""Synthetic API payloads (tests/fixtures/api) normalise like real ones and carry none of the real values."""
import importlib.util
import json
import re
from pathlib import Path

import pytest

from fitbit_air.normalize import NORMALIZERS, to_raw_row
from fitbit_air.registry import BY_NAME

HERE = Path(__file__).parent
API = sorted((HERE / "fixtures" / "api").glob("*.json"))
REAL = HERE / "fixtures" / "real"
SCRIPT = HERE.parent / "scripts" / "make_fixtures.py"


def _make_fixtures():
    spec = importlib.util.spec_from_file_location("make_fixtures", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def test_one_synthetic_fixture_per_real_type():
    assert API, "run scripts/make_fixtures.py"
    if REAL.is_dir():
        assert {p.stem for p in API} == {p.stem for p in REAL.glob("*.json")}


@pytest.mark.parametrize("path", API, ids=lambda p: p.stem)
def test_synthetic_payload_normalizes(path):
    dt = BY_NAME[path.stem]
    point = _load(path)
    raw = to_raw_row(dt, point)
    assert raw.local_date == "2030-01-15"
    rows = NORMALIZERS[dt.name](point[dt.field], raw.point_key, raw.local_date)
    assert any(rows.values()), f"{dt.name} produced no rows from its synthetic payload"
    for table_rows in rows.values():
        assert all(r["local_date"] == raw.local_date for r in table_rows)


@pytest.mark.parametrize("path", API, ids=lambda p: p.stem)
def test_anonymising_a_synthetic_payload_changes_nothing(path):
    """Values depend on structure only, so the committed fixtures are a fixed point (needs no real data)."""
    assert _make_fixtures().anonymise(path.stem, _load(path)) == _load(path)


def test_every_utc_offset_is_zero():
    offsets = [(p.stem, key, value) for p in API for key, value in _walk(_load(p))
               if key.lower().rsplit(".", 1)[-1].endswith("utcoffset")]
    assert offsets, "no offsets found: the walk is broken"
    assert [o for o in offsets if o[2] != "0s"] == []


def test_sleep_summary_agrees_with_its_timeline():
    sleep = _load(HERE / "fixtures" / "api" / "sleep.json")["sleep"]
    minutes = [int(round((_utc(s["endTime"]) - _utc(s["startTime"])) / 60)) for s in sleep["stages"]]
    summary = sleep["summary"]
    asleep = sum(m for m, s in zip(minutes, sleep["stages"]) if s["type"] != "AWAKE")
    assert int(summary["minutesInSleepPeriod"]) == sum(minutes)
    assert int(summary["minutesAsleep"]) == asleep
    assert int(summary["minutesAwake"]) == sum(minutes) - asleep
    for entry in summary["stagesSummary"]:
        of_type = [m for m, s in zip(minutes, sleep["stages"]) if s["type"] == entry["type"]]
        assert (int(entry["minutes"]), int(entry["count"])) == (sum(of_type), len(of_type))
    assert (_utc(sleep["interval"]["endTime"]) - _utc(sleep["interval"]["startTime"])) / 60 == sum(minutes)


def test_exercise_durations_are_consistent():
    ex = _load(HERE / "fixtures" / "api" / "exercise.json")["exercise"]
    interval_s = _utc(ex["interval"]["endTime"]) - _utc(ex["interval"]["startTime"])
    active_s = float(ex["activeDuration"].rstrip("s"))
    zones_s = sum(float(v.rstrip("s")) for v in ex["metricsSummary"]["heartRateZoneDurations"].values())
    assert interval_s == 30 * 60 and active_s == 28 * 60 and zones_s <= active_s


def test_unknown_numeric_path_raises_and_names_it():
    with pytest.raises(ValueError, match="mystery"):
        _make_fixtures().anonymise("steps", {"steps": {"mystery": 7}})


_INTERVAL = {"startTime": "2026-01-02T03:04:05Z", "startUtcOffset": "7200s", "endTime": "2026-01-02T03:05:05Z",
             "endUtcOffset": "7200s"}


def test_number_inside_an_interval_dict_is_neutralised_or_raises():
    mf = _make_fixtures()
    with pytest.raises(ValueError, match="distanceMillimeters"):
        mf.anonymise("steps", {"steps": {"interval": {**_INTERVAL, "distanceMillimeters": "5"}}})
    out = mf.anonymise("active-energy-burned", {"activeEnergyBurned": {"interval": {**_INTERVAL, "kcal": 9.0}}})
    assert out["activeEnergyBurned"]["interval"]["kcal"] == 3.5  # the neutral value, not the input


def test_free_text_and_unknown_timestamps_raise():
    mf = _make_fixtures()
    for body in ({"note": "free text"}, {"interval": {**_INTERVAL, "label": "free text"}},
                 {"note": "2026-01-02T03:04:05+02:00"}):
        with pytest.raises(ValueError, match="note|label"):
            mf.anonymise("steps", {"steps": body})
    assert mf.anonymise("steps", {"steps": {"state": "SOME_ENUM_9"}})["steps"]["state"] == "SOME_ENUM_9"


def test_dict_keys_must_be_camel_case_identifiers():
    mf = _make_fixtures()
    for key in ("Bad Key", "BadKey", "bad_key", "9lives", "serial FB512ABC123"):
        with pytest.raises(ValueError, match="key"):
            mf.anonymise("steps", {"steps": {key: "ENUM"}})
        with pytest.raises(ValueError, match="key"):
            mf.anonymise("steps", {"steps": {"interval": {**_INTERVAL, key: "ENUM"}}})
        with pytest.raises(ValueError, match="key"):
            mf.anonymise("heart-rate", {"heartRate": {"sampleTime": {"physicalTime": "2026-01-02T03:04:05Z", key: 1}}})
    assert mf.anonymise("steps", {"steps": {"someKey2": "ENUM"}})["steps"] == {"someKey2": "ENUM"}


def test_enum_strings_are_words_with_underscored_segments_not_serials():
    mf = _make_fixtures()
    for value in ("FB512ABC123", "AB12", "9LIVES", "_X", "X_", "A__B"):
        with pytest.raises(ValueError, match="state"):
            mf.anonymise("steps", {"steps": {"state": value}})
    for value in ("FAT_BURN", "WITH_SLEEP", "SOME_ENUM_9", "STAGES"):
        assert mf.anonymise("steps", {"steps": {"state": value}})["steps"]["state"] == value


def test_sleep_summary_entries_are_validated_and_rebuilt_from_the_hypnogram():
    mf = _make_fixtures()
    template = _load(HERE / "fixtures" / "api" / "sleep.json")

    def with_entry(**changes):
        point = json.loads(json.dumps(template))
        point["sleep"]["summary"]["stagesSummary"][0].update(changes)
        return point

    for changes, where in (({"note": "free text"}, "note"), ({"type": "free text"}, "type"),
                           ({"type": "FB512ABC123"}, "type"), ({"extra": 7}, "extra")):
        with pytest.raises(ValueError, match=where):
            mf.anonymise("sleep", with_entry(**changes))
    out = mf.anonymise("sleep", with_entry(type="LIGHT"))["sleep"]["summary"]["stagesSummary"]
    assert [e["type"] for e in out] == list(dict.fromkeys(mf.HYPNOGRAM))  # one entry per distinct hypnogram type


def test_timestamps_with_an_offset_are_rewritten_to_the_synthetic_utc_time():
    point = {"heartRate": {"sampleTime": {"physicalTime": "2026-01-02T03:04:05+02:00", "utcOffset": "7200s"},
                           "beatsPerMinute": "80"}}
    out = _make_fixtures().anonymise("heart-rate", point)["heartRate"]
    assert out["sampleTime"] == {"physicalTime": "2030-01-15T00:00:00Z", "utcOffset": "0s"}
    ex = {"exercise": {"interval": {**_INTERVAL, "endTime": "2026-01-02T03:35:05.250+02:00"},
                       "createTime": "2026-01-02T03:36:05.123456-05:00", "exerciseType": "RUNNING",
                       "displayName": "Some free text"}}
    out = _make_fixtures().anonymise("exercise", ex)["exercise"]
    assert out["interval"]["endTime"] == "2030-01-15T00:30:00.000Z"
    assert out["createTime"] == "2030-01-15T00:31:00.000000Z"
    assert out["displayName"] == "Run"  # from the exercise type, never the real text


def test_active_zone_minutes_follow_the_zone():
    mf = _make_fixtures()
    for zone, expected in (("FAT_BURN", "1"), ("CARDIO", "2"), ("PEAK", "2")):
        body = {"interval": _INTERVAL, "heartRateZone": zone, "activeZoneMinutes": "7"}
        assert mf.anonymise("active-zone-minutes", {"activeZoneMinutes": body})["activeZoneMinutes"][
            "activeZoneMinutes"] == expected


def test_sleep_uses_the_canonical_hypnogram():
    mf = _make_fixtures()
    sleep = _load(HERE / "fixtures" / "api" / "sleep.json")["sleep"]
    assert tuple(s["type"] for s in sleep["stages"]) == mf.HYPNOGRAM
    assert len(sleep["shortAwakenings"]) == mf.N_AWAKENINGS


def _utc(value: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


@pytest.mark.skipif(not REAL.is_dir(), reason="real fixtures are private")
def test_no_real_identifier_or_timestamp_leaks():
    real_text = " ".join(p.read_text(encoding="utf-8") for p in REAL.glob("*.json"))
    tokens = set(re.findall(r"dataPoints/[\w-]+|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", real_text))
    api_text = " ".join(p.read_text(encoding="utf-8") for p in API)
    leaked = [t for t in tokens if t in api_text]
    if leaked:  # counts only: the tokens themselves are real
        pytest.fail(f"{len(leaked)} real token(s) found in the synthetic fixtures", pytrace=False)


@pytest.mark.skipif(not REAL.is_dir(), reason="real fixtures are private")
def test_no_real_value_survives_except_fixed_by_structure():
    """Compared as booleans, reporting paths only. A synthetic leaf may equal its real twin only if it is an enum
    (API vocabulary), a civil date/time part (the synthetic time is derived, so a part can coincide), an exercise
    displayName (a generic label chosen from the exercise type), or a per-minute active-minute count, which a
    one-minute interval forces to be 1."""
    civil_part = re.compile(r"civil\w*Time\.(date\.(year|month|day)|time\.(hours|minutes|seconds|nanos))$"
                            r"|\.date\.(year|month|day)$")
    per_minute = re.compile(r"activeMinutesByActivityLevel\[\d+\]\.activeMinutes$"
                            r"|\.activeZoneMinutes\.activeZoneMinutes$")
    enum = re.compile(r"[A-Z][A-Z0-9_]*")
    suspicious = []
    for real_path in sorted(REAL.glob("*.json")):
        real = dict(_walk(_load(real_path)))
        synth = dict(_walk(_load(HERE / "fixtures" / "api" / real_path.name)))
        for key, value in real.items():
            if isinstance(value, bool) or value is None or key not in synth or synth[key] != value:
                continue
            if (isinstance(value, str) and enum.fullmatch(value)) or civil_part.search(key) \
                    or per_minute.search(key) or key.endswith(".displayName"):
                continue
            suspicious.append(f"{real_path.stem}{key}")
    if suspicious:  # paths only: the values are real
        pytest.fail(f"{len(suspicious)} synthetic leaf value(s) equal the real one: " + ", ".join(suspicious),
                    pytrace=False)


def test_make_fixtures_defaults_come_from_the_repo_root():
    mf = _make_fixtures()
    assert mf.DEFAULT_REAL == HERE.resolve() / "fixtures" / "real"
    assert mf.DEFAULT_OUT == HERE.resolve() / "fixtures" / "api"


def test_make_fixtures_main_takes_arguments(tmp_path):
    mf = _make_fixtures()
    real = tmp_path / "real"
    real.mkdir()
    synthetic = HERE / "fixtures" / "api" / "sleep.json"
    (real / "sleep.json").write_text(synthetic.read_text(encoding="utf-8"), encoding="utf-8")
    assert mf.main(["--real", str(real), "--out", str(tmp_path / "out")]) == 0
    assert _load(tmp_path / "out" / "sleep.json") == _load(synthetic)  # anonymising synthetic data is a no-op


def test_make_fixtures_refuses_a_missing_real_folder(tmp_path, capsys):
    mf = _make_fixtures()
    assert mf.main(["--real", str(tmp_path / "none"), "--out", str(tmp_path / "out")]) == 2
    assert "not found" in capsys.readouterr().err and not (tmp_path / "out").exists()
