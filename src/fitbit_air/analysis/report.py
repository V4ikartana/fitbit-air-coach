"""Plain-text daily report for `fitbit-air report`."""
from __future__ import annotations

from datetime import date, timedelta

from ..store import Store
from .bodyage import CAVEAT_EXTRAPOLATED
from .evidence import Registry, load_registry

DISCLAIMER = "Not medical advice: statistical estimates from a consumer wearable."
LOW_CONFIDENCE = 0.5
STRESS_DAYS = 7  # the experimental stress index is shown only as a mean over this many days
STRESS_MIN_VALUES = 3
PHASE_B_SCORES = ("body_age", "race")  # rendered in their own sections, not the generic Scores list
BODY_AGE_CAVEAT = ("Body age is a population-statistics estimate, not a medical assessment; the combination model "
                   "is not independently validated.")
COMPONENT_LABELS = {"vo2": "VO2 max", "rhr": "resting HR", "steps": "steps", "mvpa": "brisk minutes",
                    "strength": "strength", "sleep_min": "sleep", "sri": "sleep regularity", "bmi": "BMI"}
MISSING_HINTS = {"rhr": "needs resting HR on at least 7 nights in 30 (wear the band overnight)",
                 "steps": "wear the band during the day", "mvpa": "wear the band during the day",
                 "sleep_min": "needs at least 7 valid nights in 30 (wear the band overnight)",
                 "sri": "needs nightly wear for several nights in a row",
                 "vo2": "shown when Google reports a VO2 max or resting HR is known"}
LEVER_LABELS = {"steps_7000": "7,000 steps a day", "rhr_minus_10": "resting HR 10 bpm lower",
                "rhr_to_floor": "resting HR down to {floor:g}", "mvpa_150": "150 brisk minutes a week",
                "bmi_minus_2": "BMI 2 points lower", "vo2_plus_3_5": "VO2 max +3.5"}


def _subscores(components: dict) -> dict[str, float | None]:
    if "subscores" in components:
        return components["subscores"]
    return {k: v.get("score") for k, v in components.items() if isinstance(v, dict) and "score" in v}


def render(store: Store, day: str, reg: Registry | None = None) -> str:
    reg = reg or load_registry()
    a = store.analysis_for_day(day)
    lines = [f"Fitbit Air report for {day}", ""]
    safety = [i for i in a["insights"] if i["kind"] == "safety"]
    if safety:
        lines.append("SAFETY")
        for i in safety:
            lines += [f"  ! {i['title']}", f"    {i['detail']}"]
        lines.append("")
    lines.append("Scores")
    if not a["scores"]:
        lines.append("  No analysis for this date yet. Run: fitbit-air analyze "
                     "(dates older than 60 days need: fitbit-air analyze --since YYYY-MM-DD)")
    for s in a["scores"]:
        if s["score"] in PHASE_B_SCORES:
            continue
        name = s["score"].capitalize()
        if s["status"] == "ok":
            band = f" ({s['band']})" if s["band"] else ""
            confidence = s["confidence"] or 0.0
            lines.append(f"  {name:10} {s['value']:5.0f}{band}   confidence {confidence:.0%}")
            if confidence < LOW_CONFIDENCE:
                lines.append("      (low confidence: baselines still building)")
            for key, value in _subscores(s["components"]).items():
                line = f"      {key:12} {'  -' if value is None else f'{value:5.0f}'}"
                band_status = s["components"].get(key, {}).get("band_status") if key == "hrv" else None
                if band_status:
                    line += f"   (7-day band: {band_status})"
                lines.append(line)
        else:
            lines.append(f"  {name:10}     -   {s['components'].get('reason', s['status'])}")
    others = [i for i in a["insights"] if i["kind"] != "safety"]
    if others:
        lines += ["", "Findings"]
        for i in others:
            lines += [f"  [{i['severity']}] {i['title']}", f"      {i['detail']}"]
    by_name = {s["score"]: s for s in a["scores"]}
    lines += _activity_lines({m["metric"]: m["value"] for m in a["metrics"]}, reg)
    lines += _race_lines(by_name.get("race"), day, reg)
    lines += _body_age_lines(by_name.get("body_age"), reg)
    first = (date.fromisoformat(day) - timedelta(days=STRESS_DAYS - 1)).isoformat()
    stress = [v for _, v in store.metric_values("stress_share", first, day)]
    if len(stress) >= STRESS_MIN_VALUES:
        lines += ["", f"Experimental stress index ({STRESS_DAYS}-day): {sum(stress) / len(stress):.0%} of waking "
                      "sedentary minutes elevated (trend only)"]
    if reg.overrides():
        lines.append("")
        lines.append("Personal threshold overrides (your settings, not the cited defaults):")
        for name, o in sorted(reg.overrides().items()):
            default = "" if o["default"] is None else f" (default {o['default']:g})"
            value = o["value"] if isinstance(o["value"], list) else f"{o['value']:g}"
            lines.append(f"  {name} = {value}{default}: {o['reason']}")
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


def _activity_lines(m: dict, reg: Registry) -> list[str]:
    parts = []
    if m.get("steps_7d") is not None:
        parts.append(f"steps {m['steps_7d']:,.0f}/day (7-day)")
    if m.get("mvpa_week") is not None:
        parts.append(f"moderate-to-vigorous {m['mvpa_week']:.0f} min/week (brisk walking + workouts)")
    if m.get("azm_week") is not None:
        parts.append(f"Google active zone minutes {m['azm_week']:.0f}/week")
    if m.get("sedentary_h") is not None:
        parts.append(f"sedentary {m['sedentary_h']:.1f} h/day")
    if m.get("strength_days_week") is not None:
        target = reg.param("strength_target_days")
        parts.append(f"strength {m['strength_days_week']:.1f} days/week (target {target:g})")
    if m.get("weight_trend_kg") is not None:
        rate = m.get("weight_rate_pct_week")
        rate_text = f" ({rate:+.1f}%/week)" if rate is not None else ""
        parts.append(f"weight trend {m['weight_trend_kg']:.1f} kg{rate_text}")
    if m.get("bmi") is not None:
        parts.append(f"BMI {m['bmi']:.1f}")
    return ["", "Activity & weight", "  " + "; ".join(parts)] if parts else []


def _race_lines(s: dict | None, day: str, reg: Registry) -> list[str]:
    if not s:
        return []
    c = s["components"]
    if c.get("status") == "no_race":
        reason = c.get("reason") or "no race configured"
        return ["", "Running & race",
                f"  {reason[0].upper()}{reason[1:]}. Set both with `fitbit-air profile set race_date YYYY-MM-DD` "
                "and `race_distance_km`."]
    km = f"{c['race_km']:g} km"
    lines = ["", f"Running & {c['race_km']:g}k"]
    if c.get("race_date") and day == c["race_date"]:
        lines.append("  Race day — good luck! Run your own pace; run-walk is fine.")
        return lines
    if c.get("race_date") and day > c["race_date"]:
        lines.append("  Race date passed; set a new one with `fitbit-air profile set race_date YYYY-MM-DD`")
        return lines
    if c.get("cardio_distance_sessions"):
        lines.append(f"  Cardio sessions with distance found but no runs — log runs as 'Run' so they count toward "
                     f"the {c['race_km']:g}k plan")
    if c["status"] == "no_runs":
        lines.append(f"  No runs logged yet. Start with a {c['first_run_km']:g} km run (run-walk is fine) by "
                     f"{c['first_run_by']} to reach {km} by {c['race_date']} at about +10% a week.")
        return lines
    if c["status"] == "no_runs_behind":
        lines.append(f"  No runs logged yet, and a gradual build from a first {c['first_run_km']:g} km run no longer "
                     f"fits before race day (about {c['weeks_short']} weeks short).")
        lines.append(f"  Building at no more than ~10% a week reaches about {c['safe_by_race_km']:.1f} km by "
                     f"{c['race_date']}; consider run-walk on race day or a later race.")
        return lines
    planned = c.get("planning_km")
    longest = f"longest {c['longest_km']:.1f} km"
    if planned is not None and round(planned, 1) < round(c["longest_km"], 1):
        longest += f", planned from {planned:.1f} km: a jump counts as +10% at most"
    lines.append(f"  Plan: {c['status'].replace('_', ' ')} ({longest}, {c['increments']} weekly +10% steps needed, "
                 f"{c['weeks_left']} weeks left)")
    lines.append(f"  Safe distance by race day: {c['safe_by_race_km']:.1f} km")
    if c.get("riegel_min") is not None:
        h, mnt = divmod(round(c["riegel_min"]), 60)
        lines.append(f"  Estimated {km} time: {h}:{mnt:02d} (Riegel)")
    else:
        lines.append("  A finish-time estimate appears once you've run at least half the race distance.")
    if c.get("easy_share") is not None:
        target = reg.param("intensity_easy_target")
        lines.append(f"  Easy running share (28 days): {c['easy_share']:.0%} (target about {target:.0%})")
    return lines


def _value_text(name: str, value: float) -> str:
    if name == "sleep_min":
        return f"{value / 60:.1f} h"
    if name == "bmi":
        return f"{value:.1f}"
    units = {"rhr": " bpm", "steps": "/day", "mvpa": " min/week", "strength": " min/week", "vo2": " ml/kg/min",
             "sri": ""}
    if name in units:
        return f"{value:,.0f}{units[name]}"
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"


def _body_age_lines(s: dict | None, reg: Registry) -> list[str]:
    if not s:
        return []
    c = s["components"]
    if s["status"] == "insufficient_data" or s.get("value") is None or not c.get("display"):
        return ["", "Body age", f"  - {c.get('reason') or 'not enough data yet'}", *_missing_lines(c),
                f"  {BODY_AGE_CAVEAT}"]
    label = " (provisional)" if s["status"] == "provisional" else ""
    lines = ["", f"Body age {c['display']} vs your age {c.get('age')}{label}"]
    comps = c.get("components") or {}
    scored = sorted(((k, v) for k, v in comps.items() if v.get("years") is not None),
                    key=lambda kv: abs(kv[1]["years"]), reverse=True)
    for name, comp in scored:
        lines.append(f"      {COMPONENT_LABELS.get(name, name):16} {comp['years']:+5.1f} y   "
                     f"(your value {_value_text(name, comp['value'])})")
    for name, comp in comps.items():
        if comp.get("excluded") and comp.get("value") is not None:
            what = "estimated from heart rates" if name == "vo2" else comp["excluded"]
            lines.append(f"      {COMPONENT_LABELS.get(name, name)} ~{comp['value']:.0f} ({what}; shown, not scored)")
    lines += _missing_lines(c)
    if c.get("ignored"):
        lines.append("      ignored (implausible): " + ", ".join(COMPONENT_LABELS.get(k, k) for k in c["ignored"]))
    best = sorted(((k, v) for k, v in (c.get("levers") or {}).items() if v is not None and v < 0),
                  key=lambda kv: kv[1])[:3]
    if best:
        floor = reg.param("lever_rhr_floor")
        lines.append("  Biggest levers: " + ", ".join(
            f"{LEVER_LABELS.get(k, k.replace('_', ' ')).format(floor=floor)} {v:+.1f} y" for k, v in best))
        if c.get("levers_unclamped"):
            lines.append("  (levers are estimated before the cap on body age is applied)")
    lines += _trend_lines(c, reg)
    lines.append(f"  {BODY_AGE_CAVEAT}")
    if "age_extrapolated" in (c.get("flags") or []):
        lines.append(f"  {CAVEAT_EXTRAPOLATED.strip()}")
    return lines


def _missing_lines(c: dict) -> list[str]:
    """One actionable line per body-age input that couldn't be computed."""
    lines = []
    for name in c.get("missing") or []:
        if name == "bmi":
            needs = []
            if not c.get("have_height"):
                needs.append("set height (`fitbit-air profile set height_m 1.75`)")
            if not c.get("have_weight"):
                needs.append("log your weight a few times a week")
            hint = " and ".join(needs) or "needs height and a recent weigh-in"
        else:
            hint = MISSING_HINTS.get(name)
        if hint:
            lines.append(f"  Not included: {COMPONENT_LABELS.get(name, name)} — {hint}")
    return lines


def _trend_lines(c: dict, reg: Registry) -> list[str]:
    """Body-age change (age held fixed), preferring the 90-day comparison; small changes read as stable."""
    for key, days in (("change_90d", 90), ("change_30d", 30)):
        change = c.get(key)
        if change is not None:
            band = reg.param("bodyage_trend_deadband")
            if abs(change) < band:
                return [f"  Trend: stable over the last {days} days (within ±{band:g} y)"]
            return [f"  Trend: {change:+.1f} y over the last {days} days"]
    return []
