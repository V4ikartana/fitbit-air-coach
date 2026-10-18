"""Run the analysis for a date window and record the run (shared by the CLI and the coach)."""
from __future__ import annotations

from datetime import date, timedelta

from ..config import Paths
from ..plugins import load_plugins
from ..store import Store
from ..userconfig import registry_for
from . import daily
from .evidence import load_registry
from .profile import Profile, load_profile, profile_path

MAX_PLUGIN_ERRORS = 5  # per run; the summary line carries the full count


def run_analysis(store: Store, paths: Paths, since: date | None = None, today: date | None = None):
    """Recompute analysis from `since` (default: 60 days before `today`, which defaults to the real today) and record
    the run. A broken config file falls back to defaults for that file and is recorded as the run's error: a config
    never suppresses the built-in rows."""
    today = today or date.today()
    since = since or today - timedelta(days=60)
    reg, problems = registry_for(paths)  # first: if it ever raised, no run would be left half-recorded
    run_id = store.start_analysis_run(since.isoformat())
    errors: list[str] = []
    try:
        pfile = profile_path(paths)
        try:
            profile = load_profile(pfile)
        except (ValueError, OSError) as exc:  # the safety rails don't depend on the profile
            profile = Profile()
            errors.append(f"config error: {pfile}: {exc}; analysed with the built-in profile defaults")
        plugins = load_plugins(paths, profile.plugins_enabled)  # never raises; a bad plugin is a listed problem
        try:
            summary = daily.analyze(store, profile, reg, since, today, plugins=plugins)
        except Exception as exc:
            if not reg.overrides():
                raise
            errors.append(f"config error: analysis failed with your threshold overrides ({exc!r}); used built-in "
                          "defaults")
            summary = daily.analyze(store, profile, load_registry(), since, today, plugins=plugins)
    except Exception as exc:
        store.finish_analysis_run(run_id, "; ".join([*errors, repr(exc)]), None)
        raise
    errors += [f"config error: {p}" for p in problems]
    summary.config_errors = [e.removeprefix("config error: ") for e in errors]
    errors += [e for e in [getattr(summary, "error", None)] if e]  # Phase B failure, if any
    errors += [f"plugin: {p}" for p in plugins.problems]
    run_problems = getattr(summary, "plugin_problems", [])
    errors += [f"plugin: {p}" for p in run_problems[:MAX_PLUGIN_ERRORS]]
    if len(run_problems) > MAX_PLUGIN_ERRORS:
        errors.append(f"plugin: ... and {len(run_problems) - MAX_PLUGIN_ERRORS} more")
    store.finish_analysis_run(run_id, "; ".join(errors) or None, str(summary))
    return summary
