"""Sleep health on the RU-SATED dimensions (Buysse 2014): duration, regularity (SRI), efficiency, timing."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .data import Night
from .evidence import Registry
from .mathutil import circular_sd_minutes, lin

# Stage labels that count as asleep (Fitbit/Google staged and classic sleep); anything else (AWAKE, unknown) does not.
SLEEP_STAGES = frozenset({"LIGHT", "DEEP", "REM", "ASLEEP", "RESTLESS"})
SUBSCORES = ("duration", "regularity", "efficiency", "timing")
_WEIGHT_PARAMS = {"duration": "w_duration", "regularity": "w_regularity", "efficiency": "w_efficiency",
                  "timing": "w_timing"}


def duration_score(asleep_min: float, reg: Registry) -> float:
    h = asleep_min / 60
    ok_min, ok_max = reg.param("sleep_dur_ok_min_h"), reg.param("sleep_dur_ok_max_h")
    if ok_min <= h <= ok_max:
        return 100.0
    if h < ok_min:
        return lin(h, reg.param("sleep_dur_zero_h"), 0, ok_min, 100)
    return lin(h, ok_max, 100, reg.param("sleep_dur_long_h"), reg.param("sleep_dur_long_score"))


def efficiency_score(efficiency: float, reg: Registry) -> float:
    return lin(efficiency, reg.param("eff_zero"), 0, reg.param("eff_full"), 100)


def regularity_score(sri_value: float, reg: Registry) -> float:
    return lin(sri_value, reg.param("sri_zero"), 0, reg.param("sri_full"), 100)


def timing_score(sd_minutes: float, reg: Registry) -> float:
    return lin(sd_minutes, reg.param("timing_full_min"), 100, reg.param("timing_zero_min"), 0)


def local_minutes(night: Night) -> set[int]:
    """Asleep minutes as 'local epoch minutes' (local wall-clock time counted as if it were UTC).

    Only explicit sleep stages count (SLEEP_STAGES, case-insensitive)."""
    out: set[int] = set()
    for stage, start, end in night.stages:
        if stage.upper() not in SLEEP_STAGES:
            continue
        a = int((start.timestamp() + night.offset_s) // 60)
        b = int((end.timestamp() + night.offset_s) // 60)
        out.update(range(a, b))
    return out


def stages_usable(night: Night) -> bool:
    """True when the stage data covers enough of the night to derive regularity and timing from it.

    Stage-derived asleep minutes must reach 80 % of the reported minutes asleep (at least 60 stage minutes when
    the reported value is missing); a night without usable stages would otherwise read as an empty night and
    collapse the SRI."""
    stage_min = len(local_minutes(night))
    if night.min_asleep is None:
        return stage_min >= 60
    return stage_min >= 0.8 * night.min_asleep


def midpoint_minute_of_day(night: Night) -> float:
    """Local clock minute of the midpoint of the sleep session (session start/end bounds)."""
    mid = (night.start_utc.timestamp() + night.end_utc.timestamp()) / 2 + night.offset_s
    return (mid / 60) % 1440


def sri(asleep: set[int], first_minute: int, days: int) -> float:
    """Sleep Regularity Index (Phillips 2017) over `days` days starting at `first_minute` (a local epoch minute).

    Compares each minute t of the first days-1 days with t + 24 h; -100 (opposite) .. 100 (identical).
    score_night passes local noon as the start, so a window is noon-to-noon and a night is never split."""
    n = (days - 1) * 1440
    same = sum(1 for t in range(first_minute, first_minute + n) if (t in asleep) == ((t + 1440) in asleep))
    return -100 + 200 * same / n


def sleep_score(subscores: dict[str, float | None], reg: Registry) -> tuple[float | None, dict[str, float]]:
    """Weighted score over the available subscores; weights renormalised when some are missing.

    Returns (None, {}) when no subscore is available: insufficient data never produces a score."""
    available = {k: v for k, v in subscores.items() if v is not None}
    if not available:
        return None, {}
    raw = {k: reg.param(_WEIGHT_PARAMS[k]) for k in available}
    total = sum(raw.values())
    weights = {k: w / total for k, w in raw.items()}
    return sum(weights[k] * available[k] for k in available), weights


def debt_night(night: Night | None, reg: Registry) -> bool:
    """A night that counts toward sleep debt: a staged main sleep with known minutes asleep and at least
    `debt_min_period_min` minutes in the sleep period.

    Short nights count (they are exactly what debt is about); the 3-hour validity rule applies only to
    baselines, regularity and timing. Fragments below the floor are not treated as a night's sleep."""
    return (night is not None and (night.sleep_type or "").lower() == "stages" and night.min_asleep is not None
            and (night.min_in_period or 0) >= reg.param("debt_min_period_min"))


def trailing_debt(day: str, nights: dict[str, Night], need_h: float, reg: Registry) -> float | None:
    """Estimated weekly sleep debt in hours: mean shortfall below `need_h` per night, scaled to a full window.

    Uses the nights waking in the `sleep_debt_window_days` days ending `day` that pass `debt_night`; returns None
    when fewer than `sleep_debt_min_nights` such nights exist. With fewer nights than the window, the figure is an
    estimate (mean shortfall x window days), not a sum over the nights present."""
    window = int(reg.param("sleep_debt_window_days"))
    d0 = date.fromisoformat(day)
    shortfalls = []
    for i in range(window):
        w = (d0 - timedelta(days=i)).isoformat()
        if debt_night(nights.get(w), reg):
            shortfalls.append(max(0.0, need_h - nights[w].min_asleep / 60))
    if len(shortfalls) < reg.param("sleep_debt_min_nights"):
        return None
    return sum(shortfalls) / len(shortfalls) * window


def score_night(day: str, nights: dict[str, Night], valid: set[str], need_h: float, reg: Registry) -> dict | None:
    """Sleep result for the main sleep waking on `day`, or None if that night is not valid or nothing is scorable.

    Efficiency is minutes asleep / minutes in the sleep period (Google's definition; close to, not exactly, time
    in bed). Regularity and timing need the `sri_window_days` nights waking d-k+1 .. d, all valid with usable
    stages."""
    if day not in valid or day not in nights:
        return None
    n = nights[day]
    d0 = date.fromisoformat(day)
    k = int(reg.param("sri_window_days"))
    wakes = [(d0 - timedelta(days=i)).isoformat() for i in range(k)]
    sri_value = None
    timing_sd = None
    if all(w in valid and w in nights and stages_usable(nights[w]) for w in wakes):
        # Noon-to-noon window [noon(d-k), noon(d)): the k nights waking d-k+1 .. d, including last night.
        asleep: set[int] = set()
        for w in wakes:
            asleep |= local_minutes(nights[w])
        first_day = d0 - timedelta(days=k)
        first = int(datetime(first_day.year, first_day.month, first_day.day, 12, tzinfo=timezone.utc).timestamp() // 60)
        sri_value = sri(asleep, first, k)
        timing_sd = circular_sd_minutes([midpoint_minute_of_day(nights[w]) for w in wakes])
    subscores = {
        "duration": duration_score(n.min_asleep, reg) if n.min_asleep is not None else None,
        "regularity": regularity_score(sri_value, reg) if sri_value is not None else None,
        "efficiency": efficiency_score(n.efficiency, reg) if n.efficiency is not None else None,
        "timing": timing_score(timing_sd, reg) if timing_sd is not None else None,
    }
    value, weights = sleep_score(subscores, reg)
    if value is None:
        return None
    return {
        "value": value,
        "weights": weights,
        "subscores": subscores,
        "coverage": sum(reg.param(_WEIGHT_PARAMS[k2]) for k2 in weights),
        "sri": sri_value,
        "timing_sd_min": timing_sd,
        "debt_h": trailing_debt(day, nights, need_h, reg),
        "asleep_min": n.min_asleep,
        "efficiency": n.efficiency,
        "deep_min": n.min_deep,
        "rem_min": n.min_rem,
    }
