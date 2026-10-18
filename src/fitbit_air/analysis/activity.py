"""Daily activity: steps, MVPA (walking cadence + workouts), sedentary time and strength training."""
from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, timedelta

from ..timeutil import parse_ts
from .evidence import Registry

AZM_METRICS = ("azm_fat_burn", "azm_cardio", "azm_peak")
CADENCE_METRIC = "cadence_mvpa"
# Minute-level activity-level intervals give total sedentary time; sedentary-period bouts undercount it.
SEDENTARY_METRIC = "level_sedentary_min"
STRENGTH_NAME_HINTS = ("weight", "strength", "resistance", "circuit", "lifting")
STRENGTH_TYPES = frozenset({"WEIGHTS", "WEIGHTLIFTING", "STRENGTH_TRAINING", "CIRCUIT_TRAINING", "CROSSFIT",
                            "BODYWEIGHT", "CALISTHENICS", "PILATES", "RESISTANCE_TRAINING"})


def daily_totals(rows: list[tuple[str, str, str, str, float]]) -> dict[str, dict[str, float]]:
    """Activity interval rows (local_date, metric, start, end, value) -> metric -> {date: daily sum}."""
    out: dict[str, dict[str, float]] = {}
    for local_date, metric, _s, _e, value in rows:
        if value is not None:
            by_day = out.setdefault(metric, {})
            by_day[local_date] = by_day.get(local_date, 0.0) + value
    return out


def day_window(day: str, days: int) -> list[str]:
    """The `days` dates ending on `day`, newest first."""
    d = date.fromisoformat(day)
    return [(d - timedelta(days=i)).isoformat() for i in range(days)]


def _eligible_days(window: list[str], wear_days: set[str], data_days: set[str]) -> list[str]:
    """Window days that count toward an average: the worn days, or, before any device history, the days with data."""
    if any(d in wear_days for d in window):
        return [d for d in window if d in wear_days]
    return [d for d in window if d in data_days]


def worn_day_mean(values: dict[str, float], day: str, window_days: int, wear_days: set[str],
                  min_days: float) -> float | None:
    """Mean of daily `values` over the `window_days` ending `day`, with the worn-day denominator rule.

    Denominator is the days the band was worn (a worn day with no rows counts as 0); with no wear days in the
    window (pre-device history) it is the days that have data. None with fewer than `min_days` eligible days.
    """
    eligible = _eligible_days(day_window(day, window_days), wear_days, set(values))
    if len(eligible) < min_days:
        return None
    return sum(values.get(d, 0.0) for d in eligible) / len(eligible)


def steps_average(steps: dict[str, float], day: str, wear_days: set[str], reg: Registry) -> float | None:
    """Mean daily steps over the activity window ending `day` (see `worn_day_mean`)."""
    return worn_day_mean(steps, day, int(reg.param("activity_window_days")), wear_days, reg.param("min_activity_days"))


def cadence_mvpa_daily(rows: list[tuple[str, str, str, str, float]], sessions: list[dict],
                       reg: Registry) -> dict[str, float]:
    """WHO-equivalent MVPA minutes per day from walking cadence (absolute intensity), not heart-rate zones.

    A steps interval at >= cadence_moderate_spm counts its minutes, >= cadence_vigorous_spm counts them double
    (Tudor-Locke 2019: ~3 and ~6 METs). Rows faster than cadence_max_spm are aggregated totals, not cadence. Every day
    with steps rows is present (0 when nothing was brisk). Workouts without step data (e.g. cycling, elliptical) add
    their minutes as moderate, minus any brisk minutes already counted inside them.
    """
    moderate, vigorous = reg.param("cadence_moderate_spm"), reg.param("cadence_vigorous_spm")
    ceiling = reg.param("cadence_max_spm")
    out: dict[str, float] = {}
    brisk: list[tuple[datetime, datetime]] = []
    for local_date, metric, s_utc, e_utc, value in rows:
        if metric != "steps" or value is None:
            continue
        s, e = parse_ts(s_utc), parse_ts(e_utc)
        minutes = (e - s).total_seconds() / 60
        if minutes <= 0:
            continue
        out.setdefault(local_date, 0.0)
        cadence = value / minutes
        if moderate <= cadence <= ceiling:
            out[local_date] += minutes * (2 if cadence >= vigorous else 1)
            brisk.append((s, e))
    merged = _merge(brisk)
    ends = [e for _s, e in merged]
    for session in sessions:
        if session.get("steps") or not session.get("end_utc"):
            continue
        s, e = parse_ts(session["start_utc"]), parse_ts(session["end_utc"])
        minutes = (e - s).total_seconds() / 60
        i = bisect_right(ends, s)
        while i < len(merged) and merged[i][0] < e:
            minutes -= (min(e, merged[i][1]) - max(s, merged[i][0])).total_seconds() / 60
            i += 1
        if minutes > 0:
            out[session["local_date"]] = out.get(session["local_date"], 0.0) + minutes
    return out


def mvpa_week(azm: dict[str, dict[str, float]], day: str, wear_days: set[str], reg: Registry,
              window_days: int | None = None, min_days: float | None = None,
              metrics: tuple[str, ...] = AZM_METRICS) -> float | None:
    """WHO-equivalent MVPA minutes per week from per-day minute totals under `metrics` (vigorous already doubled):
    Active Zone Minutes by default, or {CADENCE_METRIC: cadence_mvpa_daily(...)}.

    Same denominator rule as `steps_average`: a worn day with no AZM rows earned 0. The window and minimum default
    to the weekly activity window; body age passes its 30-day window.
    """
    window = day_window(day, window_days or int(reg.param("activity_window_days")))
    data_days = {d for d in window if any(d in azm.get(m, {}) for m in metrics)}
    eligible = _eligible_days(window, wear_days, data_days)
    if len(eligible) < (reg.param("min_activity_days") if min_days is None else min_days):
        return None
    total = sum(azm.get(m, {}).get(d, 0.0) for m in metrics for d in eligible)
    return total * 7 / len(eligible)


def _merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[list[datetime]] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def sedentary_hours(rows: list[tuple[str, str, str, str, float]],
                    sleep: list[tuple[datetime, datetime]]) -> dict[str, float]:
    """Total sedentary hours per day (Ekelund's total-sedentary-time basis), minus any overlap with sleep.

    Uses minute-level activity-level intervals; sleep intervals are merged first so overlap is never subtracted twice.
    The merged intervals are sorted and disjoint, so each row bisects on their end times to the first interval that
    ends after the row starts and walks forward only while intervals start before the row ends.
    """
    merged_sleep = _merge(sleep)
    sleep_ends = [e for _s, e in merged_sleep]
    out: dict[str, float] = {}
    for local_date, metric, s_utc, e_utc, _v in rows:
        if metric != SEDENTARY_METRIC:
            continue
        s, e = parse_ts(s_utc), parse_ts(e_utc)
        minutes = (e - s).total_seconds() / 60
        i = bisect_right(sleep_ends, s)  # first interval ending strictly after the row starts
        while i < len(merged_sleep) and merged_sleep[i][0] < e:
            ss, se = merged_sleep[i]
            minutes -= (min(e, se) - max(s, ss)).total_seconds() / 60
            i += 1
        out[local_date] = out.get(local_date, 0.0) + max(0.0, minutes) / 60
    return out


def _is_strength(session: dict) -> bool:
    if (session.get("exercise_type") or "") in STRENGTH_TYPES:
        return True
    name = (session.get("display_name") or "").lower()
    return any(hint in name for hint in STRENGTH_NAME_HINTS)


def strength_week(sessions: list[dict], day: str, window_days: int) -> tuple[float, float]:
    """(minutes per week, distinct strength days per week) over the window ending `day`; WHO counts days."""
    window = set(day_window(day, window_days))
    chosen = [s for s in sessions if s.get("local_date") in window and _is_strength(s)]
    minutes = sum(s.get("duration_min") or 0.0 for s in chosen)
    days_n = len({s["local_date"] for s in chosen})
    return minutes * 7 / window_days, days_n * 7 / window_days
