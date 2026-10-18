"""Per-day data quality: wear coverage and night validity."""
from __future__ import annotations

from .data import Night
from .evidence import Registry


def day_quality(coverage_minutes: int, reg: Registry) -> str:
    """'good' (>= 70% of the day worn), 'partial' (>= 30%) or 'invalid'."""
    fraction = coverage_minutes / 1440
    if fraction >= reg.param("coverage_good"):
        return "good"
    if fraction >= reg.param("coverage_partial"):
        return "partial"
    return "invalid"


def night_valid(night: Night | None, reg: Registry) -> bool:
    """A staged main sleep of at least 3 hours in bed."""
    return (
        night is not None
        and (night.sleep_type or "").lower() == "stages"
        and (night.min_in_period or 0) >= reg.param("night_min_period_min")
    )
