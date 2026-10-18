"""Turn raw Google Health API data points into rows for the clean tables."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Callable, Iterable

from .errors import NormalizeError
from .registry import DataType, Kind
from .store import RawRow, Store
from .timeutil import (UTC, canonical_ts, civil_str, date_str, iso_utc, minutes_between, num, parse_duration_s,
                       parse_ts)

log = logging.getLogger(__name__)

# What a normalizer may raise on a payload that doesn't have the expected shape.
MALFORMED = (KeyError, TypeError, ValueError, AttributeError)

Rows = dict[str, list[dict]]
Normalizer = Callable[[dict, str, str], Rows]  # (payload body, point_key, local_date) -> rows by table


def describe_source(data_source: dict | None) -> str:
    if not data_source:
        return "reconciled"  # reconcile responses carry no dataSource
    platform = data_source.get("platform", "UNKNOWN")
    device = (data_source.get("device") or {}).get("displayName")
    return f"{platform}/{device}" if device else platform


def _utc(value: str) -> str:
    return iso_utc(parse_ts(value))


def _local_date(civil: dict | None, instant_utc: str, offset: str | None) -> str:
    """Local date from the API's civil time; else from the UTC instant plus its UTC offset; else the UTC date."""
    if civil:
        return date_str(civil["date"])
    if offset:
        return (parse_ts(instant_utc) + timedelta(seconds=parse_duration_s(offset))).date().isoformat()
    log.warning("no civil time or UTC offset for %s; using the UTC date", instant_utc)
    return instant_utc[:10]


def time_fields(dt: DataType, body: dict) -> tuple[str | None, str | None, str, str | None]:
    """(start_utc, end_utc, local_date, utc_offset) for one payload body."""
    if dt.kind is Kind.DAILY:
        return None, None, date_str(body["date"]), None
    if dt.kind is Kind.SAMPLE:
        st = body["sampleTime"]
        start = _utc(st["physicalTime"])
        return start, None, _local_date(st.get("civilTime"), start, st.get("utcOffset")), st.get("utcOffset")
    iv = body["interval"]
    start = _utc(iv["startTime"])
    end = _utc(iv["endTime"]) if iv.get("endTime") else None
    if dt.kind is Kind.SLEEP:  # sleep belongs to the date you wake up
        offset = iv.get("endUtcOffset")
        return start, end, _local_date(iv.get("civilEndTime"), end or start, offset), offset
    offset = iv.get("startUtcOffset")
    return start, end, _local_date(iv.get("civilStartTime"), start, offset), offset


def to_raw_row(dt: DataType, point: dict) -> RawRow:
    body = point[dt.field]
    start, end, local_date, offset = time_fields(dt, body)
    name = point.get("name") or point.get("dataPointName")
    source = describe_source(point.get("dataSource"))
    key = dt.point_key(body, name)
    if dt.method == "list" and not (dt.keyed_by_name and name):
        key += "|" + source  # `list` returns one point per source, so the source is part of the identity
    return RawRow(
        data_type=dt.name,
        point_key=key,
        start_utc=start,
        end_utc=end,
        local_date=local_date,
        utc_offset=offset,
        source=source,
        payload=point,
    )


# ---- row builders ----------------------------------------------------------

def _samples(body: dict, local_date: str, values: dict[str, float | None]) -> Rows:
    ts = _utc(body["sampleTime"]["physicalTime"])
    return {"samples": [{"ts_utc": ts, "local_date": local_date, "metric": m, "value": v}
                        for m, v in values.items() if v is not None]}


def _daily(local_date: str, values: dict[str, float | None]) -> Rows:
    return {"daily_metrics": [{"local_date": local_date, "metric": m, "value": v}
                              for m, v in values.items() if v is not None]}


def _activity(body: dict, local_date: str, values: dict[str, float | None]) -> Rows:
    iv = body["interval"]
    start, end = _utc(iv["startTime"]), _utc(iv["endTime"])
    return {"activity_intervals": [{"start_utc": start, "end_utc": end, "local_date": local_date, "metric": m, "value": v}
                                   for m, v in values.items() if v is not None]}


def _interval_minutes(body: dict) -> float:
    iv = body["interval"]
    return round(minutes_between(iv["startTime"], iv["endTime"]), 3)


def _lower(value: str | None) -> str:
    return (value or "unspecified").lower()


def _div(value, divisor: float) -> float | None:
    """Unit conversion by division (v / 1000 is exact where v * (1 / 1000) is not)."""
    v = num(value)
    return None if v is None else v / divisor


# ---- heart & samples -------------------------------------------------------

def heart_rate(body: dict, key: str, local_date: str) -> Rows:
    st = body["sampleTime"]
    return {"heart_rate": [{
        "ts_utc": _utc(st["physicalTime"]),
        "local_ts": civil_str(st["civilTime"]) if st.get("civilTime") else None,
        "local_date": local_date,
        "bpm": int(body["beatsPerMinute"]),
        "motion": (body.get("metadata") or {}).get("motionContext"),
    }]}


def heart_rate_variability(body: dict, key: str, local_date: str) -> Rows:
    return _samples(body, local_date, {
        "hrv_rmssd_ms": num(body.get("rootMeanSquareOfSuccessiveDifferencesMilliseconds")),
        "hrv_sdnn_ms": num(body.get("standardDeviationMilliseconds")),
    })


def oxygen_saturation(body: dict, key: str, local_date: str) -> Rows:
    return _samples(body, local_date, {"spo2_pct": num(body.get("percentage"))})


def respiratory_rate_sleep_summary(body: dict, key: str, local_date: str) -> Rows:
    stats = {"full": "fullSleepStats", "deep": "deepSleepStats", "light": "lightSleepStats", "rem": "remSleepStats"}
    return _samples(body, local_date, {
        f"resp_{name}_brpm": num((body.get(field) or {}).get("breathsPerMinute")) for name, field in stats.items()
    })


def vo2_max(body: dict, key: str, local_date: str) -> Rows:
    return _samples(body, local_date, {"vo2_max": num(body.get("vo2Max"))})


def run_vo2_max(body: dict, key: str, local_date: str) -> Rows:
    return _samples(body, local_date, {"run_vo2_max": num(body.get("runVo2Max"))})


def weight(body: dict, key: str, local_date: str) -> Rows:
    return _samples(body, local_date, {"weight_kg": _div(body.get("weightGrams"), 1000)})


def body_fat(body: dict, key: str, local_date: str) -> Rows:
    return _samples(body, local_date, {"body_fat_pct": num(body.get("percentage"))})


def height(body: dict, key: str, local_date: str) -> Rows:
    return _samples(body, local_date, {"height_m": _div(body.get("heightMillimeters"), 1000)})


# ---- daily metrics ---------------------------------------------------------

def daily_resting_heart_rate(body: dict, key: str, local_date: str) -> Rows:
    return _daily(local_date, {"resting_hr": num(body.get("beatsPerMinute"))})


def daily_heart_rate_variability(body: dict, key: str, local_date: str) -> Rows:
    return _daily(local_date, {
        "hrv_rmssd_ms": num(body.get("averageHeartRateVariabilityMilliseconds")),
        "hrv_deep_rmssd_ms": num(body.get("deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds")),
        "hrv_entropy": num(body.get("entropy")),
        "non_rem_hr": num(body.get("nonRemHeartRateBeatsPerMinute")),
    })


def daily_oxygen_saturation(body: dict, key: str, local_date: str) -> Rows:
    return _daily(local_date, {
        "spo2_avg_pct": num(body.get("averagePercentage")),
        "spo2_sd_pct": num(body.get("standardDeviationPercentage")),
        "spo2_lower_pct": num(body.get("lowerBoundPercentage")),
        "spo2_upper_pct": num(body.get("upperBoundPercentage")),
    })


def daily_respiratory_rate(body: dict, key: str, local_date: str) -> Rows:
    return _daily(local_date, {"resp_rate": num(body.get("breathsPerMinute"))})


def daily_sleep_temperature_derivations(body: dict, key: str, local_date: str) -> Rows:
    nightly = num(body.get("nightlyTemperatureCelsius"))
    baseline = num(body.get("baselineTemperatureCelsius"))
    return _daily(local_date, {
        "sleep_temp_c": nightly,
        "sleep_temp_baseline_c": baseline,
        "sleep_temp_dev_c": round(nightly - baseline, 3) if nightly is not None and baseline is not None else None,
        "sleep_temp_sd30_c": num(body.get("relativeNightlyStddev30dCelsius")),
    })


def daily_vo2_max(body: dict, key: str, local_date: str) -> Rows:
    estimated = body.get("estimated")
    return _daily(local_date, {
        "vo2_max": num(body.get("vo2Max")),
        "vo2_max_estimated": None if estimated is None else float(bool(estimated)),
        "vo2_max_covariance": num(body.get("vo2MaxCovariance")),
    })


def daily_heart_rate_zones(body: dict, key: str, local_date: str) -> Rows:
    values: dict[str, float | None] = {}
    for zone in body.get("heartRateZones", []):
        name = _lower(zone.get("heartRateZoneType"))
        values[f"hr_zone_{name}_lo_bpm"] = num(zone.get("minBeatsPerMinute"))
        values[f"hr_zone_{name}_hi_bpm"] = num(zone.get("maxBeatsPerMinute"))
    return _daily(local_date, values)


# ---- activity ---------------------------------------------------------------

def steps(body: dict, key: str, local_date: str) -> Rows:
    return _activity(body, local_date, {"steps": num(body.get("count"))})


def distance(body: dict, key: str, local_date: str) -> Rows:
    return _activity(body, local_date, {"distance_m": _div(body.get("millimeters"), 1000)})


def active_zone_minutes(body: dict, key: str, local_date: str) -> Rows:
    return _activity(body, local_date, {f"azm_{_lower(body.get('heartRateZone'))}": num(body.get("activeZoneMinutes"))})


def active_minutes(body: dict, key: str, local_date: str) -> Rows:
    return _activity(body, local_date, {
        f"active_min_{_lower(x.get('activityLevel'))}": num(x.get("activeMinutes"))
        for x in body.get("activeMinutesByActivityLevel", [])
    })


def sedentary_period(body: dict, key: str, local_date: str) -> Rows:
    return _activity(body, local_date, {"sedentary_min": _interval_minutes(body)})


def activity_level(body: dict, key: str, local_date: str) -> Rows:
    return _activity(body, local_date, {f"level_{_lower(body.get('activityLevelType'))}_min": _interval_minutes(body)})


def active_energy_burned(body: dict, key: str, local_date: str) -> Rows:
    return _activity(body, local_date, {"active_kcal": num(body.get("kcal"))})


def time_in_heart_rate_zone(body: dict, key: str, local_date: str) -> Rows:
    return _activity(body, local_date, {f"hr_zone_{_lower(body.get('heartRateZoneType'))}_minutes": _interval_minutes(body)})


# ---- sessions & alerts ------------------------------------------------------

def exercise(body: dict, key: str, local_date: str) -> Rows:
    iv = body["interval"]
    m = body.get("metricsSummary") or {}
    active = body.get("activeDuration")
    return {"exercise_sessions": [{
        "id": key,
        "exercise_type": body.get("exerciseType"),
        "display_name": body.get("displayName"),
        "start_utc": _utc(iv["startTime"]),
        "end_utc": _utc(iv["endTime"]) if iv.get("endTime") else None,
        "local_date": local_date,
        "duration_min": round(minutes_between(iv["startTime"], iv["endTime"]), 2) if iv.get("endTime") else None,
        "active_min": round(parse_duration_s(active) / 60, 2) if active else None,
        "avg_hr": num(m.get("averageHeartRateBeatsPerMinute")),
        "calories_kcal": num(m.get("caloriesKcal")),
        "steps": num(m.get("steps")),
        "distance_m": _div(m.get("distanceMillimeters"), 1000),
        "azm": num(m.get("activeZoneMinutes")),
    }]}


def irregular_rhythm_notification(body: dict, key: str, local_date: str) -> Rows:
    iv = body["interval"]
    return {"alerts": [{
        "start_utc": _utc(iv["startTime"]),
        "end_utc": _utc(iv["endTime"]) if iv.get("endTime") else None,
        "local_date": local_date,
        "kind": "irregular_rhythm",
        "payload": json.dumps(body),
    }]}


# ---- sleep -----------------------------------------------------------------

def sleep(body: dict, key: str, local_date: str) -> Rows:
    iv = body["interval"]
    summary = body.get("summary") or {}
    meta = body.get("metadata") or {}
    by_stage = {s.get("type"): num(s.get("minutes")) for s in summary.get("stagesSummary", [])}
    asleep = num(summary.get("minutesAsleep"))
    period = num(summary.get("minutesInSleepPeriod"))
    session = {
        "id": key,
        "start_utc": _utc(iv["startTime"]),
        "end_utc": _utc(iv["endTime"]),
        "local_date": local_date,
        "is_main": int(bool(meta.get("mainSleep"))),
        "is_nap": int(bool(meta.get("nap"))),
        "sleep_type": body.get("type"),
        "min_asleep": asleep,
        "min_awake": num(summary.get("minutesAwake")),
        "min_to_fall_asleep": num(summary.get("minutesToFallAsleep")),
        "min_after_wake": num(summary.get("minutesAfterWakeUp")),
        "min_in_period": period,
        "efficiency": round(asleep / period, 4) if asleep is not None and period else None,
        "min_light": by_stage.get("LIGHT"),
        "min_deep": by_stage.get("DEEP"),
        "min_rem": by_stage.get("REM"),
        "update_time": canonical_ts(body["updateTime"]) if body.get("updateTime") else None,
    }
    stages = [{
        "session_id": key,
        "stage": st["type"],
        "start_utc": _utc(st["startTime"]),
        "end_utc": _utc(st["endTime"]),
        "local_date": local_date,
    } for st in body.get("stages", [])]
    return {"sleep_sessions": [session], "sleep_stages": stages}


_NEVER = datetime.min.replace(tzinfo=UTC)


def _recency(session: dict) -> tuple[datetime, str]:
    """Sort key, newest last: update time as an instant (missing is oldest), ties broken by later end."""
    ut = session.get("update_time")
    return (parse_ts(ut) if ut else _NEVER, session["end_utc"])


def dedupe_sleep(rows: Rows) -> None:
    """When Google reprocesses a night, overlapping sessions can coexist; keep the newest one."""
    newest_first = sorted(rows.get("sleep_sessions", []), key=_recency, reverse=True)
    kept: list[dict] = []
    for s in newest_first:
        if not any(s["start_utc"] < k["end_utc"] and s["end_utc"] > k["start_utc"] for k in kept):
            kept.append(s)
    ids = {k["id"] for k in kept}
    rows["sleep_sessions"] = sorted(kept, key=lambda r: r["start_utc"])
    rows["sleep_stages"] = [st for st in rows.get("sleep_stages", []) if st["session_id"] in ids]


NORMALIZERS: dict[str, Normalizer] = {
    "heart-rate": heart_rate,
    "heart-rate-variability": heart_rate_variability,
    "oxygen-saturation": oxygen_saturation,
    "respiratory-rate-sleep-summary": respiratory_rate_sleep_summary,
    "vo2-max": vo2_max,
    "run-vo2-max": run_vo2_max,
    "weight": weight,
    "body-fat": body_fat,
    "height": height,
    "daily-resting-heart-rate": daily_resting_heart_rate,
    "daily-heart-rate-variability": daily_heart_rate_variability,
    "daily-oxygen-saturation": daily_oxygen_saturation,
    "daily-respiratory-rate": daily_respiratory_rate,
    "daily-sleep-temperature-derivations": daily_sleep_temperature_derivations,
    "daily-vo2-max": daily_vo2_max,
    "daily-heart-rate-zones": daily_heart_rate_zones,
    "steps": steps,
    "distance": distance,
    "active-zone-minutes": active_zone_minutes,
    "active-minutes": active_minutes,
    "sedentary-period": sedentary_period,
    "activity-level": activity_level,
    "active-energy-burned": active_energy_burned,
    "time-in-heart-rate-zone": time_in_heart_rate_zone,
    "exercise": exercise,
    "irregular-rhythm-notification": irregular_rhythm_notification,
    "sleep": sleep,
}


REBUILD_BATCH_DAYS = 7  # bounds memory: a full heart-rate rebuild is ~17k rows per day


def _widen(dates: list[str]) -> list[str]:
    """Every date plus its neighbours: a reprocessed sleep session can move its wake date by a day."""
    out: set[str] = set()
    for d in dates:
        day = date.fromisoformat(d)
        out.update((day + timedelta(days=k)).isoformat() for k in (-1, 0, 1))
    return sorted(out)


def rebuild(store: Store, dt: DataType, dates: Iterable[str]) -> int:
    """Regenerate the clean rows produced by `dt` on `dates` from raw_points. Returns rows written.

    Dates are processed in sorted batches of at most REBUILD_BATCH_DAYS so memory stays bounded. Sleep is
    small and its sessions can overlap across days, so it is one batch: it writes `dates` +/- 1 day but reads
    +/- 2 days, so every written date is de-duplicated against its neighbours too.

    Each batch commits on its own, so a rebuild is idempotent but not atomic across batches: if it fails
    midway (e.g. NormalizeError), earlier batches stay rebuilt; fix the cause and re-run to recover.
    Raises NormalizeError, leaving that batch's existing clean rows untouched, if every point in a batch
    fails to normalize.
    """
    dates = sorted(set(dates))
    if dt.name == "sleep":
        target = _widen(dates)
        batches = [(target, _widen(target))] if dates else []  # (dates written, dates read)
    else:
        batches = [(b, b) for b in (dates[i : i + REBUILD_BATCH_DAYS] for i in range(0, len(dates), REBUILD_BATCH_DAYS))]
    fn = NORMALIZERS[dt.name]
    written = 0
    for batch, read in batches:
        rows: Rows = defaultdict(list)
        seen = skipped = 0
        first: tuple[str, Exception] | None = None
        for key, local_date, point in store.raw_points_for(dt.name, read):
            seen += 1
            try:
                for table, table_rows in fn(point[dt.field], key, local_date).items():
                    rows[table].extend(table_rows)
            except MALFORMED as exc:
                skipped += 1
                first = first or (key, exc)
        if first:
            log.warning("skipping %d of %d malformed %s points between %s and %s, e.g. %s: %r",
                        skipped, seen, dt.name, batch[0], batch[-1], first[0], first[1])
            if skipped == seen:
                raise NormalizeError(f"{dt.name}: all {seen} points failed to normalize, e.g. {first[0]}: {first[1]!r}")
        if dt.name == "sleep":
            dedupe_sleep(rows)
            in_batch = set(batch)
            rows = {t: [r for r in rs if r["local_date"] in in_batch] for t, rs in rows.items()}
        store.replace_clean(dt.name, batch, rows)
        written += sum(len(r) for r in rows.values())
    return written
