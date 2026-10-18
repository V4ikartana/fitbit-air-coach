"""Personal rolling baselines (median / MAD) and deviations. Uses only the user's own data."""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Baseline:
    center: float
    scale: float
    n: int
    confidence: float


def compute_baseline(series: dict[str, float], day: str, valid_days: set[str], floor: float,
                     window_days: int, full_n: int) -> Baseline | None:
    """Baseline from valid days in [day - window_days, day - 1]; today is never included."""
    if floor <= 0:
        raise ValueError(f"floor must be positive, got {floor}")
    d = date.fromisoformat(day)
    start = d - timedelta(days=window_days)
    in_window = sorted(k for k in series if k in valid_days and start <= date.fromisoformat(k) < d)
    if not in_window:
        return None
    values = [series[k] for k in in_window]
    center = statistics.median(values)
    mad = statistics.median(abs(v - center) for v in values)
    first = date.fromisoformat(in_window[0])
    span = (d - first).days  # calendar days since the first usable day (at most window_days)
    n = len(values)
    confidence = min(1.0, n / full_n) * min(1.0, n / span)
    return Baseline(center=center, scale=max(1.4826 * mad, floor), n=n, confidence=round(confidence, 3))


def zscore(value: float, baseline: Baseline) -> float:
    return (value - baseline.center) / baseline.scale


def rolling_mean(series: dict[str, float], day: str, days: int, min_n: int,
                 valid_days: set[str] | None = None) -> float | None:
    """Mean over (day - days, day], today included; None unless at least `min_n` values exist.

    When `valid_days` is given only those dates count. For HRV the series must already be
    ln-transformed (the band compares ln(RMSSD) means)."""
    d = date.fromisoformat(day)
    values = [v for k, v in series.items()
              if (valid_days is None or k in valid_days) and d - timedelta(days=days) < date.fromisoformat(k) <= d]
    return sum(values) / len(values) if len(values) >= min_n else None


def hrv_band_status(rolling_ln: float, baseline: Baseline, halfwidth_sd: float) -> str:
    """Plews: the 7-day ln(RMSSD) mean against baseline +/- halfwidth_sd x scale."""
    half = halfwidth_sd * baseline.scale
    if rolling_ln < baseline.center - half:
        return "below"
    if rolling_ln > baseline.center + half:
        return "above"
    return "within"
