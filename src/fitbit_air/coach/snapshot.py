"""Daily dashboard snapshot: the dashboard's sections, built from the same coach functions, as one JSON file."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..dashboard_build import load_dashboard_config
from ..timeutil import iso_utc
from . import actions, tools
from .tools import Ctx

SCHEMA = 1
# The snapshot is stored as one artifact db document, and that limit is 256 KiB; stay under it with margin.
MAX_BYTES = 240_000
TREND_METRICS = ["rhr", "hrv_7d", "sleep_debt_h", "steps_7d", "mvpa_week", "sedentary_h", "weight_trend_kg",
                 "score:recovery", "score:sleep"]


ACTIVITY_TAB_METRICS = ["steps_7d", "mvpa_week", "sedentary_h", "weight_trend_kg"]  # the Activity tab's charts


def trend_metrics(ctx: Ctx) -> list[str]:
    """The metrics the dashboard's Trends tab is configured to show, plus the Activity tab's four. Plugin metrics
    allowed are the loaded plugins' names (ctx.plugins, loaded per the profile's switch) and the `plugin:*` names
    already in the analysis table; no plugin code runs here. A dashboard.toml with problems means the default
    layout, as in the built page."""
    try:
        plugin_names = {m.removeprefix("plugin:") for m in ctx.store.analysis_metric_names() if m.startswith("plugin:")}
        if ctx.plugins is not None:
            plugin_names |= set(ctx.plugins.metric_names())
        cfg, problems = load_dashboard_config(ctx.paths, plugin_names)
        wanted = list(TREND_METRICS if problems else cfg["trend_metrics"])
    except Exception:  # noqa: BLE001 - the snapshot must build whatever the layout file looks like
        wanted = list(TREND_METRICS)
    return wanted + [m for m in ACTIVITY_TAB_METRICS if m not in wanted]


def build_snapshot(ctx: Ctx) -> dict:
    """Every section is a coach tool result; a failing section keeps its {error, hint} and the rest still build."""
    def strip(result: dict) -> dict:
        result.pop("freshness", None)  # one freshness for the whole snapshot, in `status`
        return result

    metrics = trend_metrics(ctx)
    return {
        "schema": SCHEMA,
        "generated_at": iso_utc(ctx.now),
        "as_of": ctx.today.isoformat(),
        "status": tools.data_status(ctx),
        "overview": strip(tools.get_overview(ctx)),
        "trends30": strip(tools.get_trends(ctx, metrics, days=30)),
        "trends90": strip(tools.get_trends(ctx, metrics, days=90)),
        "sleep14": strip(tools.get_sleep(ctx, days=14)),
        "activity30": strip(tools.get_activity(ctx, days=30)),
        "running90": strip(tools.get_running(ctx, days=90)),
        "bodyage": strip(tools.get_body_age(ctx)),
        "targets": strip(actions.get_coach_notes(ctx, status="active")),
    }


def write_snapshot(ctx: Ctx, out: Path) -> Path:
    """Write atomically (temp file, then rename); refuse a snapshot over MAX_BYTES and leave the old file alone."""
    text = json.dumps(build_snapshot(ctx), allow_nan=False, separators=(",", ":"))
    if len(text.encode()) > MAX_BYTES:
        raise ValueError(f"snapshot too large: {len(text.encode())} bytes (max {MAX_BYTES})")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=out.parent, suffix=".tmp", delete=False) as fh:
            tmp = Path(fh.name)
            fh.write(text)
        os.replace(tmp, out)
    except BaseException:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        raise
    return out
