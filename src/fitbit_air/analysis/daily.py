"""Run the analyses for a date range and write metric_daily, scores_daily, insights and runs."""
from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from ..plugins import DayContext, LoadedPlugins, run_daily_plugins
from ..store import Store
from . import baseline as bl
from . import fitness, quality, recovery, safety, signals
from . import sleep as slp
from .baseline import Baseline
from .data import DAILY_SOURCES, Inputs, load_inputs
from .evidence import Registry
from .profile import Profile

log = logging.getLogger(__name__)

# analysis metric -> (daily_metrics source, transform, baseline-floor param)
OVERNIGHT = {
    "hrv": ("hrv_rmssd_ms", math.log, "floor_hrv"),
    "rhr": ("resting_hr", None, "floor_rhr"),
    "resp": ("resp_rate", None, "floor_resp"),
    "temp": ("sleep_temp_c", None, "floor_temp"),
    "spo2": ("spo2_avg_pct", None, "floor_spo2"),
}
EVIDENCE = {
    "sleep": ["watson2015_sleep_duration", "phillips2017_sri", "windred2024_sri_mortality", "buysse2014_ru_sated",
              "design_sri_mapping", "design_sleep_weights"],
    "recovery": ["plews2013_hrv", "buchheit2014_monitoring", "fullagar2015_sleep_athletes", "design_recovery_weights"],
    "hrv_band": ["plews2013_hrv"],
    "strain": ["mishra2020_presymptomatic", "quer2021_wearables_covid", "page1954_cusum"],
    "stress": ["design_stress_index"],
    "safety": ["safety_thresholds"],
    "sleep_debt": ["watson2015_sleep_duration"],
    "baselines": ["design_baseline_minimums"],
}
# Plausibility ranges applied at load: values outside are treated as missing everywhere.
PLAUSIBLE = {"resting_hr": ("plaus_rhr_min", "plaus_rhr_max"), "spo2_avg_pct": ("plaus_spo2_min", "plaus_spo2_max")}
STRAIN_CAUSES = ("Common non-illness causes: alcohol, a late heavy meal, a hard workout, travel or jet lag, "
                 "psychological stress, poor sleep.")
FLAG_TEXT = {"rhr_up": "resting HR", "resp_up": "breathing rate", "temp_up": "sleep temperature",
             "hrv_down": "HRV", "spo2_down": "SpO2"}
FLAG_METRIC = {"rhr_up": "rhr", "resp_up": "resp", "temp_up": "temp", "hrv_down": "hrv", "spo2_down": "spo2"}
UNSCORABLE_NIGHT = "not enough sleep data to score this night"


@dataclass
class AnalysisSummary:
    days: int
    metric_rows: int
    score_rows: int
    insights: int
    runs: int = 0
    error: str | None = None  # a Phase B failure (Phase A rows were still written)
    plugin_problems: list[str] = field(default_factory=list)  # distinct plugin errors (their rows were skipped)
    config_errors: list[str] = field(default_factory=list)  # set by the runner: config files that fell back

    def __str__(self) -> str:
        text = (f"{self.days} days: {self.metric_rows} metrics, {self.score_rows} scores, {self.insights} insights, "
                f"{self.runs} runs")
        if self.plugin_problems:
            text += f"; plugin problems: {len(self.plugin_problems)}"
        return f"{text}; {self.error}" if self.error else text


@dataclass
class _Context:
    """Everything derived once from the loaded inputs."""
    inp: Inputs
    series: dict[str, dict[str, float]]  # analysis metric -> {date: value} (hrv in ln; plausible, finite)
    valid_nights: set[str]
    qual: dict[str, str]  # date -> day-wear quality (only days whose minute data was loaded)
    hrv_roll: dict[str, float]  # date -> 7-day mean of ln(RMSSD) over valid nights
    sed_bpm: dict[str, list[float]]  # good-wear date -> waking sedentary minute HR
    floors: dict[str, float]
    window: int
    full_n: int


@dataclass
class _Day:
    """One day's analysis results, before they are turned into rows."""
    ds: str
    quality: str
    bases: dict[str, Baseline | None]
    z: dict[str, float | None]
    hrv_roll: float | None = None
    hrv_roll_b: Baseline | None = None  # baseline of past 7-day means
    hrv_roll_z: float | None = None
    hrv_trend_ready: bool = False  # the baseline of 7-day means has >= min_n_recovery values
    hrv_status: str | None = None
    hrv_below_run: int = 0  # consecutive days (ending today) with hrv_status "below"
    night: dict | None = None
    night_valid: bool = False
    debt: float | None = None
    n_rec: int = 0
    n_sig: int = 0
    rec: tuple[float, dict[str, float | None], dict[str, float | None]] | None = None
    signals_on: bool = False  # strain signals evaluated tonight (baseline established and RHR present)
    flags: list[str] = field(default_factory=list)
    severity: str | None = None
    conditions: list[str] = field(default_factory=list)
    alert_run: int = 0
    stress: float | None = None


def _dates(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range(max(0, (end - start).days + 1))]


def _r(x: float | None, nd: int = 3) -> float | None:
    return None if x is None else round(x, nd)


def _round_tree(v, nd: int = 2):
    if isinstance(v, float):
        return round(v, nd)
    if isinstance(v, dict):
        return {k: _round_tree(x, nd) for k, x in v.items()}
    return v


def _finite(x: float | None) -> float | None:
    return x if x is not None and math.isfinite(x) else None


def _plausible(values: dict[str, float], lo: float, hi: float) -> dict[str, float]:
    """Drop non-finite values and values outside [lo, hi]."""
    return {d: v for d, v in values.items() if math.isfinite(v) and lo <= v <= hi}


def _filtered_daily(inp: Inputs, reg: Registry) -> dict[str, dict[str, float]]:
    out = {}
    for src, values in inp.daily.items():
        if src in PLAUSIBLE:
            lo, hi = (reg.param(p) for p in PLAUSIBLE[src])
            out[src] = _plausible(values, lo, hi)
        else:
            out[src] = {d: v for d, v in values.items() if math.isfinite(v)}
    return out


def _prepare(store: Store, reg: Registry, load_start: date, minutes_start: date, today: date) -> _Context:
    inp = load_inputs(store, load_start.isoformat(), today.isoformat(), minutes_start=minutes_start.isoformat())
    inp = replace(inp, daily=_filtered_daily(inp, reg))

    series: dict[str, dict[str, float]] = {}
    for m, (src, transform, _) in OVERNIGHT.items():
        raw = inp.daily.get(src, {})
        series[m] = {d: (transform(v) if transform else v) for d, v in raw.items() if transform is None or v > 0}
    valid_nights = {d for d, n in inp.nights.items() if quality.night_valid(n, reg)}
    series["sleep_min"] = {d: inp.nights[d].min_asleep for d in valid_nights if inp.nights[d].min_asleep is not None}
    floors = {m: reg.param(fp) for m, (_, _, fp) in OVERNIGHT.items()} | {"sleep_min": reg.param("floor_sleep_min")}

    all_days = _dates(load_start, today)
    minute_days = _dates(minutes_start, today)
    qual = {d.isoformat(): quality.day_quality(len(inp.hr_minutes.get(d.isoformat(), [])), reg) for d in minute_days}

    # Rolling 7-day ln(RMSSD) means, only where the whole look-back lies inside the loaded range.
    roll_days, roll_min = int(reg.param("hrv_roll_days")), int(reg.param("hrv_roll_min"))
    hrv_roll = {}
    for d in all_days[roll_days - 1:]:
        v = bl.rolling_mean(series["hrv"], d.isoformat(), roll_days, roll_min, valid_days=valid_nights)
        if v is not None:
            hrv_roll[d.isoformat()] = v

    sed_bpm = {}
    for d in minute_days:
        ds = d.isoformat()
        if qual[ds] != "good" or not inp.sedentary.get(ds):
            continue
        near = [inp.nights[w] for w in ((d + timedelta(days=k)).isoformat() for k in range(-1, 3)) if w in inp.nights]
        sed_bpm[ds] = signals.waking_sedentary_bpm(inp.hr_minutes.get(ds, []), inp.sedentary[ds],
                                                   [(n.start_utc, n.end_utc) for n in near])

    return _Context(inp=inp, series=series, valid_nights=valid_nights, qual=qual, hrv_roll=hrv_roll, sed_bpm=sed_bpm,
                    floors=floors, window=int(reg.param("baseline_window_days")),
                    full_n=int(reg.param("baseline_conf_full_n")))


def _baselines_and_hrv(ctx: _Context, day: _Day, reg: Registry) -> None:
    ds = day.ds
    for m, values in ctx.series.items():
        b = bl.compute_baseline(values, ds, ctx.valid_nights, ctx.floors[m], ctx.window, ctx.full_n)
        day.bases[m] = b
        day.z[m] = _finite(bl.zscore(values[ds], b)) if b is not None and ds in values else None
    day.hrv_roll = ctx.hrv_roll.get(ds)
    if day.hrv_roll is None:
        return
    day.hrv_roll_b = bl.compute_baseline(ctx.hrv_roll, ds, set(ctx.hrv_roll), ctx.floors["hrv"], ctx.window,
                                         ctx.full_n)
    if day.hrv_roll_b is not None:
        day.hrv_roll_z = _finite(bl.zscore(day.hrv_roll, day.hrv_roll_b))
        day.hrv_trend_ready = day.hrv_roll_z is not None and day.hrv_roll_b.n >= reg.param("min_n_recovery")
    if day.bases["hrv"] is not None:  # Plews band: rolling mean against the daily ln baseline
        day.hrv_status = bl.hrv_band_status(day.hrv_roll, day.bases["hrv"], reg.param("hrv_band_halfwidth_sd"))


def _sleep_and_recovery(ctx: _Context, day: _Day, profile: Profile, reg: Registry) -> None:
    ds, nights = day.ds, ctx.inp.nights
    day.night_valid = ds in ctx.valid_nights
    day.night = slp.score_night(ds, nights, ctx.valid_nights, profile.sleep_need_h, reg)
    day.debt = slp.trailing_debt(ds, nights, profile.sleep_need_h, reg)
    day.n_rec = min((day.bases[m].n if day.bases[m] else 0) for m in ("hrv", "rhr"))
    if day.n_rec < reg.param("min_n_recovery") or day.z["rhr"] is None:
        return
    sleep_last = day.night["value"] if day.night else None
    if day.hrv_trend_ready:
        day.rec = recovery.recovery_score(day.hrv_roll_z, day.z["hrv"], day.z["rhr"], sleep_last, day.debt, reg)
    elif day.z["hrv"] is not None:
        # 7-day trend not established yet: last night's HRV is the HRV term, shrunk so one night can't swing it
        night_only = day.z["hrv"] * reg.param("hrv_night_only_shrink")
        day.rec = recovery.recovery_score(night_only, None, day.z["rhr"], sleep_last, day.debt, reg)


def _strain(ctx: _Context, day: _Day, tracker: signals.StrainTracker, missing: int, reg: Registry) -> int:
    """Advance the strain tracker by one night; returns the updated count of consecutive missing-RHR nights."""
    ds, series = day.ds, ctx.series
    day.n_sig = day.bases["rhr"].n if day.bases["rhr"] else 0
    if day.n_sig < reg.param("min_n_signals"):
        tracker.idle()
        return 0
    if day.z["rhr"] is None:
        missing += 1
        if missing <= reg.param("strain_max_skipped_nights"):
            tracker.skip()
        else:
            tracker.idle()
        return missing
    day.signals_on = True
    day.flags = signals.strain_flags(
        rhr=ctx.inp.daily.get("resting_hr", {}).get(ds), rhr_b=day.bases["rhr"],
        resp=series["resp"].get(ds), resp_b=day.bases["resp"],
        temp=series["temp"].get(ds), temp_b=day.bases["temp"],
        google_temp_dev=ctx.inp.daily.get("sleep_temp_dev_c", {}).get(ds),
        hrv_status=day.hrv_status, hrv_night_z=day.z["hrv"],
        spo2=series["spo2"].get(ds), spo2_b=day.bases["spo2"], reg=reg)
    day.severity, day.conditions, _ = tracker.step(day.flags, day.z["rhr"])
    return 0


def _stress(ctx: _Context, d: date, reg: Registry) -> float | None:
    ds = d.isoformat()
    if ds not in ctx.sed_bpm:
        return None
    first = (d - timedelta(days=int(reg.param("stress_baseline_days")))).isoformat()
    prior = [k for k in ctx.sed_bpm if first <= k < ds and ctx.sed_bpm[k]]
    if len(prior) < reg.param("min_n_signals"):
        return None
    median = statistics.median(b for k in prior for b in ctx.sed_bpm[k])
    return signals.stress_share(ctx.sed_bpm[ds], median, reg)


def _metric_rows(ctx: _Context, day: _Day, reg: Registry) -> list[dict]:
    """Metric rows. While a baseline is still warming up (n < min_n_recovery) its value and baseline are stored
    but z and band_status are left empty."""
    ds, q = day.ds, day.quality
    nq = "good" if day.night_valid else "invalid"  # overnight rows: quality is the night's validity
    min_rec = reg.param("min_n_recovery")
    rows = []
    for m in ("hrv", "rhr", "resp", "temp", "spo2", "sleep_min"):
        if ds not in ctx.series[m]:
            continue
        b = day.bases[m]
        value = math.exp(ctx.series[m][ds]) if m == "hrv" else ctx.series[m][ds]
        center = None if b is None else (math.exp(b.center) if m == "hrv" else b.center)
        n = b.n if b else 0
        rows.append({"local_date": ds, "metric": m, "value": _r(value), "baseline": _r(center),
                     "scale": _r(b.scale if b else None), "z": _r(day.z[m]) if n >= min_rec else None,
                     "band_status": None, "confidence": b.confidence if b else 0.0, "quality": nq, "n_baseline": n})
    if day.hrv_roll is not None:
        # baseline / scale / z / n describe the baseline of past 7-day means (what recovery scores against), so
        # they are consistent with each other; band_status is the Plews band, which compares the 7-day mean with
        # the *daily* ln baseline (see the hrv row).
        rb, hb = day.hrv_roll_b, day.bases["hrv"]
        n = rb.n if rb else 0
        warm = n >= min_rec and hb is not None and hb.n >= min_rec
        rows.append({"local_date": ds, "metric": "hrv_7d", "value": _r(math.exp(day.hrv_roll)),
                     "baseline": _r(math.exp(rb.center)) if rb else None, "scale": _r(rb.scale) if rb else None,
                     "z": _r(day.hrv_roll_z) if warm else None, "band_status": day.hrv_status if warm else None,
                     "confidence": rb.confidence if rb else 0.0, "quality": nq, "n_baseline": n})
    if day.debt is not None:
        # a multi-night estimate: not tied to tonight's validity
        rows.append({"local_date": ds, "metric": "sleep_debt_h", "value": _r(day.debt, 2), "quality": "good"})
    if day.signals_on:
        rows.append({"local_date": ds, "metric": "strain_level",
                     "value": {None: 0, "watch": 1, "alert": 2}[day.severity], "quality": nq})
    if day.stress is not None:
        rows.append({"local_date": ds, "metric": "stress_share", "value": _r(day.stress), "quality": q})
    return rows


def _sleep_reason(ctx: _Context, day: _Day, reg: Registry) -> str:
    min_h = reg.param("night_min_period_min") / 60
    if day.night_valid:
        return UNSCORABLE_NIGHT
    n = ctx.inp.nights.get(day.ds)
    if n is not None and (n.sleep_type or "").lower() == "stages":  # staged but under the minimum length
        asleep_min = n.min_asleep if n.min_asleep is not None else (n.min_in_period or 0)
        return f"short night: {asleep_min / 60:.1f} h asleep, under the {min_h:g} h minimum for a full score"
    return f"no valid main sleep (staged, {min_h:g} h or more) waking on this date"


def _score_rows(ctx: _Context, day: _Day, reg: Registry) -> list[dict]:
    ds = day.ds
    rows = []
    if day.night:
        rows.append({"local_date": ds, "score": "sleep", "value": _r(day.night["value"], 1), "band": None,
                     "confidence": _r(day.night["coverage"], 2), "status": "ok",
                     "components": {k: _round_tree(v) for k, v in day.night.items() if k not in ("value", "coverage")},
                     "evidence_ids": EVIDENCE["sleep"]})
    else:
        rows.append({"local_date": ds, "score": "sleep", "value": None, "band": None, "confidence": 0.0,
                     "status": "insufficient_data",
                     "components": {"reason": _sleep_reason(ctx, day, reg)},
                     "evidence_ids": EVIDENCE["sleep"]})
    if day.rec:
        value, comps, w = day.rec
        rows.append({
            "local_date": ds, "score": "recovery", "value": _r(value, 1), "band": recovery.band(value, reg),
            "confidence": _rec_confidence(day), "status": "ok",
            "components": {
                "hrv": {"score": _r(comps["hrv"], 1),
                        "roll_z": _r(day.hrv_roll_z, 2) if day.hrv_trend_ready else None,
                        "night_z": _r(day.z["hrv"], 2),  # unshrunk
                        "shrunk": not day.hrv_trend_ready,
                        "band_status": day.hrv_status},
                "rhr": {"score": _r(comps["rhr"], 1), "z": _r(day.z["rhr"], 2)},
                "sleep": {"score": _r(comps["sleep"], 1), "debt_h": _r(day.debt, 2)},
                "weights": {k: _r(v, 3) for k, v in w.items()}},
            "evidence_ids": EVIDENCE["recovery"]})
    else:
        min_rec = int(reg.param("min_n_recovery"))
        reason = (f"building baseline: {day.n_rec}/{min_rec} valid days" if day.n_rec < min_rec
                  else "today's HRV or resting HR is missing")
        rows.append({"local_date": ds, "score": "recovery", "value": None, "band": None, "confidence": 0.0,
                     "status": "insufficient_data", "components": {"reason": reason},
                     "evidence_ids": EVIDENCE["recovery"]})
    return rows


def _strain_detail(ctx: _Context, day: _Day) -> str:
    parts = []
    for f in day.flags:
        m = FLAG_METRIC[f]
        b = day.bases[m]
        now = ctx.series[m].get(day.ds)
        if now is not None and b is not None:
            now_v, base_v = (math.exp(now), math.exp(b.center)) if m == "hrv" else (now, b.center)
            parts.append(f"{f} ({FLAG_TEXT[f]} {now_v:.1f} vs your baseline {base_v:.1f})")
        else:
            parts.append(f)
    return (f"Signals: {', '.join(parts) or 'none today'}. Conditions: {', '.join(day.conditions)}. "
            f"{STRAIN_CAUSES}")


def _rec_confidence(day: _Day) -> float:
    return min(day.bases["hrv"].confidence, day.bases["rhr"].confidence)


def _insights(ctx: _Context, day: _Day, profile: Profile, reg: Registry) -> list[dict]:
    ds = day.ds
    rows = []

    def add(kind: str, sev: str, title: str, detail: str, evidence: list[str]) -> None:
        rows.append({"local_date": ds, "kind": kind, "severity": sev, "title": title, "detail": detail,
                     "evidence_ids": evidence})

    for _rail, title, detail in safety.evaluate(ds, ctx.inp.daily.get("resting_hr", {}),
                                                ctx.inp.daily.get("spo2_avg_pct", {}), ctx.inp.alerts,
                                                day.alert_run, reg):
        add("safety", "alert", title, detail, EVIDENCE["safety"])
    if day.severity:
        add("strain", day.severity, "Your body is showing strain signals", _strain_detail(ctx, day),
            EVIDENCE["strain"])
    confident = day.rec is not None and _rec_confidence(day) >= reg.param("finding_min_confidence")
    if (confident and day.hrv_trend_ready and day.hrv_below_run >= reg.param("hrv_below_days")
            and day.hrv_roll_z <= reg.param("hrv_below_z")):  # sustained and clear, not a one-night dip
        add("recovery", "watch", "HRV 7-day average is below your normal band",
            "A sustained HRV drop below your usual range often reflects accumulated fatigue, illness or stress.",
            EVIDENCE["hrv_band"])
    if confident and recovery.band(day.rec[0], reg) == "red":
        add("recovery", "watch", "Recovery is low today",
            "Consider an easy day: light movement, early night.", EVIDENCE["recovery"])
    if day.debt is not None and day.debt >= reg.param("sleep_debt_watch_h"):
        add("sleep", "watch", f"Sleep debt of {day.debt:.1f} h this week",
            f"Estimated from your recent nights against your {profile.sleep_need_h:g} h nightly need.",
            EVIDENCE["sleep_debt"])
    min_rec, min_sig = int(reg.param("min_n_recovery")), int(reg.param("min_n_signals"))
    if day.n_rec < min_rec or day.n_sig < min_sig:
        add("data", "info", "Building your baselines",
            f"{day.n_rec}/{min_rec} valid days for recovery, {day.n_sig}/{min_sig} for strain signals.",
            EVIDENCE["baselines"])
    return rows


PLUGIN_BUDGET_S = 5.0  # total time one plugin file may spend across all days of one analysis run


def _plugin_rows(store: Store, plugins, metric_rows: list[dict], since: date, today: date
                 ) -> tuple[list[dict], list[dict], list[str]]:
    """Plugin metric and insight rows for [since, today]; never raises (a plugin must not cost the analysis)."""
    try:
        raw: dict[str, dict[str, float]] = {}
        for d, metric, value in store.daily_values_between(since.isoformat(), today.isoformat()):
            if value is not None:
                raw.setdefault(d, {})[metric] = value
        by_day: dict[str, dict[str, float]] = {}
        for r in metric_rows:
            if r.get("value") is not None:
                by_day.setdefault(r["local_date"], {})[r["metric"]] = r["value"]
        metrics, insights, problems = [], [], []
        spent: dict[str, float] = {}  # seconds each plugin file has used in this run
        for d in _dates(since, today):
            ds = d.isoformat()
            m_rows, i_rows, probs = run_daily_plugins(plugins, ds, DayContext(by_day.get(ds, {}), raw.get(ds, {})),
                                                      spent, PLUGIN_BUDGET_S)
            metrics += m_rows
            insights += i_rows
            problems += probs
        problems += [f"plugin {file} used {used:.1f} s, over its {PLUGIN_BUDGET_S:g} s budget for one analysis run; "
                     "its remaining days were skipped" for file, used in spent.items() if used > PLUGIN_BUDGET_S]
        return metrics, insights, list(dict.fromkeys(problems))  # one line per distinct problem, not one per day
    except Exception as exc:  # noqa: BLE001
        log.exception("analysis: plugins failed; writing the built-in rows only")
        return [], [], [f"plugins could not run: {exc!r}"]


def analyze(store: Store, profile: Profile, reg: Registry, since: date, today: date,
            plugins: LoadedPlugins | None = None) -> AnalysisSummary:
    """Analyse every day in [since, today] and replace the stored analysis rows dated on/after `since`.

    Daily metrics and nights (cheap) load from two baseline windows before `since`, which fills the baselines,
    the baseline of 7-day HRV means and the strain tracker's warm-up. Minute-level data (expensive) loads only
    as far back as the stress baseline needs. `since` is clipped up to the first date with any data, so no rows
    are written for days before tracking began; with no data at all nothing is written."""
    first = store.first_data_date(DAILY_SOURCES)  # overnight sources and nights, not e.g. HR-zone settings
    if first is None:
        log.info("analysis: no data yet")
        return AnalysisSummary(days=0, metric_rows=0, score_rows=0, insights=0)
    since = max(since, date.fromisoformat(first))
    load_start = since - timedelta(days=2 * int(reg.param("baseline_window_days")))
    minutes_start = since - timedelta(days=int(reg.param("stress_baseline_days")) + 1)
    ctx = _prepare(store, reg, load_start, minutes_start, today)
    tracker = signals.StrainTracker(reg)
    missing = below_run = 0
    metric_rows: list[dict] = []
    score_rows: list[dict] = []
    insight_rows: list[dict] = []
    sri: dict[str, float] = {}
    rhr_baseline: dict[str, float] = {}
    for d in _dates(load_start, today):
        ds = d.isoformat()
        day = _Day(ds=ds, quality=ctx.qual.get(ds, "invalid"), bases={}, z={})
        _baselines_and_hrv(ctx, day, reg)
        below_run = below_run + 1 if day.hrv_status == "below" else 0  # a day without a status breaks the run
        day.hrv_below_run = below_run
        missing = _strain(ctx, day, tracker, missing, reg)  # walked from load_start to warm the tracker up
        day.alert_run = tracker.alert_run
        if day.bases.get("rhr") is not None:
            rhr_baseline[ds] = day.bases["rhr"].center
        if d < since:
            if d >= minutes_start:  # Phase B's 30-day SRI windows reach back before `since`
                night = slp.score_night(ds, ctx.inp.nights, ctx.valid_nights, profile.sleep_need_h, reg)
                if night and night.get("sri") is not None:
                    sri[ds] = night["sri"]
            continue
        _sleep_and_recovery(ctx, day, profile, reg)
        if day.night and day.night.get("sri") is not None:
            sri[ds] = day.night["sri"]
        day.stress = _stress(ctx, d, reg)
        metric_rows += _metric_rows(ctx, day, reg)
        score_rows += _score_rows(ctx, day, reg)
        insight_rows += _insights(ctx, day, profile, reg)

    phase_b_error = None
    try:  # a Phase B bug must never cost the Phase A rows
        phase_a = fitness.PhaseAInputs(
            rhr=ctx.series["rhr"], rhr_baseline=rhr_baseline, sleep_min=ctx.series["sleep_min"], sri=sri,
            nights=ctx.inp.nights, age=ctx.inp.age, first_data=first,
            wear_days={d for d, q in ctx.qual.items() if q == "good"}, history_from=minutes_start.isoformat(),
            vo2={d: v for d, _m, v in store.daily_metric_values(["vo2_max"], load_start.isoformat(),
                                                                 today.isoformat()) if v is not None})
        fit = fitness.compute(store, profile, reg, since, today, phase_a)
    except Exception as exc:
        log.exception("analysis: Phase B failed; writing Phase A rows only")
        phase_b_error = f"phase B failed: {exc!r}"
        fit = fitness.FitnessRows()
    metric_rows += fit.metrics
    score_rows += fit.scores
    insight_rows += fit.insights
    plugin_problems: list[str] = []
    if plugins is not None and (plugins.metrics or plugins.findings):
        p_metrics, p_insights, plugin_problems = _plugin_rows(store, plugins, metric_rows, since, today)
        metric_rows += p_metrics
        insight_rows += p_insights
    store.replace_analysis(since.isoformat(), metric_rows, score_rows, insight_rows, fit.runs)
    summary = AnalysisSummary(days=len(_dates(since, today)), metric_rows=len(metric_rows),
                              score_rows=len(score_rows), insights=len(insight_rows), runs=len(fit.runs),
                              error=phase_b_error, plugin_problems=plugin_problems)
    log.info("analysis %s..%s: %s", since, today, summary)
    return summary
