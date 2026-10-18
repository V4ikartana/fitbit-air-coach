"""MCP server: the coach tools and prompts over stdio (Claude Desktop / Claude Code start `fitbit-air mcp`)."""
from __future__ import annotations

import functools
import importlib.metadata
import inspect
import json
import logging
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from typing import Annotated, Any, Callable, Sequence

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Strict

from ..config import get_paths
from ..plugins import LoadedPlugins, load_plugins
from ..store import Store
from ..timeutil import UTC
from ..userconfig import read_profile, registry_for
from . import actions, prompts, tools
from .persona import build_instructions
from .prompt_files import PromptFile, builtin_arg_problems, load_prompt_files, render_prompt
from .tools import Ctx, PluginToolCtx, ToolError

log = logging.getLogger(__name__)
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)  # clients may call these without asking
WRITES = ToolAnnotations(readOnlyHint=False)
StrictDays = Annotated[int, Strict()] | None  # True, "3" and 3.0 are rejected, not coerced


def _call(fn: Callable[..., dict], startup_problems: Sequence[str] = (), plugins: LoadedPlugins | None = None,
          **kwargs) -> dict:
    """Open a fresh Store per call (the SDK runs sync tools on worker threads) and never create a missing database.
    startup_problems (found when the server started) are reported next to the config problems found now.
    plugins (loaded when the server started) are what get_evidence reads plugin notes from."""
    paths = get_paths()
    if not paths.db.exists():
        return {"error": f"database not found at {paths.db}", "hint": "run `uv run fitbit-air sync` first"}
    store = Store(paths.db)
    try:
        reg, problems = registry_for(paths)
        ctx = Ctx(store=store, reg=reg, now=datetime.now(UTC), today=date.today(), paths=paths,
                  config_problems=[str(p) for p in problems] + ["(at server start) " + p for p in startup_problems],
                  plugins=plugins)
        return fn(ctx, **kwargs)
    finally:
        store.close()


def _user_prompt_fn(p: PromptFile) -> Callable[..., str]:
    """A prompt function with a real signature, so the SDK lists the arguments the file's `args:` line declares."""
    def fn(**kwargs) -> str:
        try:
            return render_prompt(p, date.today(), kwargs)
        except ValueError as e:  # e.g. a week_ending argument that is not YYYY-MM-DD
            return (f"The prompt {p.name!r} could not be filled in ({e}). Don't guess: ask the user for a valid "
                    "value (dates as YYYY-MM-DD), then run the prompt again.")
    fn.__name__ = p.name
    fn.__doc__ = f"Your prompt '{p.name}' (from config/coach/prompts/{p.name}.md)."
    # pydantic's validate_call reads __annotations__ while the SDK reads __signature__, so set both
    fn.__annotations__ = {**{a: str | None for a in p.args}, "return": str}
    fn.__signature__ = inspect.Signature(
        [inspect.Parameter(a, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=str | None) for a in p.args],
        return_annotation=str)
    return fn


def _plugin_tool_fn(name: str, fn: Callable[..., Any], call: Callable[..., dict]) -> Callable[..., dict]:
    """The MCP function for a plugin's coach tool: its parameters after ctx become the tool's arguments, and it gets
    the same never-raise wrapper (errors become {"error", "hint"}) as the built-in tools."""
    def run(ctx: Ctx, **kwargs) -> dict:
        try:
            out = fn(PluginToolCtx(ctx.today, ctx.now, ctx.paths.db), **kwargs)  # the real Ctx stays here
        except SystemExit:  # not an Exception, so coach_tool would let it end the server
            raise ToolError("the plugin tool called sys.exit()") from None
        if not isinstance(out, dict):
            raise ToolError(f"the plugin tool returned {type(out).__name__}, not a dict")
        try:
            json.dumps(out, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ToolError(f"the plugin tool's result is not plain JSON ({exc})") from None
        return out
    run.__name__ = f"plugin_{name}"
    guarded = tools.coach_tool(run)

    def wrapper(**kwargs) -> dict:
        return call(lambda ctx: guarded(ctx, **kwargs))  # closure: a plugin argument can be named anything

    P = inspect.Parameter
    params = []
    for p in list(inspect.signature(fn).parameters.values())[1:]:  # [0] is ctx
        if p.kind in (P.VAR_POSITIONAL, P.VAR_KEYWORD):
            continue
        ann = p.annotation
        if ann is P.empty:
            ann = type(p.default) if type(p.default) in (str, int, float, bool) else Any
        params.append(p.replace(kind=P.KEYWORD_ONLY, annotation=ann))
    wrapper.__name__ = f"plugin_{name}"
    wrapper.__annotations__ = {**{p.name: p.annotation for p in params}, "return": dict}
    wrapper.__signature__ = inspect.Signature(params, return_annotation=dict)
    return wrapper


def build_server() -> MCPServer:
    """The coach server. Tools are plain `def`s: MCPServer runs sync tools on a worker thread, so a long
    refresh_data or query_sql never blocks the event loop."""
    paths = get_paths()
    startup_problems: list[str] = []  # shown in data_status["config_problems"] so the coach can tell the user
    # The same non-creating read as `config check`; a bad profile must not stop the coach (it uses the defaults).
    profile, profile_problems = read_profile(paths)
    for problem in profile_problems:
        startup_problems.append(f"profile problem: {problem}")
        log.warning(startup_problems[-1])
    user_prompts, prompt_problems = load_prompt_files(paths)
    for problem in prompt_problems:
        startup_problems.append(f"custom prompt skipped: {problem}")
        log.warning(startup_problems[-1])
    for problem in builtin_arg_problems(paths, user_prompts):
        startup_problems.append(f"custom prompt: {problem}")
        log.warning(startup_problems[-1])
    plugins = LoadedPlugins()
    try:  # load_plugins does not raise, but a plugin problem must never stop the coach starting
        plugins = load_plugins(paths, profile.plugins_enabled)
    except Exception as e:  # noqa: BLE001
        startup_problems.append(f"plugins not loaded ({e!r})")
        log.exception("plugins not loaded")
    for problem in plugins.problems:
        startup_problems.append(f"plugin problem: {problem}")
        log.warning(startup_problems[-1])
    srv = MCPServer(name="fitbit-coach", version=importlib.metadata.version("fitbit-air"),
                    instructions=build_instructions(paths, profile, startup_problems))
    call = functools.partial(_call, startup_problems=startup_problems, plugins=plugins)

    @srv.tool(annotations=READ_ONLY)
    def get_overview(date: str | None = None) -> dict:
        """One day's scores (with confidence), findings, safety alerts first, and key metrics. Default today."""
        return call(tools.get_overview, date=date)

    @srv.tool(annotations=READ_ONLY)
    def get_trends(metrics: list[str], start: str | None = None, end: str | None = None,
                   days: StrictDays = None) -> dict:
        """Daily series with baseline/band. Metrics: rhr, hrv_7d, sleep_debt_h, steps_7d, mvpa_week, sedentary_h,
        weight_trend_kg, bmi (unknown names return the full list). Scores: score:recovery, score:sleep,
        score:body_age, score:race. Default last 30 days; max 366.
        Or pass days=N for the last N days ending at end (default today); not with start."""
        return call(tools.get_trends, metrics=metrics, start=start, end=end, days=days)

    @srv.tool(annotations=READ_ONLY)
    def get_sleep(start: str | None = None, end: str | None = None, days: StrictDays = None) -> dict:
        """Nights (bed/wake local, stages, efficiency, naps), sleep scores with subscores, sleep debt, and the
        Sleep Regularity Index (SRI, -100..100, 100 = identical timing: how alike sleep/wake timing is from day
        to day, from the sleep score). Default 7 days.
        Or pass days=N for the last N days ending at end (default today); not with start."""
        return call(tools.get_sleep, start=start, end=end, days=days)

    @srv.tool(annotations=READ_ONLY)
    def get_activity(start: str | None = None, end: str | None = None, days: StrictDays = None) -> dict:
        """Daily steps, brisk minutes, Google zone minutes, sedentary hours, strength, weight; workouts and
        weigh-ins. Default 7 days.
        Or pass days=N for the last N days ending at end (default today); not with start."""
        return call(tools.get_activity, start=start, end=end, days=days)

    @srv.tool(annotations=READ_ONLY)
    def get_running(start: str | None = None, end: str | None = None, days: StrictDays = None) -> dict:
        """Runs (distance, pace, HR zones, spike flags) and the latest race plan status (if a race
        is configured). Default 30 days.
        Or pass days=N for the last N days ending at end (default today); not with start."""
        return call(tools.get_running, start=start, end=end, days=days)

    @srv.tool(annotations=READ_ONLY)
    def get_body_age(date: str | None = None) -> dict:
        """Latest body age on or before the date: display with range, components, levers, missing inputs, trend."""
        return call(tools.get_body_age, date=date)

    @srv.tool(annotations=READ_ONLY)
    def get_evidence(ids: list[str]) -> dict:
        """Citation, DOI and finding for evidence ids attached to scores and insights."""
        return call(tools.get_evidence, ids=ids)

    @srv.tool(annotations=READ_ONLY)
    def data_status() -> dict:
        """Last sync and analysis, row counts, wear days, and how old the Google login is. Call this first."""
        return call(tools.data_status)

    @srv.tool(annotations=READ_ONLY)
    def describe_schema() -> dict:
        """Tables and views with columns and purposes, for query_sql."""
        return call(tools.describe_schema)

    @srv.tool(annotations=READ_ONLY)
    def query_sql(sql: str) -> dict:
        """Run ONE read-only SELECT on the local database (max 500 rows, 5 s; text cells are
        truncated at 2000 characters). Dates are 'YYYY-MM-DD' strings."""
        return call(tools.query_sql, sql=sql)

    @srv.tool(annotations=WRITES)
    def log_journal(date: str | None = None, feeling: int | None = None, energy: int | None = None,
                    soreness: int | None = None, session_rpe: int | None = None, illness: bool | None = None,
                    alcohol_units: float | None = None, notes: str | None = None) -> dict:
        """Log how the user feels: feeling/energy 1-5, soreness/session_rpe 0-10, illness, alcohol units 0-50,
        notes. Local only."""
        return call(actions.log_journal, date=date, feeling=feeling, energy=energy, soreness=soreness,
                     session_rpe=session_rpe, illness=illness, alcohol_units=alcohol_units, notes=notes)

    @srv.tool(annotations=READ_ONLY)
    def get_journal(start: str | None = None, end: str | None = None, days: StrictDays = None) -> dict:
        """Journal entries in a date range (default 7 days).
        Or pass days=N for the last N days ending at end (default today); not with start."""
        return call(actions.get_journal, start=start, end=end, days=days)

    @srv.tool(annotations=WRITES)
    def save_coach_note(kind: str, text: str, review_on: str | None = None) -> dict:
        """Remember an agreed plan, weekly target or observation (kind: plan|target|observation). Local only."""
        return call(actions.save_coach_note, kind=kind, text=text, review_on=review_on)

    @srv.tool(annotations=READ_ONLY)
    def get_coach_notes(status: str | None = "active", due_by: str | None = None) -> dict:
        """Coach notes, overdue first. status: active|done|dropped, or None for every note. due_by: YYYY-MM-DD;
        only notes with review_on on or before it."""
        return call(actions.get_coach_notes, status=status, due_by=due_by)

    @srv.tool(annotations=WRITES)
    def update_coach_note(id: int, status: str | None = None, text: str | None = None,
                          review_on: str | None = None) -> dict:
        """Mark a note done/dropped or change its text or review date."""
        return call(actions.update_coach_note, id=id, status=status, text=text, review_on=review_on)

    @srv.tool(annotations=WRITES)
    def set_profile(key: str, value: str | int | float | list[str]) -> dict:
        """Change a profile value and re-run the analysis. Keys: race_date (YYYY-MM-DD), race_distance_km,
        sleep_need_h, height_m, hrmax_override (a number or a numeric string), sex, units (metric or imperial),
        timezone (an IANA name such as Europe/London), goals (a list of strings).
        The value "none" clears race_date, race_distance_km, height_m, hrmax_override and timezone."""
        return call(actions.set_profile, key=key, value=value)

    @srv.tool(annotations=WRITES)
    def refresh_data() -> dict:
        """Sync new data from Google now (then analysis runs). Takes up to a few minutes."""
        return call(actions.refresh_data)

    def builtin_prompt(fn: Callable[..., str]) -> Callable[..., str]:
        """Register a built-in prompt unless the user has a prompt file of the same name."""
        if fn.__name__ not in user_prompts:  # a missing-argument override is reported by builtin_arg_problems
            srv.prompt()(fn)
        return fn

    @builtin_prompt
    def morning_checkin() -> str:
        """Morning check-in: safety, recovery and sleep, one suggestion, notes due."""
        return prompts.morning_checkin()

    @builtin_prompt
    def weekly_review(week_ending: str | None = None) -> str:
        """Weekly review: last 7 vs previous 28 days, plan status, up to three targets for next week."""
        try:
            return prompts.weekly_review(week_ending)
        except ValueError:
            return (f"The weekly review was asked for week ending {week_ending!r}, which is not a valid date. "
                    "Don't guess: ask the user for the week's last day as YYYY-MM-DD, then run the weekly review.")

    @builtin_prompt
    def plan_next_run() -> str:
        """Plan the next run within the 10%-a-week build and today's recovery."""
        return prompts.PLAN_NEXT_RUN

    @builtin_prompt
    def explain_score(score: str, date: str | None = None) -> str:
        """Explain what drove a score (recovery, sleep, body_age, race) and the lever that moves it most."""
        return prompts.explain_score(score, date)

    for p in user_prompts.values():
        srv.prompt(name=p.name)(_user_prompt_fn(p))

    for name, t in plugins.tools.items():  # read-only: a plugin tool can look at data but has no write annotation
        try:
            srv.tool(name=f"plugin_{name}", description=f"{t['description']} (your plugin)",
                     annotations=READ_ONLY)(_plugin_tool_fn(name, t["fn"], call))
        except Exception as e:  # noqa: BLE001 - e.g. a parameter the tool schema cannot describe
            startup_problems.append(f"plugin tool {name} ({t['file']}) not registered: {e!r}")
            log.warning(startup_problems[-1])

    return srv


def _setup_logging() -> None:
    """Log to data/logs/coach.log only: stdout is the MCP stream, so no console handler (unlike cli._setup_logging)."""
    root = logging.getLogger()
    if any(getattr(h, "_fitbit_air_coach", False) for h in root.handlers):
        return
    logs_dir = get_paths().logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(logs_dir / "coach.log", maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.setLevel(logging.INFO)
    handler._fitbit_air_coach = True
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def main() -> None:
    _setup_logging()
    build_server().run("stdio")
