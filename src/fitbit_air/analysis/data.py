"""Load everything the analyses need for a date range, via Store read methods (no SQL here)."""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime

from ..store import Store
from ..timeutil import parse_duration_s, parse_ts

log = logging.getLogger(__name__)

DAILY_SOURCES = ("resting_hr", "hrv_rmssd_ms", "resp_rate", "sleep_temp_c", "sleep_temp_dev_c", "spo2_avg_pct")


@dataclass
class Night:
    date: str  # wake date (local)
    session_id: str
    start_utc: datetime
    end_utc: datetime
    offset_s: int  # local time = UTC + offset_s
    sleep_type: str | None
    min_asleep: float | None
    min_in_period: float | None
    efficiency: float | None
    min_deep: float | None
    min_rem: float | None
    stages: list[tuple[str, datetime, datetime]] = field(default_factory=list)


@dataclass
class Inputs:
    daily: dict[str, dict[str, float]]  # daily_metrics name -> {local_date: value}
    nights: dict[str, Night]  # wake date -> main sleep
    hr_minutes: dict[str, list[tuple[datetime, float]]]  # local_date -> [(UTC minute start, mean bpm)]
    sedentary: dict[str, list[tuple[datetime, datetime]]]  # local_date -> sedentary intervals (UTC)
    alerts: list[tuple[str, str]]  # (local_date, kind)
    age: int | None


def _offset_s(value: str | None) -> int | None:
    """UTC offset seconds from a raw 'NNNNs' duration; None when missing or unparseable."""
    if not value:
        return None
    try:
        return int(parse_duration_s(value))
    except ValueError:
        return None


def _age(snapshot: dict) -> int | None:
    """Age from the profile snapshot; None unless it parses to a plausible 10..120."""
    try:
        age = int(float(snapshot["age"]))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return age if 10 <= age <= 120 else None


def load_inputs(store: Store, start: str, end: str, minutes_start: str | None = None) -> Inputs:
    """Inputs for [start, end]. Minute-level data (heart-rate minutes, sedentary intervals) is the expensive part:
    with `minutes_start` it is loaded only from that date (daily metrics, nights and alerts still from `start`)."""
    m_start = start if minutes_start is None else minutes_start
    daily: dict[str, dict[str, float]] = {}
    for local_date, metric, value in store.daily_metric_values(DAILY_SOURCES, start, end):
        if value is not None:
            daily.setdefault(metric, {})[local_date] = value

    sessions = store.main_sleep_sessions(start, end)
    stages: dict[str, list[tuple[str, datetime, datetime]]] = {}
    for r in store.sleep_stages_for(s["id"] for s in sessions):
        stages.setdefault(r["session_id"], []).append((r["stage"], parse_ts(r["start_utc"]), parse_ts(r["end_utc"])))
    nights: dict[str, Night] = {}
    unknown_offset: set[str] = set()
    for s in sessions:
        offset = _offset_s(s["utc_offset"])
        night = Night(
            date=s["local_date"], session_id=s["id"], start_utc=parse_ts(s["start_utc"]),
            end_utc=parse_ts(s["end_utc"]), offset_s=0 if offset is None else offset, sleep_type=s["sleep_type"],
            min_asleep=s["min_asleep"], min_in_period=s["min_in_period"], efficiency=s["efficiency"],
            min_deep=s["min_deep"], min_rem=s["min_rem"], stages=sorted(stages.get(s["id"], []), key=lambda x: x[1]),
        )
        existing = nights.get(night.date)
        if existing is None or (night.min_in_period or 0) > (existing.min_in_period or 0):
            nights[night.date] = night  # one main sleep per wake date: keep the longest
            unknown_offset.discard(night.date)
            if offset is None:
                unknown_offset.add(night.date)

    known = Counter(n.offset_s for d, n in nights.items() if d not in unknown_offset)
    for d in sorted(unknown_offset):
        fallback = known.most_common(1)[0][0] if known else 0
        log.warning("sleep on %s has no usable UTC offset; using %ss", d, fallback)
        nights[d] = replace(nights[d], offset_s=fallback)

    hr_minutes: dict[str, list[tuple[datetime, float]]] = {}
    for local_date, minute, bpm in store.hr_minute_means(m_start, end):
        hr_minutes.setdefault(local_date, []).append((parse_ts(minute + ":00Z"), bpm))

    sedentary: dict[str, list[tuple[datetime, datetime]]] = {}
    for local_date, s_utc, e_utc in store.sedentary_intervals(m_start, end):
        sedentary.setdefault(local_date, []).append((parse_ts(s_utc), parse_ts(e_utc)))

    snapshot = store.get_snapshot("profile") or {}
    return Inputs(daily=daily, nights=nights, hr_minutes=hr_minutes, sedentary=sedentary,
                  alerts=store.alerts_between(start, end), age=_age(snapshot))
