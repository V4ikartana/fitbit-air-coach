"""Coach read tools: plain functions over the Store returning JSON-able dicts (MCP wiring lives in server.py)."""
from __future__ import annotations

import dataclasses
import functools
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from ..analysis.evidence import Registry
from ..config import Paths
from ..store import Store
from ..timeutil import parse_ts
from .journal import ValidationError
from .sql import SqlError, run_query

log = logging.getLogger(__name__)

MAX_RANGE_DAYS = 366
STALE_HOURS = 6
ACTIVITY_METRICS = ("steps_7d", "mvpa_week", "azm_week", "sedentary_h", "strength_min_week", "strength_days_week",
                    "weight_trend_kg", "bmi")
OVERVIEW_METRICS = ("rhr", "hrv_7d", "sleep_debt_h", *ACTIVITY_METRICS)  # SRI lives in the sleep score
AUTH_NOTE = ("In OAuth Testing mode Google refresh tokens expire 7 days after `auth`; publish the app to production "
             "(Google Cloud console → OAuth consent screen) and run `uv run fitbit-air auth` once more.")
PURPOSES = {
    "metric_daily": "analysis metrics per day with personal baseline, z-score, band and confidence",
    "scores_daily": "daily scores (recovery, sleep, body_age, race) with components_json and evidence_ids",
    "insights": "findings per day (kind, severity, title, detail, evidence_ids); kind 'safety' = safety rails",
    "runs": "one row per run: distance, pace, HR zones z0-z5, spike flags",
    "daily_metrics": "Google daily values (resting_hr, hrv, spo2, vo2_max ...) by metric name",
    "daily_wide": "view: one row per day with the main overnight metrics as columns",
    "sleep_sessions": "sleep sessions by wake date (is_main, is_nap, stage minutes, efficiency)",
    "activity_intervals": "minute-level activity (steps, activity levels, active zone minutes) by metric",
    "exercise_sessions": "logged workouts (type, duration, avg_hr, steps, distance)",
    "samples": "point samples such as weight_kg and height_m",
    "heart_rate": "heart-rate samples",
    "journal": "user's journal: feeling, energy, soreness, session_rpe, illness, alcohol_units, notes",
    "coach_notes": "coach plans/targets/observations with review_on and status",
    "sync_runs": "sync history (started_at, finished_at, error_summary)",
    "analysis_runs": "analysis history (started_at, finished_at, error, summary)",
}


class ToolError(Exception):
    """An expected failure, reported to Claude as {"error", "hint"}."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


@dataclass
class Ctx:
    store: Store
    reg: Registry
    now: datetime  # aware UTC
    today: date  # local date
    paths: Paths
    config_problems: list[str] = field(default_factory=list)  # user config files that fell back to defaults
    plugins: object | None = None  # loaded plugins (None when disabled or not loaded)


@dataclass(frozen=True)
class PluginToolCtx:
    """What a plugin's coach tool receives instead of the real Ctx: the date and a read-only SQL query. It has no
    Store and no paths, so an honest plugin cannot write by accident. (A plugin is still the user's own code
    running with their permissions; this guards mistakes, it is not a sandbox.)"""
    today: date
    now: datetime
    _db: Path

    def query(self, sql: str) -> dict:
        """One read-only SELECT (same limits as the query_sql tool). Returns {columns, rows, truncated}, or
        {"error", "hint"} when the query is refused or fails."""
        try:
            return run_query(self._db, sql)
        except SqlError as exc:
            return {"error": str(exc), "hint": "only one SELECT is allowed; describe_schema lists the tables"}


def freshness(store: Store, now: datetime) -> dict:
    """When the last sync finished, how long ago, and whether that is past the stale threshold."""
    run = store.last_finished_run()
    if run is None:
        return {"last_sync_finished_utc": None, "hours_since_sync": None, "stale": True}
    hours = max(0.0, (now - parse_ts(run["finished_at"])).total_seconds() / 3600)  # clock skew
    return {"last_sync_finished_utc": run["finished_at"], "hours_since_sync": round(hours, 1),
            "stale": hours > STALE_HOURS}


def coach_tool(fn: Callable[..., dict]) -> Callable[..., dict]:
    """Never raise to the client: expected failures → {"error", "hint"}, anything else → an internal error dict.
    Every result carries data freshness."""
    @functools.wraps(fn)
    def wrapper(ctx: Ctx, *args, **kwargs) -> dict:
        try:
            out = fn(ctx, *args, **kwargs)
        except ToolError as exc:
            out = {"error": str(exc), "hint": exc.hint}
        except ValidationError as exc:  # bad journal/note input; other ValueErrors are bugs and get logged below
            out = {"error": str(exc), "hint": None}
        except Exception as exc:  # noqa: BLE001 - a tool must never crash the conversation
            log.exception("coach tool %s failed", fn.__name__)
            out = {"error": f"internal error: {exc!r}", "hint": "see data/logs/coach.log; try again or use query_sql"}
        try:
            out["freshness"] = freshness(ctx.store, ctx.now)
        except Exception:  # noqa: BLE001
            log.exception("freshness failed")
        return out
    return wrapper


def parse_day(value: str | None, today: date, name: str = "date") -> str:
    """Validate a YYYY-MM-DD string (None means today) and return it normalised."""
    if value is None:
        return today.isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be YYYY-MM-DD, got {value!r}") from None


def date_range(start: str | None, end: str | None, today: date, default_days: int = 7,
               days: int | None = None) -> tuple[str, str]:
    """Validated inclusive (start, end). end defaults to today; start comes from `start`, else the `days` days ending
    at end (so a saved query never goes stale), else `default_days` back. Capped in length."""
    if days is not None and start is not None:
        raise ToolError("give start or days, not both", "days = the last N days ending at end (default today)")
    if days is not None and (isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_RANGE_DAYS):
        raise ToolError(f"days must be a whole number from 1 to {MAX_RANGE_DAYS}, got {days!r}")
    e = date.fromisoformat(parse_day(end, today, "end"))
    if start is not None:
        s = date.fromisoformat(parse_day(start, today, "start"))
    else:
        s = e - timedelta(days=(days or default_days) - 1)
    if s > e:
        raise ToolError(f"start {s} is after end {e}")
    n = (e - s).days + 1
    if n > MAX_RANGE_DAYS:
        raise ToolError(f"range is {n} days; the maximum is {MAX_RANGE_DAYS}", "ask for a shorter range")
    return s.isoformat(), e.isoformat()


def _local(ts: str | None) -> str | None:
    return None if ts is None else parse_ts(ts).astimezone().strftime("%Y-%m-%dT%H:%M")


def _components(s: dict) -> dict:
    return s["components"] if isinstance(s.get("components"), dict) else {}


def _score_summary(s: dict) -> dict:
    comps = _components(s)
    return {"score": s["score"], "value": s["value"], "band": s["band"], "status": s["status"],
            "confidence": s["confidence"], "reason": comps.get("reason"), "display": comps.get("display"),
            "components": comps, "evidence_ids": s["evidence_ids"]}


@coach_tool
def get_overview(ctx: Ctx, date: str | None = None) -> dict:
    day = parse_day(date, ctx.today)
    a = ctx.store.analysis_for_day(day)
    if not (a["scores"] or a["insights"] or a["metrics"]):
        first, last = ctx.store.first_analysis_date(), ctx.store.last_analysis_date()
        if first is None:
            hint = "no analysis yet: run refresh_data"
        elif day > last:
            hint = f"latest analysis is {last}; run refresh_data or ask for {last}"
        else:
            hint = f"analysis covers {first} to {last}"
        raise ToolError(f"no analysis for {day}", hint)
    insights = [{k: i[k] for k in ("kind", "severity", "title", "detail", "evidence_ids")} for i in a["insights"]]
    return {"date": day, "safety": [i for i in insights if i["kind"] == "safety"],
            "scores": [_score_summary(s) for s in a["scores"]], "insights": insights,
            "metrics": {m["metric"]: m["value"] for m in a["metrics"] if m["metric"] in OVERVIEW_METRICS}}


@coach_tool
def get_trends(ctx: Ctx, metrics: list[str], start: str | None = None, end: str | None = None,
               days: int | None = None) -> dict:
    metrics = [metrics] if isinstance(metrics, str) else metrics
    if not metrics:
        raise ToolError("metrics is empty", "e.g. ['rhr', 'steps_7d', 'score:recovery']")
    s, e = date_range(start, end, ctx.today, default_days=30, days=days)
    known_metrics, known_scores = set(ctx.store.analysis_metric_names()), set(ctx.store.score_names())
    series: dict[str, list[dict]] = {}
    unknown: list[str] = []
    for name in metrics:
        if name.startswith("score:") and name[6:] in known_scores:
            series[name] = [{"date": r["local_date"], "value": r["value"], "band": r["band"], "status": r["status"],
                             "confidence": r["confidence"]} for r in ctx.store.scores_between([name[6:]], s, e)]
        elif name in known_metrics:
            series[name] = [{"date": r["local_date"], "value": r["value"], "baseline": r["baseline"], "z": r["z"],
                             "band": r["band_status"], "confidence": r["confidence"], "quality": r["quality"]}
                            for r in ctx.store.metric_rows_between([name], s, e)]
        else:
            unknown.append(name)
    out = {"start": s, "end": e, "series": series, "unknown": unknown}
    if unknown:
        out["available"] = sorted(known_metrics) + [f"score:{n}" for n in sorted(known_scores)]
    return out


@coach_tool
def get_sleep(ctx: Ctx, start: str | None = None, end: str | None = None, days: int | None = None) -> dict:
    s, e = date_range(start, end, ctx.today, days=days)
    nights = [{"wake_date": n["local_date"], "bed_local": _local(n["start_utc"]), "wake_local": _local(n["end_utc"]),
               "asleep_min": n["min_asleep"], "awake_min": n["min_awake"], "efficiency": n["efficiency"],
               "light_min": n["min_light"], "deep_min": n["min_deep"], "rem_min": n["min_rem"],
               "main": bool(n["is_main"]), "nap": bool(n["is_nap"]), "type": n["sleep_type"]}
              for n in ctx.store.sleep_sessions_between(s, e)]
    scores = []
    for r in ctx.store.scores_between(["sleep"], s, e):
        comps = _components(r)
        scores.append({"date": r["local_date"], "value": r["value"], "status": r["status"],
                       "confidence": r["confidence"], "subscores": comps.get("subscores"), "sri": comps.get("sri"),
                       "reason": comps.get("reason")})
    debt = {r["local_date"]: r["value"] for r in ctx.store.metric_rows_between(["sleep_debt_h"], s, e)}
    sri = {x["date"]: x["sri"] for x in scores if x["sri"] is not None}  # SRI is a sleep-score component only
    return {"start": s, "end": e, "nights": nights, "scores": scores, "debt_h": debt, "sri": sri}


@coach_tool
def get_activity(ctx: Ctx, start: str | None = None, end: str | None = None, days: int | None = None) -> dict:
    s, e = date_range(start, end, ctx.today, days=days)
    by_day: dict[str, dict] = {}
    for r in ctx.store.metric_rows_between(ACTIVITY_METRICS, s, e):
        by_day.setdefault(r["local_date"], {"date": r["local_date"]})[r["metric"]] = r["value"]
    weigh_ins = [{"date": d, "ts_utc": ts, "kg": v} for d, ts, _m, v in ctx.store.sample_values(["weight_kg"], s, e)]
    sessions = [{"date": x["local_date"], "type": x["exercise_type"], "name": x["display_name"],
                 "duration_min": x["duration_min"], "avg_hr": x["avg_hr"], "steps": x["steps"],
                 "distance_m": x["distance_m"]} for x in ctx.store.exercise_sessions(s, e)]
    return {"start": s, "end": e, "days": [by_day[d] for d in sorted(by_day)], "weigh_ins": weigh_ins,
            "sessions": sessions,
            "notes": "mvpa_week = brisk walking (100+ steps/min, 130+ double) + workouts without steps; "
                     "azm_week = Google active zone minutes (heart-rate based, reads high at a high resting HR)"}


@coach_tool
def get_running(ctx: Ctx, start: str | None = None, end: str | None = None, days: int | None = None) -> dict:
    s, e = date_range(start, end, ctx.today, default_days=30, days=days)
    race = ctx.store.latest_score("race", e)
    plan = None if race is None else {
        "as_of": race["local_date"], "status": race["band"] or (race["components"] or {}).get("status"),
        "riegel_min": race["value"], "components": race["components"], "evidence_ids": race["evidence_ids"]}
    return {"start": s, "end": e, "runs": ctx.store.runs_between(s, e), "plan": plan,
            "note": "Only sessions logged as type Run in the Google Health app count as runs."}


@coach_tool
def get_body_age(ctx: Ctx, date: str | None = None) -> dict:
    day = parse_day(date, ctx.today)
    row = ctx.store.latest_score("body_age", day)
    if row is None:
        raise ToolError(f"no body age on or before {day}", "needs 7+ days of data, height and a recent weigh-in")
    return {"as_of": row["local_date"], "value": row["value"], "status": row["status"],
            "confidence": row["confidence"], "details": row["components"], "evidence_ids": row["evidence_ids"]}


def evidence_source(reg: Registry, source_id: str) -> dict:
    """One registry source as get_evidence reports it (JSON-able). The id must be in the registry."""
    src = dataclasses.asdict(reg.source(source_id))
    src["used_for"] = list(src["used_for"])
    return src


@coach_tool
def get_evidence(ctx: Ctx, ids: list[str]) -> dict:
    ids = [ids] if isinstance(ids, str) else ids
    sources, unknown = {}, []
    for i in ids:
        if i.startswith("override:"):
            o = ctx.reg.override(i.removeprefix("override:"))
            if o is None:
                unknown.append(i)
            else:
                sources[i] = {"kind": "override", "param": i.removeprefix("override:"), **o}
        elif i.startswith("plugin:"):
            note = ctx.plugins.evidence_note(i.removeprefix("plugin:")) if ctx.plugins else None
            if note is None:
                unknown.append(i)
            else:
                sources[i] = {"kind": "plugin", "name": i.removeprefix("plugin:"), "note": note}
        elif ctx.reg.has(i):
            sources[i] = evidence_source(ctx.reg, i)
        else:
            unknown.append(i)
    return {"sources": sources, "unknown": unknown}


@coach_tool
def data_status(ctx: Ctx) -> dict:
    auth_at = ctx.store.get_meta("auth_completed_at")
    days_since = None
    if auth_at is not None:
        try:
            days_since = round((ctx.now - parse_ts(auth_at)).total_seconds() / 86400, 1)
        except ValueError:
            log.warning("malformed auth_completed_at in meta: %r", auth_at)
    counts = ctx.store.table_counts()
    for t in ("metric_daily", "scores_daily", "insights", "runs", "journal", "coach_notes"):
        counts[t] = ctx.store.count_rows(t)
    start14 = (ctx.today - timedelta(days=13)).isoformat()
    return {"last_sync": ctx.store.last_run(), "last_finished_sync": ctx.store.last_finished_run(),
            "last_analysis": ctx.store.last_analysis(), "row_counts": counts,
            "first_analysis_date": ctx.store.first_analysis_date(),
            "wear_days_last_14": ctx.store.heart_rate_days(start14, ctx.today.isoformat()),
            "auth": {"completed_at": auth_at, "days_since": days_since, "note": AUTH_NOTE},
            "overrides": ctx.reg.overrides(), "config_problems": ctx.config_problems}


@coach_tool
def describe_schema(ctx: Ctx) -> dict:
    tables = [{**t, "purpose": PURPOSES.get(t["name"])} for t in ctx.store.schema_description()]
    return {"tables": tables, "hint": "query_sql runs one read-only SELECT; dates are local 'YYYY-MM-DD' strings"}


@coach_tool
def query_sql(ctx: Ctx, sql: str) -> dict:
    try:
        return run_query(ctx.paths.db, sql)
    except SqlError as exc:
        raise ToolError(str(exc), "call describe_schema for tables and columns; only one SELECT is allowed") from exc
