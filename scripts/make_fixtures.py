"""Derive synthetic API fixtures from real ones: same keys, same value types, neutral fixed values.

Usage: uv run python scripts/make_fixtures.py [--real tests/fixtures/real] [--out tests/fixtures/api]
(defaults are resolved from the repo root; exit 2 when the real-fixture folder is missing)

Only the *structure* of a real payload is used (keys, value types, list lengths, the order of enum values, the number
of fractional digits). No value is scaled or shifted from the real one, so nothing real can be recovered from the
output, and anonymising an already-anonymised payload gives the same payload (the tests rely on that).
- Every timestamp is rebuilt from 2030-01-15T00:00:00Z: intervals get a fixed length (1 minute, 30 for an exercise,
  and for sleep the sum of the stage lengths), and sub-second digits are written as zeros. Every UTC offset is "0s",
  every civil date/time is recomputed from its UTC partner, and a daily payload's own date is 2030-01-15.
  Offset 0 means these fixtures do not exercise local-date logic; test_normalize.py and tests/samples.py cover that.
- Sleep uses a fixed canonical hypnogram (HYPNOGRAM: 12 stages with the API's own stage names) and two short
  awakenings; each stage lasts 10, 20 or 30 minutes by index. Only the shape (keys) is copied from the first real
  stage and awakening. Every summary minute and count is recomputed from that timeline, so summaries agree with it.
- An exercise lasts 30 minutes with a 28-minute active duration and zone durations that add up to it.
- Numbers (and numeric / duration strings) take a fixed value from NEUTRAL, keyed by path suffix and then by leaf key,
  keeping the original type and fractional digits. A numeric path that is not in the table raises ValueError.
- dataPointName ids become users/me/dataTypes/<type>/dataPoints/synthetic-<n>.
- Every dict key must be a camelCase identifier (KEY) or ValueError names it.
- Only enum-shaped strings ("STAGES", "AWAKE": API vocabulary) are kept. A displayName comes from the exercise
  type (DISPLAY_NAMES). Any other string raises ValueError naming its path, so free text can never pass through.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REAL = ROOT / "tests" / "fixtures" / "real"
DEFAULT_OUT = ROOT / "tests" / "fixtures" / "api"
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
ENUM = re.compile(r"^[A-Z]+(?:_[A-Z0-9]+)*$")  # FAT_BURN, WITH_SLEEP, SOME_ENUM_9; never a serial
KEY = re.compile(r"^[a-z][A-Za-z0-9]*$")  # every JSON key in the real payloads has this shape
NUM_STR = re.compile(r"^-?\d+(\.\d+)?$")
DURATION = re.compile(r"^-?\d+(\.\d+)?s$")
TARGET = datetime(2030, 1, 15, tzinfo=timezone.utc)
INTERVAL_S = {"exercise": 30 * 60}  # every other interval lasts a minute
DEFAULT_INTERVAL_S = 60
STAGE_MINUTES = (10, 20, 30)  # stage i lasts STAGE_MINUTES[i % 3]
HYPNOGRAM = ("AWAKE", "LIGHT", "DEEP", "LIGHT", "REM", "LIGHT", "AWAKE", "LIGHT", "DEEP", "LIGHT", "REM", "AWAKE")
N_AWAKENINGS, AWAKENING_S = 2, 120
ZONE_MINUTES = {"FAT_BURN": 1, "CARDIO": 2, "PEAK": 2}  # active zone minutes per minute spent in a zone
DISPLAY_NAMES = {"WALKING": "Walk", "RUNNING": "Run", "BIKING": "Bike", "SWIMMING": "Swim", "HIKING": "Hike",
                 "WEIGHTLIFTING": "Weights", "YOGA": "Yoga", "TREADMILL": "Treadmill", "ELLIPTICAL": "Elliptical"}
HR_ZONES = {"LIGHT": (98, 116), "MODERATE": (117, 135), "VIGOROUS": (136, 155), "PEAK": (156, 190)}

# Fixed, plausible values. Keys are dotted path suffixes (list indexes removed); the longest matching suffix wins,
# so "steps.count" and "metricsSummary.steps" are different fields, and a bare leaf key is the fallback.
NEUTRAL: dict[str, float] = {
    "weightGrams": 75000,
    "heightMillimeters": 1750,
    "dailyRestingHeartRate.beatsPerMinute": 62,
    "heartRate.beatsPerMinute": 72,
    "steps.count": 40,
    "distance.millimeters": 28000,
    "kcal": 3.5,
    "activeMinutesByActivityLevel.activeMinutes": 1,
    "averageHeartRateVariabilityMilliseconds": 45,
    "nonRemHeartRateBeatsPerMinute": 58,
    "entropy": 2.5,
    "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds": 50.0,
    "rootMeanSquareOfSuccessiveDifferencesMilliseconds": 40,
    "highFrequencyPower": 400,
    "lowFrequencyPower": 600,
    "averagePercentage": 96.5,
    "lowerBoundPercentage": 95.0,
    "upperBoundPercentage": 98.0,
    "oxygenSaturation.percentage": 96.5,
    "breathsPerMinute": 14.5,  # the daily value and every sleep stage
    "standardDeviation": 0.6,
    "signalToNoise": 5.0,
    "nightlyTemperatureCelsius": 33.6,
    "baselineTemperatureCelsius": 33.5,
    "relativeNightlyStddev30dCelsius": 0.3,
    # an exercise: 28 active minutes, 2 km, zone durations that add up to the 1680 active seconds
    "metricsSummary.caloriesKcal": 150,
    "metricsSummary.distanceMillimeters": 2000000,
    "metricsSummary.steps": 3000,
    "metricsSummary.averagePaceSecondsPerMeter": 0.84,
    "metricsSummary.averageHeartRateBeatsPerMinute": 110,
    "heartRateZoneDurations.lightTime": 1200,
    "heartRateZoneDurations.moderateTime": 360,
    "heartRateZoneDurations.vigorousTime": 90,
    "heartRateZoneDurations.peakTime": 30,
    "activeDuration": 1680,
}


def _stage_minutes(index: int) -> int:
    return STAGE_MINUTES[index % len(STAGE_MINUTES)]


def _fmt(moment: datetime, like: str) -> str:
    """RFC 3339 in UTC for a whole-second instant, with as many zero fractional digits as `like` has."""
    fraction = re.search(r"\.(\d+)", like)
    digits = len(fraction.group(1)) if fraction else 0
    return f"{moment:%Y-%m-%dT%H:%M:%S}" + ("." + "0" * digits if digits else "") + "Z"


def _civil(moment: datetime, like: dict) -> dict:
    """Civil object (proto3 JSON style: zero time fields omitted) for a UTC instant at offset 0."""
    fields = (("hours", moment.hour), ("minutes", moment.minute), ("seconds", moment.second))
    time = {k: v for k, v in fields if v}
    if "nanos" in (like.get("time") or {}):
        time["nanos"] = 0
    return {"date": {"year": moment.year, "month": moment.month, "day": moment.day}, "time": time}


def _lookup(path: str) -> float:
    parts = [p for p in path.split(".") if p]
    for n in range(len(parts), 0, -1):
        key = ".".join(parts[-n:])
        if key in NEUTRAL:
            return NEUTRAL[key]
    raise ValueError(f"no neutral value for numeric path {'.'.join(parts)!r}; add it to NEUTRAL in make_fixtures.py")


def _like(original, value: float):
    """`value` in the type (and fractional digits) of `original`: int, float, numeric string or "<n>s" duration."""
    if isinstance(original, int):
        return int(round(value))
    if isinstance(original, float):
        return float(value)
    decimals = len(original.split(".")[1].rstrip("s")) if "." in original else 0
    return f"{value:.{decimals}f}" + ("s" if original.endswith("s") else "")


def _is_number(o) -> bool:
    if isinstance(o, bool):
        return False
    if isinstance(o, (int, float)):
        return True
    return isinstance(o, str) and bool(NUM_STR.match(o) or DURATION.match(o))


def _check_key(key: str, path: str) -> None:
    if not KEY.match(key):
        raise ValueError(f"unexpected key at path {path.lstrip('.')!r}: keys must be camelCase identifiers")


def _interval(orig: dict, start: datetime, end: datetime, conv, path: str, anchors: dict | None = None) -> dict:
    """An interval-shaped dict (startTime/endTime + offsets, maybe civil times and create/update times). Every other
    key goes through `conv`, so a number or string in here is neutralised (or raises) like anywhere else."""
    out = {}
    for k, v in orig.items():
        _check_key(k, path)
        if k == "startTime":
            out[k] = _fmt(start, v)
        elif k == "endTime":
            out[k] = _fmt(end, v)
        elif k.lower().endswith("utcoffset"):
            out[k] = "0s"
        elif k == "civilStartTime":
            out[k] = _civil(start, v)
        elif k == "civilEndTime":
            out[k] = _civil(end, v)
        elif k in ("createTime", "updateTime") and anchors:
            out[k] = _fmt(anchors[k], v)
        else:
            out[k] = conv(v, f"{path}.{k}")
    return out


def anonymise(dtype: str, point: dict) -> dict:
    """The synthetic twin of one real data point (the whole {"dataPointName": ..., "<field>": {...}} object)."""
    has_stages = bool((point.get("sleep") or {}).get("stages"))
    total_s = sum(_stage_minutes(i) for i in range(len(HYPNOGRAM) if has_stages else 0)) * 60
    anchor = TARGET + timedelta(seconds=total_s if dtype == "sleep" else INTERVAL_S.get(dtype, DEFAULT_INTERVAL_S))
    anchors = {"createTime": anchor + timedelta(seconds=60), "updateTime": anchor + timedelta(seconds=120)}
    counter = {"n": 0}

    def sleep_body(o: dict, path: str) -> dict:
        stages_src, awakenings_src = o.get("stages") or [], o.get("shortAwakenings") or []
        types = HYPNOGRAM if stages_src else ()
        starts, t = [], TARGET
        for i in range(len(types)):
            starts.append(t)
            t += timedelta(minutes=_stage_minutes(i))
        by_type: dict[str, list[int]] = {}
        for i, stage_type in enumerate(types):
            by_type.setdefault(stage_type, []).append(_stage_minutes(i))
        awake, period = sum(by_type.get("AWAKE", [])), total_s // 60
        # time to fall asleep / after waking: a leading / trailing AWAKE stage
        computed = {"minutesInSleepPeriod": period, "minutesAwake": awake, "minutesAsleep": period - awake,
                    "minutesToFallAsleep": _stage_minutes(0) if types and types[0] == "AWAKE" else 0,
                    "minutesAfterWakeUp": _stage_minutes(len(types) - 1) if types and types[-1] == "AWAKE" else 0}

        def stages_summary(entries: list, where: str) -> list:
            """One entry per distinct hypnogram type; only the key names come from the real entry. Every real value
            other than minutes/count (so `type` too) goes through conv, which enforces the enum rule."""
            for e in entries:
                for k, val in e.items():
                    _check_key(k, where)
                    if k in ("minutes", "count"):
                        if not _is_number(val):
                            raise ValueError(f"unexpected value at path {(where + '.' + k).lstrip('.')!r}")
                    else:
                        conv(val, f"{where}.{k}")
            if not entries:
                return []
            shape = entries[0]
            return [{k: (t if k == "type" else _like(shape[k], sum(by_type[t])) if k == "minutes"
                         else _like(shape[k], len(by_type[t])) if k == "count" else conv(shape[k], f"{where}.{k}"))
                     for k in shape} for t in dict.fromkeys(types)]

        def summary(sv: dict) -> dict:
            out = {}
            for sk, v in sv.items():
                if sk == "stagesSummary":
                    out[sk] = stages_summary(v, f"{path}.summary.stagesSummary")
                elif sk in computed:
                    out[sk] = _like(v, computed[sk])
                else:
                    out[sk] = conv(v, f"{path}.summary.{sk}")
            return out

        out = {}
        for k, v in o.items():
            if k == "interval":
                out[k] = _interval(v, TARGET, TARGET + timedelta(seconds=total_s), conv, f"{path}.interval")
            elif k == "stages":  # keys of the first real stage, canonical types and times
                out[k] = [_interval({**v[0], "type": stage_type}, starts[i],
                                    starts[i] + timedelta(minutes=_stage_minutes(i)), conv, f"{path}.stages", anchors)
                          for i, stage_type in enumerate(types)]
            elif k == "shortAwakenings":  # two of them, spread evenly through the night
                step = total_s // (N_AWAKENINGS + 1)
                out[k] = [_interval(awakenings_src[0], TARGET + timedelta(seconds=step * (j + 1)),
                                    TARGET + timedelta(seconds=step * (j + 1) + AWAKENING_S), conv,
                                    f"{path}.shortAwakenings") for j in range(N_AWAKENINGS)] if awakenings_src else []
            elif k == "summary":
                out[k] = summary(v)
            elif k in ("createTime", "updateTime"):
                out[k] = _fmt(anchors[k], v)
            else:
                out[k] = conv(v, f"{path}.{k}")
        return out

    def conv(o, path: str):
        if isinstance(o, dict):
            for key in o:
                _check_key(key, path)
            if "stages" in o and "interval" in o:
                return sleep_body(o, path)
            if "startTime" in o and "endTime" in o:
                return _interval(o, TARGET, TARGET + timedelta(seconds=INTERVAL_S.get(dtype, DEFAULT_INTERVAL_S)),
                                 conv, path, anchors)
            if "physicalTime" in o:  # a sample's time
                return {k: (_fmt(TARGET, v) if k == "physicalTime" else "0s" if k.lower().endswith("utcoffset")
                            else _civil(TARGET, v) if k == "civilTime" else conv(v, f"{path}.{k}"))
                        for k, v in o.items()}
            out = {}
            for k, v in o.items():
                if k == "date" and isinstance(v, dict):  # a daily payload's own date
                    out[k] = {"year": TARGET.year, "month": TARGET.month, "day": TARGET.day}
                elif k == "dataPointName" and isinstance(v, str):
                    counter["n"] += 1
                    out[k] = f"users/me/dataTypes/{dtype}/dataPoints/synthetic-{counter['n']}"
                elif k in ("minBeatsPerMinute", "maxBeatsPerMinute") and "heartRateZoneType" in o:
                    low, high = HR_ZONES[o["heartRateZoneType"]]
                    out[k] = _like(v, low if k.startswith("min") else high)
                elif k == "activeZoneMinutes" and "heartRateZone" in o:  # one interval minute in that zone
                    out[k] = _like(v, ZONE_MINUTES.get(o["heartRateZone"], 0))
                elif k == "displayName" and isinstance(v, str) and isinstance(o.get("exerciseType"), str):
                    kind = o["exerciseType"]
                    out[k] = DISPLAY_NAMES.get(kind, kind.replace("_", " ").title())
                else:
                    out[k] = conv(v, f"{path}.{k}")
            return out
        if isinstance(o, list):
            return [conv(v, f"{path}[]") for v in o]
        if o is None or isinstance(o, bool):
            return o
        clean = path.replace("[]", "")
        if isinstance(o, str) and TS.match(o):
            leaf = clean.rsplit(".", 1)[-1]
            if leaf in anchors:
                return _fmt(anchors[leaf], o)
            raise ValueError(f"no synthetic time for timestamp path {clean.lstrip('.')!r}")
        if _is_number(o):
            return _like(o, _lookup(clean))
        if isinstance(o, str) and ENUM.match(o):
            return o
        raise ValueError(f"unexpected string at path {clean.lstrip('.')!r}: only enum-shaped values are kept")

    return conv(point, "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Derive synthetic API fixtures from the private real ones.")
    ap.add_argument("--real", default=str(DEFAULT_REAL), help="real fixtures (default: %(default)s)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="where to write (default: %(default)s)")
    a = ap.parse_args(argv)
    real, out = Path(a.real), Path(a.out)
    if not real.is_dir():
        shown = real.resolve().relative_to(ROOT).as_posix() if real.resolve().is_relative_to(ROOT) else real.name
        print(f"error: real-fixture folder not found: {shown} (only the maintainer's private repo has one)",
              file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    for f in sorted(real.glob("*.json")):
        point = anonymise(f.stem, json.loads(f.read_text(encoding="utf-8")))
        (out / f.name).write_text(json.dumps(point, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out / f.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
