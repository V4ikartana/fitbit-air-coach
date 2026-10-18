"""Weight: one weigh-in per day, an EWMA trend (10-day half-life), weekly rate and BMI."""
from __future__ import annotations

from datetime import date, timedelta

from .evidence import Registry


def daily_weighins(rows: list[tuple[str, str, str, float]]) -> list[tuple[str, float]]:
    """Sample rows (local_date, ts_utc, metric, kg) -> [(date, kg)] keeping each day's last weigh-in, sorted."""
    last: dict[str, tuple[str, float]] = {}
    for local_date, ts, _metric, kg in rows:
        if kg is not None and (local_date not in last or ts >= last[local_date][0]):
            last[local_date] = (ts, kg)
    return sorted((d, v[1]) for d, v in last.items())


def trend_series(weighins: list[tuple[str, float]], reg: Registry) -> dict[str, float]:
    """EWMA trend at each weigh-in date; alpha = 1 - 0.5 ** (days since previous / half-life)."""
    half_life = reg.param("weight_half_life_days")
    out: dict[str, float] = {}
    prev_day: date | None = None
    trend = 0.0
    for d, kg in weighins:
        day = date.fromisoformat(d)
        if prev_day is None:
            trend = kg
        else:
            alpha = 1 - 0.5 ** ((day - prev_day).days / half_life)
            trend += alpha * (kg - trend)
        out[d] = trend
        prev_day = day
    return out


def _trend_at(trend: dict[str, float], day: str) -> float | None:
    earlier = [d for d in trend if d <= day]
    return trend[max(earlier)] if earlier else None


def _slope_per_day(points: list[tuple[date, float]]) -> float:
    """Least-squares slope (kg/day) of weigh-ins against date; needs at least two distinct dates."""
    xs = [p[0].toordinal() for p in points]
    mx, my = sum(xs) / len(xs), sum(p[1] for p in points) / len(points)
    sxx = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (p[1] - my) for x, p in zip(xs, points)) / sxx


def weight_status(weighins: list[tuple[str, float]], day: str, reg: Registry) -> dict | None:
    """Trend, weekly rate (needs enough recent weigh-ins) and data counts at `day`; None if no recent weigh-in.

    The rate is the least-squares slope of the raw weigh-ins in the inclusive window [day-(window-1), day] times 7,
    so one noisy weigh-in or a stale anchor cannot fake a fast loss. The EWMA is kept only for `trend_kg`.
    """
    upto = [w for w in weighins if w[0] <= day]
    if not upto:
        return None
    d = date.fromisoformat(day)
    if (d - date.fromisoformat(upto[-1][0])).days > reg.param("weight_latest_max_age_days"):
        return None
    trend = trend_series(upto, reg)
    now = _trend_at(trend, day)
    window = int(reg.param("weight_rate_window_days"))
    start = (d - timedelta(days=window - 1)).isoformat()
    recent = [(date.fromisoformat(w[0]), w[1]) for w in upto if w[0] >= start]
    rate_kg = rate_pct = None
    if len(recent) >= reg.param("weight_min_weighins") and (recent[-1][0] - recent[0][0]).days >= 7:
        rate_kg = _slope_per_day(recent) * 7
        rate_pct = rate_kg / now * 100
    return {"trend_kg": now, "rate_kg_week": rate_kg, "rate_pct_week": rate_pct, "n_recent": len(recent),
            "latest_date": upto[-1][0]}


def bmi(kg: float | None, height_m: float | None) -> float | None:
    return None if kg is None or not height_m else kg / height_m ** 2
