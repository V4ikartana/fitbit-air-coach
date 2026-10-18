"""Strain signals (illness early warning) and the experimental daytime stress index."""
from __future__ import annotations

import math
from datetime import datetime

from .baseline import Baseline, zscore
from .evidence import Registry


FAMILY = {"rhr_up": "rhr", "temp_up": "temp", "hrv_down": "hrv", "resp_up": "breathing", "spo2_down": "breathing"}
_CONDITION_FAMILY = {"multiple_flags": "multiple", "rhr_2_nights": "rhr", "rhr_drift": "rhr"}


def strain_flags(*, rhr: float | None, rhr_b: Baseline | None, resp: float | None, resp_b: Baseline | None,
                 temp: float | None, temp_b: Baseline | None, google_temp_dev: float | None, hrv_status: str | None,
                 hrv_night_z: float | None, spo2: float | None, spo2_b: Baseline | None, reg: Registry) -> list[str]:
    out: list[str] = []
    if rhr is not None and not reg.param("plaus_rhr_min") <= rhr <= reg.param("plaus_rhr_max"):
        rhr = None
    if spo2 is not None and not reg.param("plaus_spo2_min") <= spo2 <= reg.param("plaus_spo2_max"):
        spo2 = None
    if rhr is not None and rhr_b is not None and zscore(rhr, rhr_b) >= reg.param("sig_rhr_z") \
            and rhr >= rhr_b.center + reg.param("sig_rhr_bpm"):
        out.append("rhr_up")
    if resp is not None and resp_b is not None and resp >= resp_b.center + reg.param("sig_resp_br") \
            and zscore(resp, resp_b) >= reg.param("sig_resp_z"):
        out.append("resp_up")
    if (google_temp_dev is not None and google_temp_dev >= reg.param("sig_temp_dev_c")) or (
            temp is not None and temp_b is not None and zscore(temp, temp_b) >= reg.param("sig_temp_z")):
        out.append("temp_up")
    if hrv_status == "below" and hrv_night_z is not None and hrv_night_z <= reg.param("sig_hrv_z"):
        out.append("hrv_down")
    if spo2 is not None and spo2_b is not None and spo2 <= spo2_b.center - reg.param("sig_spo2_drop"):
        out.append("spo2_down")
    return out


def cusum_step(s_prev: float, z: float, k: float) -> float:
    """Upper one-sided CUSUM accumulator (Page 1954): max(0, s_prev + z - k)."""
    return max(0.0, s_prev + z - k)


class StrainTracker:
    """Walks nights in order and turns flags into watch/alert conditions."""

    def __init__(self, reg: Registry):
        self._k, self._h = reg.param("cusum_k"), reg.param("cusum_h")
        self._persist = int(reg.param("strain_persist_nights"))
        self.idle()

    def idle(self) -> None:
        """Forget all state (no baseline / data too sparse: the sequence is broken)."""
        self._s = 0.0
        self._drifting = False
        self._prev_rhr_up = False
        self.run = 0
        self.alert_run = 0

    def skip(self) -> None:
        """A night without data inside an established baseline: no consecutive-night pair across the gap,
        but the CUSUM, run and alert_run carry on."""
        self._prev_rhr_up = False

    def step(self, flags: list[str], rhr_z: float | None) -> tuple[str | None, list[str], int]:
        conditions: list[str] = []
        if len({FAMILY[f] for f in flags}) >= 2:
            conditions.append("multiple_flags")
        if "rhr_up" in flags and self._prev_rhr_up:
            conditions.append("rhr_2_nights")
        if rhr_z is not None:
            if self._drifting and rhr_z <= self._k:
                self._s, self._drifting = 0.0, False   # back to baseline: the drift episode ends cleanly
            else:
                self._s = cusum_step(self._s, rhr_z, self._k)
                self._drifting = self._s > self._h
                if self._drifting:
                    conditions.append("rhr_drift")     # latched while the drift is still going
        self._prev_rhr_up = "rhr_up" in flags
        was_alert = self.alert_run > 0
        self.run = self.run + 1 if conditions else 0
        if not conditions:
            self.alert_run = 0
            return None, [], self.run
        families = {_CONDITION_FAMILY[c] for c in conditions}
        persisted = self.run >= self._persist and conditions != ["rhr_drift"]   # a slow drift alone stays "watch"
        severity = "alert" if len(families) >= 2 or persisted or was_alert else "watch"
        self.alert_run = self.alert_run + 1 if severity == "alert" else 0
        return severity, conditions, self.run


def _minute_set(intervals: list[tuple[datetime, datetime]]) -> set[int]:
    """Epoch minutes m whose start m*60 lies in some half-open interval [s, e)."""
    out: set[int] = set()
    for s, e in intervals:
        out.update(range(math.ceil(s.timestamp() / 60), math.ceil(e.timestamp() / 60)))
    return out


def _inside(t: datetime, minutes: set[int], intervals: list[tuple[datetime, datetime]]) -> bool:
    ts = t.timestamp()
    if ts % 60 == 0:
        return int(ts // 60) in minutes
    return any(s <= t < e for s, e in intervals)  # off-minute timestamp: fall back to the interval test


def waking_sedentary_bpm(minutes: list[tuple[datetime, float]], sedentary: list[tuple[datetime, datetime]],
                         sleep: list[tuple[datetime, datetime]]) -> list[float]:
    """Per-minute HR values of minutes that fall inside a sedentary interval and outside every sleep interval.

    Intervals are half-open [s, e). Minute-aligned timestamps are tested against precomputed minute sets."""
    sed, slp = _minute_set(sedentary), _minute_set(sleep)
    return [bpm for t, bpm in minutes if _inside(t, sed, sedentary) and not _inside(t, slp, sleep)]


def stress_share(bpm: list[float], baseline_median: float, reg: Registry) -> float | None:
    """Share of waking sedentary minutes more than `stress_margin_bpm` above the personal median (experimental)."""
    if len(bpm) < reg.param("stress_min_minutes"):
        return None
    margin = reg.param("stress_margin_bpm")
    return sum(1 for b in bpm if b > baseline_median + margin) / len(bpm)
