"""Running: Karvonen zones, per-run metrics, the single-run spike rule, intensity balance, race forecast."""
from __future__ import annotations

import math
import re
import statistics
from datetime import date, datetime, timedelta

from ..timeutil import parse_ts
from .evidence import Registry

ZONE_FRACTIONS = (0.5, 0.6, 0.7, 0.8, 0.9, 1.0)  # Karvonen % of HR reserve bounding Z1..Z5
_RUN_TYPE = re.compile(r"(^|_)RUN(NING)?(_|$)")  # RUN, RUNNING, TREADMILL_RUNNING; not CRUNCH


def is_run(session: dict) -> bool:
    return bool(_RUN_TYPE.search((session.get("exercise_type") or "").upper()))


def hrmax(age: int | None, override: int | None, reg: Registry) -> float | None:
    if override:
        return float(override)
    if age is None:
        return None
    return reg.param("hrmax_intercept") - reg.param("hrmax_slope") * age


def zone_bounds(hr_max: float, hr_rest: float) -> list[float]:
    """Lower bound of Z1..Z5 plus HRmax: HRrest + f x (HRmax - HRrest) for f in 50..100%."""
    reserve = hr_max - hr_rest
    return [hr_rest + f * reserve for f in ZONE_FRACTIONS]


def _zone(bpm: float, bounds: list[float]) -> int | None:
    """1..5, or None below Z1."""
    if bpm < bounds[0]:
        return None
    for z in range(5, 0, -1):
        if bpm >= bounds[z - 1]:
            return z
    return None


def run_metrics(session: dict, hr_minutes: list[tuple[datetime, float]], bounds: list[float], reg: Registry) -> dict:
    """Per-run row fields. Decoupling uses HR only (no per-half pace in the data), assuming constant speed.

    Zone minutes are None when the run has no HR samples (unknown, not zero); z0 counts minutes below Z1.
    A missing end time is taken as start + duration; with neither, zones and decoupling are unknown.
    """
    start = parse_ts(session["start_utc"])
    km = (session.get("distance_m") or 0.0) / 1000 or None
    duration = session.get("duration_min")
    if session.get("end_utc"):
        end = parse_ts(session["end_utc"])
    elif duration:
        end = start + timedelta(minutes=duration)
    else:
        end = None
    minutes = duration or ((end - start).total_seconds() / 60 if end else None)
    if minutes is not None and minutes <= 0:
        minutes = None
    inside = [(t, b) for t, b in hr_minutes if start <= t < end] if end else []
    zones: dict[int, int | None] = {z: None for z in range(0, 6)}
    if inside:
        zones = {z: 0 for z in range(0, 6)}
        for _t, b in inside:
            zones[_zone(b, bounds) or 0] += 1
    decoupling = _decoupling(start, end, minutes, inside, reg) if end else None
    avg_hr = session.get("avg_hr") or (sum(b for _, b in inside) / len(inside) if inside else None)
    economy = None
    if km and minutes and avg_hr and avg_hr <= bounds[2]:  # easy run: average HR within Z1-Z2
        economy = km * 1000 / (avg_hr * minutes)
    return {"session_id": session["id"], "local_date": session["local_date"], "distance_km": km,
            "duration_min": minutes, "pace_min_km": (minutes / km) if km and minutes else None, "avg_hr": avg_hr,
            "z0_min": zones[0], "z1_min": zones[1], "z2_min": zones[2], "z3_min": zones[3], "z4_min": zones[4],
            "z5_min": zones[5], "decoupling_pct": decoupling,
            "decoupling_approx": 1 if decoupling is not None else None,
            "economy_m_per_beat": economy}


def _decoupling(start: datetime, end: datetime, minutes: float | None, inside: list[tuple[datetime, float]],
                reg: Registry) -> float | None:
    """HR drift between the two time-halves after the warm-up; None for short or non-steady (run-walk) efforts."""
    warmup = reg.param("decoupling_warmup_min")
    if not minutes or minutes - warmup < reg.param("decoupling_min_run_min"):
        return None
    cut = start + timedelta(minutes=warmup)
    steady = [(t, b) for t, b in inside if t >= cut]
    if len(steady) < 4:
        return None
    values = [b for _, b in steady]
    mean = statistics.fmean(values)
    if mean <= 0 or statistics.pstdev(values) / mean > reg.param("decoupling_max_cv"):
        return None
    mid = cut + (end - cut) / 2
    first = [b for t, b in steady if t < mid]
    second = [b for t, b in steady if t >= mid]
    if not first or not second:
        return None
    return (1 - statistics.fmean(first) / statistics.fmean(second)) * 100  # EF1/EF2 = HR2/HR1 at constant speed


def spike(km: float | None, start_utc: str, prior: list[dict], reg: Registry) -> tuple[float | None, str | None]:
    """(% over the longest run in the previous 30 days, class) if above the spike threshold, else (None, None)."""
    if not km:
        return None, None
    start = parse_ts(start_utc)
    lookback = start - timedelta(days=reg.param("spike_lookback_days"))
    longest = max((r["distance_km"] for r in prior
                   if r.get("distance_km") and lookback <= parse_ts(r["start_utc"]) < start),
                  default=None)
    if not longest:
        return None, None
    pct = round((km / longest - 1) * 100, 6)  # rounding keeps an exact +10% from flagging via float error
    if pct <= reg.param("spike_pct"):
        return None, None
    if pct > reg.param("spike_large_pct"):
        return pct, "large"
    if pct > reg.param("spike_moderate_pct"):
        return pct, "moderate"
    return pct, "small"


def weekly_progression(km: float | None, start_utc: str, prior: list[dict], reg: Registry) -> float | None:
    """% over the longest run that started 7-30 days earlier, if above the spike threshold, else None.

    Catches compounding week-on-week growth that the per-session spike rule allows.
    """
    if not km:
        return None
    start = parse_ts(start_utc)
    oldest = start - timedelta(days=reg.param("spike_lookback_days"))
    newest = start - timedelta(days=reg.param("weekly_lookback_min_days"))
    longest = max((r["distance_km"] for r in prior
                   if r.get("distance_km") and oldest <= parse_ts(r["start_utc"]) <= newest),
                  default=None)
    if not longest:
        return None
    pct = round((km / longest - 1) * 100, 6)
    return pct if pct > reg.param("spike_pct") else None


def intensity_balance(runs: list[dict], day: str, reg: Registry) -> float | None:
    """Share of running time in Z0-Z2 (walk breaks count as easy) over the intensity window ending `day`."""
    start = (date.fromisoformat(day) - timedelta(days=int(reg.param("intensity_window_days")) - 1)).isoformat()
    recent = [r for r in runs if start <= r["local_date"] <= day]
    total = sum(sum(r.get(f"z{z}_min") or 0 for z in range(0, 6)) for r in recent)
    if not total:
        return None
    return sum(sum(r.get(f"z{z}_min") or 0 for z in range(0, 3)) for r in recent) / total


def _weeks_needed(increments: int, cutback_every: int) -> int:
    """Calendar weeks to complete `increments` build weeks with a cutback week after every cutback_every-1 of them."""
    if increments <= 0:
        return 0
    return increments + (increments - 1) // (cutback_every - 1)


def _safe_km(longest: float, race_km: float, weeks_left: int, taper: int, cutback_every: int, growth: float) -> float:
    """Longest run reachable by race day on the +10% / cutback schedule (capped at the race distance)."""
    available = max(0, weeks_left - taper)
    build_weeks = max(0, available - available // cutback_every)
    return min(race_km, longest * growth ** build_weeks)


def _planning_km(distances: list[float], growth: float) -> float:
    """Longest run for planning, in date order: each run counts at most `growth` x the planning distance so far, so
    neither a spike nor repeats of it can make the plan look better than a +10%-per-run build would."""
    planning = distances[0]
    for km in distances[1:]:
        planning = max(planning, min(km, growth * planning))
    return planning


def forecast(runs: list[dict], day: str, race_date: str, race_km: float, reg: Registry) -> dict:
    """Completion forecast: build the longest run to `race_km` by +10% steps (one per week), with cutback weeks
    and taper."""
    d, race = date.fromisoformat(day), date.fromisoformat(race_date)
    weeks_left = max(0, (race - d).days // 7)
    growth, taper = reg.param("forecast_growth"), int(reg.param("forecast_taper_weeks"))
    cutback = int(reg.param("forecast_cutback_every"))
    start = (d - timedelta(days=int(reg.param("spike_lookback_days")) - 1)).isoformat()
    recent = [r["distance_km"] for r in sorted((r for r in runs if r.get("distance_km")
                                                and start <= r["local_date"] <= day), key=lambda r: r["local_date"])]
    if not recent:
        first_km = reg.param("forecast_first_run_km")
        needed = max(0, math.ceil(math.log(race_km / first_km) / math.log(growth)))
        weeks_needed = _weeks_needed(needed, cutback)
        first_run_by = race - timedelta(weeks=weeks_needed + taper)
        behind = first_run_by < d
        return {"status": "no_runs_behind" if behind else "no_runs", "longest_km": None, "increments": None,
                "weeks_needed": weeks_needed, "weeks_left": weeks_left, "first_run_by": first_run_by.isoformat(),
                "latest_start": None, "first_run_km": first_km,
                "weeks_short": math.ceil((d - first_run_by).days / 7) if behind else None,
                "safe_by_race_km": _safe_km(first_km, race_km, weeks_left, taper, cutback, growth),
                "planning_km": None}
    longest = max(recent)
    planning = _planning_km(recent, growth)
    increments = 0 if planning >= race_km else math.ceil(math.log(race_km / planning) / math.log(growth))
    weeks_needed = _weeks_needed(increments, cutback)
    if weeks_needed <= weeks_left - taper:
        status = "on_track"
    elif weeks_needed <= weeks_left:
        status = "tight"
    else:
        status = "behind"
    return {"status": status, "longest_km": longest, "increments": increments, "weeks_needed": weeks_needed,
            "weeks_left": weeks_left, "first_run_by": None,
            "latest_start": (race - timedelta(weeks=weeks_needed + taper)).isoformat(), "first_run_km": None,
            "weeks_short": None, "safe_by_race_km": _safe_km(planning, race_km, weeks_left, taper, cutback, growth),
            "planning_km": planning}


def riegel_estimate(runs: list[dict], day: str, race_km: float, reg: Registry) -> float | None:
    """Predicted race time (minutes) from the fastest-paced run of at least max(riegel_min_km, half the race) in
    the lookback."""
    start = (date.fromisoformat(day) - timedelta(days=int(reg.param("riegel_lookback_days")) - 1)).isoformat()
    # extrapolating far is unreliable
    min_km = max(reg.param("riegel_min_km"), reg.param("riegel_min_fraction") * race_km)
    candidates = [r for r in runs if start <= r["local_date"] <= day and (r.get("distance_km") or 0) >= min_km
                  and r.get("duration_min")]
    if not candidates:
        return None
    best = min(candidates, key=lambda r: r["duration_min"] / r["distance_km"])
    return best["duration_min"] * (race_km / best["distance_km"]) ** reg.param("riegel_exponent")


def uth_vo2max(hr_max: float | None, hr_rest: float | None, reg: Registry) -> float | None:
    """Heart Rate Ratio Method (low precision)."""
    if not hr_max or not hr_rest:
        return None
    return reg.param("uth_factor") * hr_max / hr_rest
