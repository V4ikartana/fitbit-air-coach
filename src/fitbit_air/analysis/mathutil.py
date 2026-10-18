"""Small numeric helpers shared by the analyses."""
from __future__ import annotations

import math


def lin(x: float, x0: float, y0: float, x1: float, y1: float) -> float:
    """Linear interpolation from (x0, y0) to (x1, y1), clamped to the segment's end values."""
    if not all(math.isfinite(v) for v in (x, x0, y0, x1, y1)):
        raise ValueError("lin inputs must be finite")
    if x1 == x0:
        raise ValueError("x0 and x1 must differ")
    t = min(1.0, max(0.0, (x - x0) / (x1 - x0)))
    return y0 + t * (y1 - y0)


def phi(z: float) -> float:
    """Standard normal cumulative distribution function."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def circular_sd_minutes(minutes_of_day: list[float]) -> float:
    """Circular standard deviation, in minutes, of clock times given as minutes after midnight.

    The result is unbounded (about 1700 min as the mean resultant length r approaches 0, e.g. for exactly
    opposite times); callers map it to a score. r is clamped to [1e-12, 1 - 1e-12] to stay finite.
    """
    if not minutes_of_day:
        raise ValueError("need at least one value")
    if not all(math.isfinite(m) for m in minutes_of_day):
        raise ValueError("clock times must be finite")
    angles = [2 * math.pi * m / 1440 for m in minutes_of_day]
    c = sum(math.cos(a) for a in angles) / len(angles)
    s = sum(math.sin(a) for a in angles) / len(angles)
    r = min(max(math.hypot(c, s), 1e-12), 1 - 1e-12)
    return math.sqrt(-2 * math.log(r)) * 1440 / (2 * math.pi)
