"""Body age: per-habit all-cause hazard ratios vs a typical same-age man, converted to years (Gompertz)."""
from __future__ import annotations

import math

from .evidence import Registry

DOMAINS = {"fitness": ("vo2", "rhr"), "activity": ("steps", "mvpa", "strength"), "sleep": ("sleep_min", "sri"),
           "body": ("bmi",)}
REFERENCE = {"vo2": "vo2_ref", "rhr": "rhr_ref", "steps": "steps_ref", "mvpa": "mvpa_ref", "strength": "strength_ref",
             "sleep_min": "sleep_ref_min", "sri": "sri_ref", "bmi": "bmi_ref"}
TABLES = {"steps": "ding2025_steps", "mvpa": "arem2015_mvpa", "strength": "momma2022_strength",
          "sleep_min": "cappuccio2010_sleep", "sri": "windred2024_sri", "bmi": "gbmc2016_bmi"}
EVIDENCE = {"vo2": ["kodama2009_crf", "kaminsky2022_friend"], "rhr": ["zhang2016_rhr"], "steps": ["ding2025_steps"],
            "mvpa": ["arem2015_pa", "tudorlocke2019_cadence"], "strength": ["momma2022_strength"], "sleep_min": ["cappuccio2010_sleep"],
            "sri": ["windred2024_sri_mortality"], "bmi": ["gbmc2016_bmi"],
            "_method": ["pang2021_hr_to_age", "whoop_age_method", "design_body_age_overlap", "design_reference_person"]}
# always computable (0 when nothing logged), so it doesn't count toward the minimum
NOT_COUNTED = frozenset({"strength"})
# Estimated components are shown but never scored: an estimate derived from another input would double count it.
ESTIMATED_REASON = {"vo2": "derived from resting HR"}
ESTIMATED_EVIDENCE = {"vo2": ["uth2004_vo2max", "tanaka2001_hrmax"]}
CAVEAT = ("Population-statistics estimate from studies of mostly middle-aged adults; not a medical assessment. "
          "The combination model is not independently validated.")
CAVEAT_EXTRAPOLATED = " At your age this describes a risk profile, not a precise age."


def _hazard(component: str, x: float, reg: Registry) -> float:
    if component == "vo2":
        return reg.param("vo2_hr_per_met") ** (x / reg.param("vo2_met_ml"))
    if component == "rhr":
        return reg.param("rhr_hr_per_10bpm") ** (x / 10)
    return reg.table(TABLES[component]).value(x)


def component_years(component: str, value: float, reg: Registry) -> float:
    """ln(HR(person) / HR(reference)) / b."""
    ratio = _hazard(component, value, reg) / _hazard(component, reg.param(REFERENCE[component]), reg)
    return math.log(ratio) / reg.param("gompertz_b")


def _plausible(component: str, x: object, reg: Registry) -> bool:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return False
    if component == "vo2":
        return reg.param("plaus_vo2_min") <= x <= reg.param("plaus_vo2_max")
    if component == "rhr":
        return reg.param("plaus_rhr_min") <= x <= reg.param("plaus_rhr_max")
    if component == "bmi":
        return reg.param("plaus_bmi_min") <= x <= reg.param("plaus_bmi_max")
    if component == "steps":
        return 0 <= x <= reg.param("plaus_steps_max")
    if component == "mvpa":
        return 0 <= x <= reg.param("plaus_mvpa_max")
    if component == "strength":
        return x >= 0
    if component == "sleep_min":
        return 0 < x <= reg.param("plaus_sleep_min_max")
    return -100 <= x <= 100  # sri


def _domain_total(vals: list[float], w: float) -> float:
    """Sign-separated overlap: the strongest harm and the strongest benefit count fully, the rest at weight w.

    Separating by sign keeps the total continuous: sorting by |delta| alone would jump when a harm and a benefit
    swap rank."""
    total = 0.0
    for group in (sorted((v for v in vals if v > 0), reverse=True), sorted(v for v in vals if v < 0)):
        if group:
            total += group[0] + w * sum(group[1:])
    return total


def _total(years: dict[str, float], reg: Registry) -> tuple[float, dict[str, float]]:
    w = reg.param("bodyage_overlap_weight")
    domains: dict[str, float] = {}
    for name, members in DOMAINS.items():
        vals = [years[m] for m in members if m in years]
        if vals:
            domains[name] = _domain_total(vals, w)
    return sum(domains.values()), domains


def _whole(x: float) -> int:
    """Round half up (Python's round() sends 36.5 to 36)."""
    return math.floor(x + 0.5)


def _result(status: str, age_value: float | None = None, reason: str | None = None, uncertainty: float | None = None,
            confidence: float = 0.0, clamped: bool = False, components: dict | None = None,
            domains: dict | None = None, levers: dict | None = None, flags: list[str] | None = None,
            ignored: list[str] | None = None, caveat: str = CAVEAT, levers_unclamped: bool = False,
            bounds: tuple[float, float] | None = None) -> dict:
    display = None
    if age_value is not None and uncertainty is not None:
        lo, hi = bounds if bounds is not None else (-math.inf, math.inf)
        shown = _whole(age_value)
        if clamped and age_value >= hi:
            label = f"{shown}+"
        elif clamped and age_value <= lo:
            label = f"≤{shown}"
        else:
            label = str(shown)
        display = (f"{label} (range {_whole(max(age_value - uncertainty, lo))}–"
                   f"{_whole(min(age_value + uncertainty, hi))})")
    return {"status": status, "body_age": age_value, "reason": reason, "components": components or {},
            "domains": domains or {}, "levers": levers or {}, "uncertainty": uncertainty, "confidence": confidence,
            "clamped": clamped, "flags": flags or [], "ignored": ignored or [], "caveat": caveat, "display": display,
            "levers_unclamped": levers_unclamped}


def insufficient(reason: str) -> dict:
    """An insufficient_data result with the usual keys, for callers that rule body age out up front."""
    return _result("insufficient_data", reason=reason)


def body_age(age: int | None, inputs: dict[str, float | None], days_of_data: int, reg: Registry, *,
             estimated: frozenset[str]) -> dict:
    """Body age with components, domain totals, clamp, uncertainty, status/confidence and levers.

    `estimated` (required, so a caller can't silently score a resting-HR-derived VO2 twice) names inputs that are
    derived rather than measured (e.g. VO2 max from resting HR): they are listed but neither scored nor counted.
    Implausible or non-finite inputs are treated as missing and reported in `ignored`; unknown input names raise
    ValueError."""
    unknown = sorted(set(inputs) - set(REFERENCE))
    if unknown:
        raise ValueError(f"unknown body age inputs: {unknown}")
    ignored = [k for k, v in inputs.items() if v is not None and not _plausible(k, v, reg)]
    valid = {k: float(v) for k, v in inputs.items() if v is not None and k not in ignored}
    present = {k: v for k, v in valid.items() if k not in estimated}
    counted = [k for k in present if k not in NOT_COUNTED]
    minimum = int(reg.param("bodyage_min_components"))
    reason = None
    if age is None:
        reason = "age unknown"
    elif age < reg.param("bodyage_min_age"):
        reason = f"body age needs age {int(reg.param('bodyage_min_age'))}+"
    elif days_of_data < reg.param("bodyage_min_days"):
        reason = f"needs {int(reg.param('bodyage_min_days'))} days of data, have {days_of_data}"
    elif len(counted) < minimum:
        reason = f"needs {minimum} components, have {len(counted)}"
    if reason is not None:
        return _result("insufficient_data", reason=reason, ignored=ignored)

    years = {k: component_years(k, v, reg) for k, v in present.items()}
    total, domains = _total(years, reg)
    lo, hi = age - reg.param("bodyage_clamp_below"), age + reg.param("bodyage_clamp_above")
    raw = age + total
    value = min(max(raw, lo), hi)
    clamped = value != raw
    provisional = days_of_data < reg.param("bodyage_provisional_days")
    extrapolated = age < reg.param("bodyage_extrapolation_age")
    uncertainty = max(reg.param("bodyage_uncertainty_min"),  # a near-reference total isn't precise
                      reg.param("bodyage_uncertainty_frac") * sum(abs(v) for v in domains.values()))
    if provisional:
        uncertainty += reg.param("bodyage_provisional_extra")
    if extrapolated:
        uncertainty *= reg.param("bodyage_extrapolation_factor")

    def with_change(component: str, new_value: float) -> float:
        """Change in the (unclamped) total if one input changed, so levers stay informative at the clamp."""
        changed = dict(years)
        changed[component] = component_years(component, new_value, reg)
        return _total(changed, reg)[0] - total

    levers: dict[str, float] = {}
    if "steps" in present and present["steps"] < reg.param("lever_steps"):
        levers["steps_7000"] = with_change("steps", reg.param("lever_steps"))
    if "rhr" in present:
        floor = reg.param("lever_rhr_floor")
        target = present["rhr"] - reg.param("lever_rhr_drop")
        if target >= floor:
            levers["rhr_minus_10"] = with_change("rhr", target)
        elif present["rhr"] > floor:
            levers["rhr_to_floor"] = with_change("rhr", floor)
    if "mvpa" in present and present["mvpa"] < reg.param("lever_mvpa"):
        levers["mvpa_150"] = with_change("mvpa", reg.param("lever_mvpa"))
    if "bmi" in present:
        delta = with_change("bmi", present["bmi"] - reg.param("lever_bmi_drop"))
        if delta <= 0:  # never suggest a change that the evidence says is worse (e.g. underweight)
            levers["bmi_minus_2"] = delta
    if "vo2" in present:
        levers["vo2_plus_3_5"] = with_change("vo2", present["vo2"] + reg.param("lever_vo2_gain"))

    components: dict[str, dict] = {k: {"value": present[k], "years": years[k], "evidence": EVIDENCE[k]}
                                   for k in present}
    for k in valid:
        if k in estimated:
            components[k] = {"value": valid[k], "years": None, "excluded": ESTIMATED_REASON.get(k, "estimated"),
                             "evidence": EVIDENCE[k] + ESTIMATED_EVIDENCE.get(k, [])}
    return _result(
        "provisional" if provisional else "ok", age_value=value, uncertainty=uncertainty, clamped=clamped,
        confidence=min(1.0, days_of_data / reg.param("bodyage_full_confidence_days")), components=components,
        domains=domains, levers=levers, flags=["age_extrapolated"] if extrapolated else [], ignored=ignored,
        caveat=CAVEAT + (CAVEAT_EXTRAPOLATED if extrapolated else ""), levers_unclamped=clamped, bounds=(lo, hi))
