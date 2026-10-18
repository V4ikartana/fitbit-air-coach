"""User plugins: <home>/plugins/*.py with register(api). Off unless [plugins] enabled = true. A plugin is the
user's own code running with their permissions; a broken one is logged and skipped, never breaking a sync."""
from __future__ import annotations

import contextlib
import importlib.util
import inspect
import logging
import math
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Paths
from .userconfig import ConfigProblem

log = logging.getLogger(__name__)
NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_TEXT = 2000
FINDING_SEVERITIES = ("info", "watch")  # never "alert": only the built-in safety rails raise alerts
# SystemExit is not an Exception: a plugin calling sys.exit() must not end a sync or the coach server
PLUGIN_ERRORS = (Exception, SystemExit)
_clock = time.monotonic  # replaced in tests


@dataclass
class LoadedPlugins:
    metrics: dict[str, dict] = field(default_factory=dict)   # name -> {fn, unit, description, file}
    findings: dict[str, dict] = field(default_factory=dict)  # name -> {fn, evidence_note, file}
    tools: dict[str, dict] = field(default_factory=dict)     # name -> {fn, description, file}
    problems: list[ConfigProblem] = field(default_factory=list)
    files: list[str] = field(default_factory=list)  # plugin files that loaded without error

    def metric_names(self) -> set[str]:
        return set(self.metrics)

    def metric_meta(self) -> dict[str, dict]:
        """name -> {unit, description}: what the dashboard needs to label a plugin metric."""
        return {n: {"unit": m["unit"], "description": m["description"]} for n, m in self.metrics.items()}

    def evidence_note(self, name: str) -> str | None:
        f = self.findings.get(name)
        return f["evidence_note"] if f else None


class PluginAPI:
    def __init__(self, loaded: LoadedPlugins, file: str):
        self._l, self._file = loaded, file

    def _name(self, kind: str, name: str, table: dict) -> None:
        if not isinstance(name, str) or not NAME.match(name):
            raise ValueError(f"{kind} name {name!r} must be lowercase letters, digits or _ (max 40)")
        if name in table:
            raise ValueError(f"{kind} {name!r} is already registered by {table[name]['file']}")

    @staticmethod
    def _callable(kind: str, fn: Callable) -> None:
        if not callable(fn):
            raise ValueError(f"{kind} needs a function, got {type(fn).__name__}")

    def metric(self, name: str, fn: Callable, unit: str = "", description: str = "") -> None:
        self._name("metric", name, self._l.metrics)
        self._callable("metric", fn)
        self._l.metrics[name] = {"fn": fn, "unit": str(unit)[:20], "description": str(description)[:200],
                                 "file": self._file}

    def finding(self, name: str, fn: Callable, evidence_note: str) -> None:
        self._name("finding", name, self._l.findings)
        self._callable("finding", fn)
        self._l.findings[name] = {"fn": fn, "evidence_note": str(evidence_note)[:MAX_TEXT], "file": self._file}

    def coach_tool(self, name: str, fn: Callable, description: str) -> None:
        self._name("coach tool", name, self._l.tools)
        self._callable("coach tool", fn)
        params = list(inspect.signature(fn).parameters.values())
        if not params or params[0].kind not in (inspect.Parameter.POSITIONAL_ONLY,
                                                inspect.Parameter.POSITIONAL_OR_KEYWORD):
            raise ValueError("a coach tool is fn(ctx, **kwargs): its first parameter receives the context")
        if any(p.kind is inspect.Parameter.POSITIONAL_ONLY for p in params[1:]):
            raise ValueError("coach tool parameters after ctx must be usable by keyword (no `/` before them)")
        self._l.tools[name] = {"fn": fn, "description": str(description)[:500], "file": self._file}


def _module_name(f: Path) -> str:
    return f"fitbit_air_user_plugin_{f.stem}"


def _register_file(loaded: LoadedPlugins, f: Path) -> None:
    spec = importlib.util.spec_from_file_location(_module_name(f), f)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # before exec: @dataclass and typing look the module up by name
    spec.loader.exec_module(module)
    register = getattr(module, "register", None)
    if not callable(register):
        raise ValueError("defines no register(api) function")
    register(PluginAPI(loaded, str(f)))


def load_plugins(paths: Paths, enabled: bool) -> LoadedPlugins:
    """Never raises. A file that fails is skipped whole (nothing it registered before failing is kept)."""
    loaded = LoadedPlugins()
    if not enabled:
        return loaded
    try:
        files = sorted(paths.plugins_dir.glob("*.py")) if paths.plugins_dir.is_dir() else []
    except OSError as exc:
        loaded.problems.append(ConfigProblem(str(paths.plugins_dir), f"cannot list the plugins folder: {exc}"))
        return loaded
    for f in files:
        before = (dict(loaded.metrics), dict(loaded.findings), dict(loaded.tools))
        try:
            # stdout is the MCP stream when the coach server loads plugins: a stray print() must not corrupt it
            with contextlib.redirect_stdout(sys.stderr):
                _register_file(loaded, f)
            loaded.files.append(str(f))
        except PLUGIN_ERRORS as exc:  # a plugin must never break the app
            log.warning("plugin %s skipped: %r", f, exc)
            loaded.metrics, loaded.findings, loaded.tools = before
            sys.modules.pop(_module_name(f), None)
            loaded.problems.append(ConfigProblem(str(f), f"plugin skipped: {exc!r}"))
    return loaded


class DayContext:
    """What a plugin sees for one day: analysis metrics and raw Google daily values, read-only."""

    def __init__(self, metrics: dict[str, float], daily: dict[str, float]):
        self._m, self._d = dict(metrics), dict(daily)

    def metric(self, name: str) -> float | None:
        return self._m.get(name)

    def daily(self, name: str) -> float | None:
        return self._d.get(name)


def _where(table: dict, name: str) -> str:
    return f"{name} ({Path(table[name]['file']).name})"


def _over(spent: dict[str, float] | None, budget_s: float | None, file: str) -> bool:
    return spent is not None and budget_s is not None and spent.get(file, 0.0) > budget_s


@contextlib.contextmanager
def _timed(spent: dict[str, float] | None, file: str):
    start = _clock()
    try:
        yield
    finally:
        if spent is not None:
            spent[file] = spent.get(file, 0.0) + (_clock() - start)


def run_daily_plugins(loaded: LoadedPlugins, day: str, ctx: DayContext, spent: dict[str, float] | None = None,
                      budget_s: float | None = None) -> tuple[list[dict], list[dict], list[str]]:
    """(metric rows, insight rows, problems) for one day; bad values and exceptions are dropped and reported.
    `spent` (seconds used so far, per plugin file) and `budget_s` let the caller stop a slow plugin: once a file is
    over budget its metrics and findings are skipped. The check runs between calls; a call never gets interrupted."""
    metrics, insights, problems = [], [], []
    for name, m in loaded.metrics.items():
        if _over(spent, budget_s, m["file"]):
            continue
        try:
            with _timed(spent, m["file"]):
                v = m["fn"](day, ctx)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError(f"returned {v!r}, not a finite number")
            metrics.append({"local_date": day, "metric": f"plugin:{name}", "value": float(v), "quality": "good"})
        except PLUGIN_ERRORS as exc:
            problems.append(f"plugin metric {_where(loaded.metrics, name)}: {exc!r}")
    for name, fdef in loaded.findings.items():
        if _over(spent, budget_s, fdef["file"]):
            continue
        try:
            with _timed(spent, fdef["file"]):
                r = fdef["fn"](day, ctx)
            if r is None:
                continue
            if not isinstance(r, dict):
                raise ValueError(f"returned {type(r).__name__}, not a dict")
            if r.get("severity") not in FINDING_SEVERITIES:
                raise ValueError("severity must be info or watch (only safety rails raise alerts)")
            insights.append({"local_date": day, "kind": "plugin", "severity": r["severity"],
                             "title": str(r.get("title", name))[:200], "detail": str(r.get("detail", ""))[:MAX_TEXT],
                             "evidence_ids": [f"plugin:{name}"]})
        except PLUGIN_ERRORS as exc:
            problems.append(f"plugin finding {_where(loaded.findings, name)}: {exc!r}")
    return metrics, insights, problems
