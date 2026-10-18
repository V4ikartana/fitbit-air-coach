"""Absolute safety rails: independent of personal baselines, shown above everything else."""
from __future__ import annotations

from datetime import date, timedelta

from .evidence import Registry

URGENT = " Seek urgent care if you have chest pain, fainting or severe breathlessness."


def evaluate(day: str, rhr: dict[str, float], spo2: dict[str, float], alerts: list[tuple[str, str]],
             strain_alert_run: int, reg: Registry) -> list[tuple[str, str, str]]:
    """Return (rail_id, title, detail) for each rail triggered in the window ending `day`.

    `strain_alert_run` is the StrainTracker's count of consecutive nights at "alert" severity.
    Implausible rhr / spo2 values (outside the plaus_* ranges) are ignored.
    """
    d = date.fromisoformat(day)
    window = {(d - timedelta(days=i)).isoformat() for i in range(int(reg.param("safety_window_days")))}
    n_days = len(window)
    out: list[tuple[str, str, str]] = []
    afib_dates = sorted(local_date for local_date, kind in alerts
                        if local_date in window and kind == "irregular_rhythm")
    if afib_dates:
        out.append(("afib_notification", "Irregular heart rhythm notification",
                    f"Your Fitbit Air sent an irregular rhythm notification on {afib_dates[-1]}. This isn't a "
                    "diagnosis, but it should be checked by a clinician." + URGENT))
    high = [k for k in window if k in rhr and reg.param("plaus_rhr_min") <= rhr[k] <= reg.param("plaus_rhr_max")
            and rhr[k] > reg.param("safety_rhr_bpm")]
    if len(high) >= reg.param("safety_rhr_nights"):
        out.append(("rhr_high", "Persistently high resting heart rate",
                    f"Resting heart rate was above {reg.param('safety_rhr_bpm'):.0f} bpm on {len(high)} nights in the "
                    f"last {n_days} days. Worth a check-up with a doctor." + URGENT))
    low = [k for k in window if k in spo2 and reg.param("plaus_spo2_min") <= spo2[k] <= reg.param("plaus_spo2_max")
           and spo2[k] < reg.param("safety_spo2_pct")]
    if len(low) >= reg.param("safety_spo2_nights"):
        out.append(("spo2_low", "Low overnight oxygen",
                    f"Average overnight SpO2 was below {reg.param('safety_spo2_pct'):.0f}% on {len(low)} nights in the "
                    f"last {n_days} days. This can be a fit/sensor issue or sleep-disordered breathing; worth "
                    "discussing with a doctor." + URGENT))
    if strain_alert_run > reg.param("safety_strain_days"):
        out.append(("strain_persistent", "Strain signals have persisted",
                    f"Strain alerts have been active for {strain_alert_run} consecutive days. Consider medical advice."
                    + URGENT))
    return out
