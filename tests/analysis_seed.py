"""Seed synthetic histories into a Store for analysis tests (local timezone offset +09:30)."""
from datetime import date, datetime, timedelta, timezone

from fitbit_air.store import RawRow

UTC = timezone.utc
LOCAL_OFFSET_S = 34200


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def days(start: str, n: int) -> list[str]:
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def seed_daily(store, metric: str, values: dict[str, float]) -> None:
    store.replace_clean(f"seed-{metric}", list(values),
                        {"daily_metrics": [{"local_date": d, "metric": metric, "value": v} for d, v in values.items()]})


def seed_night(store, wake: str, bed_local: str = "23:00", asleep_h: float = 7.5, awake_min: float = 20,
               offset_s: int = LOCAL_OFFSET_S, sleep_type: str = "STAGES") -> str:
    """Main sleep waking on `wake`: an AWAKE stage then LIGHT sleep. Bedtimes at/after 12:00 are the evening before."""
    wd = date.fromisoformat(wake)
    hh, mm = (int(x) for x in bed_local.split(":"))
    bed_day = wd - timedelta(days=1) if hh >= 12 else wd
    start = datetime(bed_day.year, bed_day.month, bed_day.day, hh, mm, tzinfo=UTC) - timedelta(seconds=offset_s)
    awake = timedelta(minutes=awake_min)
    end = start + awake + timedelta(hours=asleep_h)
    sid = f"s-{wake}"
    period = (end - start).total_seconds() / 60
    asleep_min = asleep_h * 60
    store.replace_clean("sleep", [wake], {
        "sleep_sessions": [{"id": sid, "start_utc": iso(start), "end_utc": iso(end), "local_date": wake, "is_main": 1,
                            "is_nap": 0, "sleep_type": sleep_type, "min_asleep": asleep_min, "min_awake": awake_min,
                            "min_in_period": period, "efficiency": asleep_min / period, "min_light": asleep_min,
                            "min_deep": 0.0, "min_rem": 0.0}],
        "sleep_stages": [
            {"session_id": sid, "stage": "AWAKE", "start_utc": iso(start), "end_utc": iso(start + awake), "local_date": wake},
            {"session_id": sid, "stage": "LIGHT", "start_utc": iso(start + awake), "end_utc": iso(end), "local_date": wake}]})
    store.upsert_raw([RawRow("sleep", sid, iso(start), iso(end), wake, f"{offset_s}s", "reconciled", {"seed": wake})])
    return sid


def seed_hr_minutes(store, day: str, bpm: float = 60.0, minutes: int = 1440, offset_s: int = LOCAL_OFFSET_S) -> None:
    d = date.fromisoformat(day)
    midnight = datetime(d.year, d.month, d.day, tzinfo=UTC) - timedelta(seconds=offset_s)
    rows = [{"ts_utc": iso(midnight + timedelta(minutes=i)), "local_ts": None, "local_date": day, "bpm": int(bpm),
             "motion": None} for i in range(minutes)]
    store.replace_clean("heart-rate", [day], {"heart_rate": rows})


def seed_sedentary(store, day: str, start_local_h: int, hours: float, offset_s: int = LOCAL_OFFSET_S) -> None:
    d = date.fromisoformat(day)
    start = datetime(d.year, d.month, d.day, start_local_h, tzinfo=UTC) - timedelta(seconds=offset_s)
    store.replace_clean("activity-level", [day], {"activity_intervals": [
        {"start_utc": iso(start), "end_utc": iso(start + timedelta(hours=hours)), "local_date": day,
         "metric": "level_sedentary_min", "value": hours * 60}]})


def history(store, start: str, n: int, rhr: float = 60, hrv: float = 40, resp: float = 15, temp: float = 34.5,
            temp_dev: float = 0.0, spo2: float = 96, asleep_h: float = 7.5, bed: str = "23:00") -> list[str]:
    """`n` consecutive normal days: overnight metrics, a valid main sleep and full heart-rate wear."""
    ds = days(start, n)
    for metric, value in (("resting_hr", rhr), ("hrv_rmssd_ms", hrv), ("resp_rate", resp), ("sleep_temp_c", temp),
                          ("sleep_temp_dev_c", temp_dev), ("spo2_avg_pct", spo2)):
        seed_daily(store, metric, {d: value for d in ds})
    for d in ds:
        seed_night(store, d, bed_local=bed, asleep_h=asleep_h)
        seed_hr_minutes(store, d)
    return ds
