"""Phase B orchestration: activity, weight, running and body age rows for each emitted day."""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..store import Store
from ..timeutil import parse_ts
from . import activity, bodyage, running, weight
from .data import Night
from .evidence import Registry
from .profile import Profile

EVIDENCE = {
    "steps": ["ding2025_steps"],
    "mvpa": ["who2020_pa", "tudorlocke2019_cadence"],
    "sedentary": ["ekelund2019_sedentary"],
    "weight": ["donnelly2009_acsm_weight", "shcherbina2017_ee_accuracy"],
    "spike": ["frandsen2025_spikes"],
    "weekly": ["design_weekly_progression", "frandsen2025_spikes"],
    "plan": ["riegel1981", "frandsen2025_spikes"],
    "race": ["frandsen2025_spikes", "riegel1981", "design_forecast_cutback", "seiler2010_intensity", "karvonen1957",
             "tanaka2001_hrmax"],
}
CARDIO_TYPE = "CARDIO_WORKOUT"
MEN_ONLY = "calibrated for men only"
TIGHT = "Tight but doable: keep increases to about 10% a week and keep the cutback weeks."
CATCH_UP = ("Don't increase by more than ~10% a week to catch up — consider run-walk on race day or a later race. "
            "Safe distance by race day: {safe:.1f} km.")


@dataclass
class PhaseAInputs:
    """What Phase A already computed, handed to Phase B."""
    rhr: dict[str, float]  # plausible daily resting HR
    rhr_baseline: dict[str, float]  # date -> personal RHR baseline centre
    sleep_min: dict[str, float]  # valid-night minutes asleep by wake date
    sri: dict[str, float]  # date -> SRI when computed
    nights: dict[str, Night]
    age: int | None
    first_data: str  # first date with overnight data
    wear_days: set[str]  # good-wear days (only days whose minute data was loaded)
    history_from: str  # first date whose wear quality and SRI are known
    vo2: dict[str, float] = field(default_factory=dict)  # Google daily VO2 max


@dataclass
class FitnessRows:
    metrics: list[dict] = field(default_factory=list)
    scores: list[dict] = field(default_factory=list)
    insights: list[dict] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)


@dataclass
class _Ctx:
    a: PhaseAInputs
    reg: Registry
    profile: Profile
    today: date
    totals: dict[str, dict[str, float]]
    cadence: dict[str, dict[str, float]]  # {CADENCE_METRIC: {date: WHO-equivalent MVPA minutes}}
    sedentary: dict[str, float]
    sessions: list[dict]
    weighins: list[tuple[str, float]]
    height: float | None
    run_rows: list[dict]
    weekly: dict[str, float]  # session_id -> % over the longest run 7-30 days earlier, when flagged


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def _round_tree(v, nd: int = 2):
    if isinstance(v, float):
        return round(v, nd)
    if isinstance(v, dict):
        return {k: _round_tree(x, nd) for k, x in v.items()}
    if isinstance(v, list):
        return [_round_tree(x, nd) for x in v]
    return v


def _shift(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def compute(store: Store, profile: Profile, reg: Registry, since: date, today: date, a: PhaseAInputs) -> FitnessRows:
    out = FitnessRows()
    ba_window, lag = int(reg.param("bodyage_window_days")), int(reg.param("bodyage_trend_long_days"))
    reach = max(ba_window + lag, int(reg.param("riegel_lookback_days")),
                int(reg.param("weight_latest_max_age_days")) + lag)
    load_from, end = (since - timedelta(days=reach)).isoformat(), today.isoformat()
    interval_rows = store.activity_intervals(("steps", activity.SEDENTARY_METRIC, *activity.AZM_METRICS),
                                             load_from, end)
    sessions = store.exercise_sessions(load_from, end)
    heights = store.sample_values(["height_m"], "1900-01-01", end)
    hr_max = running.hrmax(a.age, profile.hrmax_override, reg)
    run_rows, weekly = _runs(store, reg, a, hr_max, sessions)
    ctx = _Ctx(
        a=a, reg=reg, profile=profile, today=today, totals=activity.daily_totals(interval_rows),
        cadence={activity.CADENCE_METRIC: activity.cadence_mvpa_daily(interval_rows, sessions, reg)},
        sedentary=activity.sedentary_hours(interval_rows, [(n.start_utc, n.end_utc) for n in a.nights.values()]),
        sessions=sessions, weighins=weight.daily_weighins(store.sample_values(["weight_kg"], load_from, end)),
        height=profile.height_m or (heights[-1][3] if heights else None), run_rows=run_rows, weekly=weekly)
    out.runs = [r for r in run_rows if r["local_date"] >= since.isoformat()]
    for i in range((today - since).days + 1):
        ds = (since + timedelta(days=i)).isoformat()
        _activity(ctx, ds, out)
        _weight(ctx, ds, out)
        _running(ctx, ds, out)
        _body_age(ctx, ds, out)
    return out


def _runs(store: Store, reg: Registry, a: PhaseAInputs, hr_max: float | None,
          sessions: list[dict]) -> tuple[list[dict], dict[str, float]]:
    """Run rows for every loaded run (so the spike rule sees the previous 30 days) and weekly build-up flags."""
    hr_cache: dict[str, list[tuple[datetime, float]]] = {}

    def hr_minutes(day: str) -> list[tuple[datetime, float]]:
        if day not in hr_cache:  # local_date .. +1 day, for runs crossing midnight
            hr_cache[day] = [(parse_ts(m + ":00Z"), bpm) for _d, m, bpm in store.hr_minute_means(day, _shift(day, 1))]
        return hr_cache[day]

    run_sessions = sorted((s for s in sessions if running.is_run(s)), key=lambda s: s["start_utc"])
    rows: list[dict] = []
    weekly: dict[str, float] = {}
    for s in run_sessions:
        hr_rest = a.rhr_baseline.get(s["local_date"]) or a.rhr.get(s["local_date"])
        if hr_max and hr_rest and s.get("end_utc"):  # no end time: no reliable HR window
            row = running.run_metrics(s, hr_minutes(s["local_date"]), running.zone_bounds(hr_max, hr_rest), reg)
        else:
            km = (s.get("distance_m") or 0) / 1000 or None
            minutes = s.get("duration_min") or None
            row = {"session_id": s["id"], "local_date": s["local_date"], "distance_km": km, "duration_min": minutes,
                   "pace_min_km": minutes / km if km and minutes else None, "avg_hr": s.get("avg_hr"),
                   **{f"z{z}_min": None for z in range(6)}, "decoupling_pct": None, "decoupling_approx": None,
                   "economy_m_per_beat": None}
        prior = [{"start_utc": p["start_utc"], "distance_km": (p.get("distance_m") or 0) / 1000}
                 for p in run_sessions if p["start_utc"] < s["start_utc"]]  # strictly earlier starts only
        row["spike_pct"], row["spike_class"] = running.spike(row["distance_km"], s["start_utc"], prior, reg)
        pct = running.weekly_progression(row["distance_km"], s["start_utc"], prior, reg)
        if pct is not None:
            weekly[s["id"]] = pct
        rows.append({k: (_r(v, 3) if isinstance(v, float) else v) for k, v in row.items()})
    return rows, weekly


def _metric(out: FitnessRows, ds: str, name: str, value: float | None, nd: int = 2) -> None:
    if value is not None:
        out.metrics.append({"local_date": ds, "metric": name, "value": _r(value, nd), "quality": "good"})


def _insight(out: FitnessRows, ds: str, kind: str, sev: str, title: str, detail: str, evidence: list[str]) -> None:
    out.insights.append({"local_date": ds, "kind": kind, "severity": sev, "title": title, "detail": detail,
                         "evidence_ids": evidence})


def _azm(ctx: _Ctx) -> dict[str, dict[str, float]]:
    return {m: ctx.totals.get(m, {}) for m in activity.AZM_METRICS}


def _activity(ctx: _Ctx, ds: str, out: FitnessRows) -> None:
    reg, wear = ctx.reg, ctx.a.wear_days
    steps_avg = activity.steps_average(ctx.totals.get("steps", {}), ds, wear, reg)
    mvpa = activity.mvpa_week(ctx.cadence, ds, wear, reg, metrics=(activity.CADENCE_METRIC,))
    azm = activity.mvpa_week(_azm(ctx), ds, wear, reg)  # Google's heart-rate-reserve zone minutes, shown only
    sed_days = [ctx.sedentary[d] for d in activity.day_window(ds, int(reg.param("activity_window_days")))
                if d in wear and d in ctx.sedentary]
    sed_avg = sum(sed_days) / len(sed_days) if len(sed_days) >= reg.param("min_activity_days") else None
    strength_min, strength_days = activity.strength_week(ctx.sessions, ds, int(reg.param("bodyage_window_days")))
    _metric(out, ds, "steps_7d", steps_avg, 0)
    _metric(out, ds, "mvpa_week", mvpa, 0)
    _metric(out, ds, "azm_week", azm, 0)
    _metric(out, ds, "sedentary_h", sed_avg, 1)
    _metric(out, ds, "strength_min_week", strength_min, 0)
    _metric(out, ds, "strength_days_week", strength_days, 1)
    if steps_avg is not None and steps_avg < reg.param("steps_benchmark"):
        _insight(out, ds, "activity", "info", f"Steps averaging {steps_avg:,.0f} a day",
                 f"Most of the mortality benefit of walking accrues by about {reg.param('steps_benchmark'):,.0f} "
                 "steps a day.", EVIDENCE["steps"])
    if mvpa is not None and mvpa < reg.param("mvpa_target_min"):
        _insight(out, ds, "activity", "info", f"{mvpa:.0f} moderate-to-vigorous minutes this week",
                 f"Counted from brisk walking ({reg.param('cadence_moderate_spm'):.0f}+ steps/min; "
                 f"{reg.param('cadence_vigorous_spm'):.0f}+ counts double) and workouts without step data. "
                 f"The WHO guideline is {reg.param('mvpa_target_min'):.0f}-{reg.param('mvpa_target_max'):.0f} "
                 "minutes of moderate activity a week (vigorous counts double).",
                 EVIDENCE["mvpa"])
    if sed_avg is not None and sed_avg > reg.param("sedentary_watch_h"):
        _insight(out, ds, "activity", "info", f"About {sed_avg:.1f} sedentary hours a day",
                 "Risk rises steeply beyond about 9-10 sedentary hours a day; short movement breaks help.",
                 EVIDENCE["sedentary"])


def _weight(ctx: _Ctx, ds: str, out: FitnessRows) -> None:
    reg = ctx.reg
    ws = weight.weight_status(ctx.weighins, ds, reg)
    if ws:
        _metric(out, ds, "weight_trend_kg", ws["trend_kg"], 2)
        _metric(out, ds, "weight_rate_pct_week", ws["rate_pct_week"], 2)
        if ws["rate_pct_week"] is not None and ws["rate_pct_week"] < -reg.param("weight_rate_max_pct"):
            _insight(out, ds, "weight", "watch",
                     f"Losing more than {reg.param('weight_rate_max_pct'):g}% of body weight per week",
                     "A gentler pace (0.5-1% a week) protects muscle and supports running training.",
                     EVIDENCE["weight"])
    _metric(out, ds, "bmi", weight.bmi(ws["trend_kg"] if ws else None, ctx.height), 2)
    weekly_slot = date.fromisoformat(ds).weekday() == ctx.today.weekday()  # at most once a week
    if (weekly_slot and "weight" in ctx.profile.goals
            and (not ws or ws["n_recent"] < reg.param("weight_min_weighins"))):
        _insight(out, ds, "weight", "info", "Log your weight a few times a week",
                 f"A trend needs at least {reg.param('weight_min_weighins'):g} weigh-ins in "
                 f"{reg.param('weight_rate_window_days'):g} days (Google Health app or a smart scale).",
                 EVIDENCE["weight"])


_ORDINALS = {2: "second", 3: "third", 4: "fourth"}


def _no_race_reason(profile) -> str:
    if profile.race_date is not None or profile.race_distance_km is not None:
        return "set both race_date and race_distance_km to get a race plan"
    return "no race configured"


def _running(ctx: _Ctx, ds: str, out: FitnessRows) -> None:
    reg, profile = ctx.reg, ctx.profile
    for r in (r for r in ctx.run_rows if r["local_date"] == ds):
        if r["spike_class"]:
            _insight(out, ds, "running", "watch",
                     f"This run was {r['spike_pct']:.0f}% longer than your longest run in "
                     f"{reg.param('spike_lookback_days'):g} days",
                     f"A single run more than {reg.param('spike_pct'):g}% longer than your longest in the previous "
                     f"{reg.param('spike_lookback_days'):g} days raised overuse-injury rates in a 5,200-runner study; "
                     "build distance gradually.", EVIDENCE["spike"])
        if r["session_id"] in ctx.weekly:
            cap, every = reg.param("spike_pct"), reg.param("forecast_cutback_every")
            nth = _ORDINALS.get(int(every), f"{every:g}th")
            _insight(out, ds, "running", "watch", f"Faster build-up than about +{cap:g}% a week",
                     f"This run was {ctx.weekly[r['session_id']]:.0f}% longer than your longest run from one to "
                     f"four weeks earlier. Keep the build-up near {cap:g}% a week, with a cutback week every {nth} "
                     "week.", EVIDENCE["weekly"])
    balance = running.intensity_balance(ctx.run_rows, ds, reg)
    _metric(out, ds, "intensity_easy_share", balance, 2)
    if not profile.has_race:  # runs are still recorded above; there is simply no race plan to score
        out.scores.append({"local_date": ds, "score": "race", "value": None, "band": None, "confidence": None,
                           "status": "insufficient_data", "evidence_ids": EVIDENCE["race"],
                           "components": {"status": "no_race", "reason": _no_race_reason(profile)}})
        return
    fc = running.forecast(ctx.run_rows, ds, profile.race_date, profile.race_distance_km, reg)
    est = running.riegel_estimate(ctx.run_rows, ds, profile.race_distance_km, reg)
    # Never store a date already behind the analysed day (a missed first-run date is reported as weeks short).
    fc = {k: (None if k in ("first_run_by", "latest_start") and v is not None and v < ds else v) for k, v in fc.items()}
    components = _round_tree({**fc, "riegel_min": est, "race_date": profile.race_date,
                              "race_km": profile.race_distance_km, "easy_share": balance})
    if fc["status"] in ("no_runs", "no_runs_behind"):
        components["cardio_distance_sessions"] = _cardio_with_distance(ctx, ds)
    out.scores.append({"local_date": ds, "score": "race", "value": _r(est, 1), "band": fc["status"],
                       "confidence": None, "status": "ok", "components": components,
                       "evidence_ids": EVIDENCE["race"]})
    if profile.race_date <= ds:  # race day reached: no more plan advice
        return
    km = f"{profile.race_distance_km:g} km"
    catch_up = CATCH_UP.format(safe=fc["safe_by_race_km"])
    if fc["status"] in ("tight", "behind"):
        advice = TIGHT if fc["status"] == "tight" else catch_up  # run-walk / later race only when behind
        _insight(out, ds, "running", "watch", f"{km} plan is {fc['status']}",
                 f"Building from {fc['planning_km']:.1f} km needs {fc['increments']} weekly +10% steps "
                 f"({fc['weeks_needed']} weeks with cutbacks); {fc['weeks_left']} weeks remain. {advice}",
                 EVIDENCE["plan"])
    elif fc["status"] == "no_runs_behind":
        _insight(out, ds, "running", "watch", f"{km} plan: no runs yet and about {fc['weeks_short']} weeks behind",
                 f"A gradual build from a first {fc['first_run_km']:g} km run no longer fits before race day. "
                 f"{catch_up}",
                 EVIDENCE["plan"])


def _cardio_with_distance(ctx: _Ctx, ds: str) -> int:
    """Cardio workouts with a distance in the run lookback: likely runs logged under the wrong activity type."""
    start = _shift(ds, -(int(ctx.reg.param("spike_lookback_days")) - 1))
    return sum(1 for s in ctx.sessions if (s.get("exercise_type") or "").upper() == CARDIO_TYPE
               and start <= s["local_date"] <= ds and (s.get("distance_m") or 0) > 0)


def _body_age_result(ctx: _Ctx, ds: str) -> tuple[dict, dict]:
    """Body age for `ds` from rolling 30-day inputs (also used 30 days back for the trend), plus facts for the
    report: which inputs were missing and whether height and a recent weigh-in exist (what BMI needs)."""
    reg, a = ctx.reg, ctx.a
    if ctx.profile.sex != "male":  # every reference value and anchor is for men
        return bodyage.insufficient(MEN_ONLY), {}
    days = int(reg.param("bodyage_window_days"))
    window, min_days = activity.day_window(ds, days), reg.param("bodyage_min_days")
    rhr_vals = [a.rhr[d] for d in window if d in a.rhr]
    sleep_vals = [a.sleep_min[d] for d in window if d in a.sleep_min]
    sri_vals = [a.sri[d] for d in window if d in a.sri]
    vo2_days = [d for d in window if d in a.vo2]
    rhr_med = statistics.median(rhr_vals) if len(rhr_vals) >= min_days else None
    hr_max = running.hrmax(a.age, ctx.profile.hrmax_override, reg)
    vo2 = a.vo2[max(vo2_days)] if vo2_days else running.uth_vo2max(hr_max, rhr_med, reg)
    estimated = frozenset() if vo2_days or vo2 is None else frozenset({"vo2"})  # Uth VO2 is derived from RHR
    ws = weight.weight_status(ctx.weighins, ds, reg)
    inputs = {
        "vo2": vo2,
        "rhr": rhr_med,
        "steps": activity.worn_day_mean(ctx.totals.get("steps", {}), ds, days, a.wear_days, min_days),
        "mvpa": activity.mvpa_week(ctx.cadence, ds, a.wear_days, reg, window_days=days, min_days=min_days,
                                   metrics=(activity.CADENCE_METRIC,)),
        "strength": activity.strength_week(ctx.sessions, ds, days)[0],
        "sleep_min": sum(sleep_vals) / len(sleep_vals) if len(sleep_vals) >= min_days else None,
        "sri": sum(sri_vals) / len(sri_vals) if sri_vals else None,
        "bmi": weight.bmi(ws["trend_kg"] if ws else None, ctx.height),
    }
    days_of_data = (date.fromisoformat(ds) - date.fromisoformat(a.first_data)).days + 1
    facts = {"missing": sorted(k for k, v in inputs.items() if v is None), "have_height": bool(ctx.height),
             "have_weight": ws is not None}
    return bodyage.body_age(a.age, inputs, days_of_data, reg, estimated=estimated), facts


def _scored(result: dict) -> set[str]:
    return {k for k, v in result.get("components", {}).items() if v.get("years") is not None}


def _change(ctx: _Ctx, ds: str, result: dict, lag: int) -> float | None:
    """Body age today minus body age `lag` days ago (age held fixed, so 0 means ageing at the calendar rate).

    Not annualised: small table steps would be multiplied into noise. Compares like with like: needs lag + window
    days of history, an earlier window whose wear days and SRI are known, unclamped values at both ends and the
    same scored components (otherwise a newly available input would masquerade as a change)."""
    window = int(ctx.reg.param("bodyage_window_days"))
    earlier = _shift(ds, -lag)
    days_of_data = (date.fromisoformat(ds) - date.fromisoformat(ctx.a.first_data)).days + 1
    if (result.get("body_age") is None or result.get("clamped") or days_of_data < lag + window
            or _shift(earlier, -(window - 1)) < ctx.a.history_from):
        return None
    past, _facts = _body_age_result(ctx, earlier)
    if past.get("body_age") is None or past.get("clamped") or _scored(past) != _scored(result):
        return None
    return result["body_age"] - past["body_age"]


def _body_age(ctx: _Ctx, ds: str, out: FitnessRows) -> None:
    result, facts = _body_age_result(ctx, ds)
    comps = result.get("components") or {}
    evidence = sorted({e for c in comps.values() for e in c.get("evidence", [])} | set(bodyage.EVIDENCE["_method"]))
    components = _round_tree({
        "components": comps, "domains": result.get("domains") or {}, "levers": result.get("levers") or {},
        "uncertainty": result.get("uncertainty"), "clamped": result.get("clamped", False),
        "levers_unclamped": result.get("levers_unclamped", False), "flags": result.get("flags") or [],
        "ignored": result.get("ignored") or [], "display": result.get("display"), "caveat": result.get("caveat"),
        "age": ctx.a.age, "reason": result.get("reason"), **facts})
    for key, param in (("change_30d", "bodyage_trend_short_days"), ("change_90d", "bodyage_trend_long_days")):
        change = _change(ctx, ds, result, int(ctx.reg.param(param)))
        if change is not None:
            components[key] = _r(change, 2)
    out.scores.append({"local_date": ds, "score": "body_age", "value": _r(result.get("body_age"), 1), "band": None,
                       "confidence": _r(result.get("confidence"), 2), "status": result["status"],
                       "components": components, "evidence_ids": evidence})
