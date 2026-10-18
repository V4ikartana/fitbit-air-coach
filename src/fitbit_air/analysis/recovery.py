"""Recovery (readiness) score from personal HRV, resting HR and sleep deviations (transparent design choice)."""
from __future__ import annotations

from .evidence import Registry
from .mathutil import lin, phi


def _component(z: float) -> float:
    return 100.0 * phi(z)


def recovery_score(hrv_roll_z: float, hrv_night_z: float | None, rhr_z: float, sleep_last: float | None,
                   debt_h: float | None, reg: Registry
                   ) -> tuple[float, dict[str, float | None], dict[str, float | None]]:
    """0-100. HRV: higher is better; resting HR: lower is better; sleep: last night's score plus 7-day debt.

    Returns (value, components, weights actually applied). The sleep component uses whichever of last night's
    score and the debt score is available; with neither it is dropped (comps["sleep"] and weights["sleep"] are
    None) and the HRV and resting-HR weights are renormalised."""
    w_roll = reg.param("rec_hrv_roll_weight")
    hrv = _component(hrv_roll_z) if hrv_night_z is None else (
        w_roll * _component(hrv_roll_z) + (1 - w_roll) * _component(hrv_night_z))
    rhr = _component(-rhr_z)
    debt_score = None if debt_h is None else lin(debt_h, 0, 100, reg.param("rec_debt_zero_h"), 0)
    w_last = reg.param("rec_sleep_last_weight")
    if sleep_last is not None and debt_score is not None:
        sleep: float | None = w_last * sleep_last + (1 - w_last) * debt_score
    else:
        sleep = sleep_last if sleep_last is not None else debt_score
    w_hrv, w_rhr = reg.param("rec_w_hrv"), reg.param("rec_w_rhr")
    if sleep is None:
        weights: dict[str, float | None] = {"hrv": w_hrv / (w_hrv + w_rhr), "rhr": w_rhr / (w_hrv + w_rhr),
                                            "sleep": None}
        value = weights["hrv"] * hrv + weights["rhr"] * rhr
    else:
        weights = {"hrv": w_hrv, "rhr": w_rhr, "sleep": reg.param("rec_w_sleep")}
        value = w_hrv * hrv + w_rhr * rhr + weights["sleep"] * sleep
    return value, {"hrv": hrv, "rhr": rhr, "sleep": sleep}, weights


def band(value: float, reg: Registry) -> str:
    if value >= reg.param("band_green"):
        return "green"
    if value <= reg.param("band_red"):
        return "red"
    return "yellow"
