"""Build the dashboard page from the template plus <home>/config/dashboard.toml (layout only, never data)."""
from __future__ import annotations

import json
import re
from importlib.resources import files
from pathlib import Path

from .config import Paths
from .userconfig import ConfigProblem, read_toml

TABS = ("today", "trends", "sleep", "activity", "running", "bodyage")
CARDS = ("plan", "sleep_debt", "activity", "body")
METRICS = ("rhr", "hrv_7d", "sleep_debt_h", "steps_7d", "mvpa_week", "sedentary_h", "weight_trend_kg", "bmi",
           "score:recovery", "score:sleep", "score:body_age")
THEME_TOKENS = ("bg", "panel", "panel-2", "line", "fg", "muted", "faint", "recovery", "sleep", "age", "run",
                "ok", "warn", "bad", "track", "tip")
RANGES = (7, 30, 90, 365)
DASHBOARD_DEFAULTS = {
    "tabs": list(TABS), "today_cards": list(CARDS),
    "trend_metrics": ["rhr", "hrv_7d", "sleep_debt_h", "steps_7d", "mvpa_week", "sedentary_h", "weight_trend_kg",
                      "score:recovery", "score:sleep"],
    "default_range_days": 30, "theme": {"dark": {}, "light": {}},
}
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
LISTS = ("tabs", "today_cards", "trend_metrics")
MAX_TREND_METRICS = 12  # keeps the snapshot (one artifact document) under its size cap


def _check_list(f: str, key: str, value, allowed: set[str], cfg: dict, problems: list[ConfigProblem]) -> None:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        problems.append(ConfigProblem(f, f"{key} must be a list of names; allowed: {sorted(allowed)}"))
        return
    bad = [v for v in value if v not in allowed]
    if bad:
        problems.append(ConfigProblem(f, f"{key}: unknown {bad}; allowed: {sorted(allowed)}"))
    elif key == "tabs" and not value:
        problems.append(ConfigProblem(f, "tabs must list at least one tab"))
    elif key == "trend_metrics" and len(dict.fromkeys(value)) > MAX_TREND_METRICS:
        problems.append(ConfigProblem(f, f"trend_metrics lists {len(dict.fromkeys(value))} metrics; at most "
                                         f"{MAX_TREND_METRICS} are allowed"))
    else:
        cfg[key] = list(dict.fromkeys(value))  # drop repeats, keep the order


def _check_theme(f: str, value, cfg: dict, problems: list[ConfigProblem]) -> None:
    if not isinstance(value, dict):
        problems.append(ConfigProblem(f, "theme: use [theme] with dark = {…} and/or light = {…}"))
        return
    for mode, colours in value.items():
        if mode not in ("dark", "light") or not isinstance(colours, dict):
            problems.append(ConfigProblem(f, f"theme.{mode}: use [theme] dark = {{…}} / light = {{…}}"))
            continue
        for token, colour in colours.items():
            if token not in THEME_TOKENS:
                problems.append(ConfigProblem(f, f"theme.{mode}.{token}: unknown token; allowed {THEME_TOKENS}"))
            elif not isinstance(colour, str) or not HEX.match(colour):
                problems.append(ConfigProblem(f, f"theme.{mode}.{token}: colour must be #rrggbb"))
            else:
                cfg["theme"][mode][token] = colour


def load_dashboard_config(paths: Paths, plugin_metrics: set[str]) -> tuple[dict, list[ConfigProblem]]:
    path = paths.config_dir / "dashboard.toml"
    raw, problems = read_toml(path)
    f = str(path)
    cfg = json.loads(json.dumps(DASHBOARD_DEFAULTS))
    allowed = {"tabs": set(TABS), "today_cards": set(CARDS),
               "trend_metrics": set(METRICS) | {f"plugin:{m}" for m in plugin_metrics}}
    for key, value in raw.items():
        if key not in DASHBOARD_DEFAULTS:
            problems.append(ConfigProblem(f, f"unknown key {key!r}"))
        elif key in LISTS:
            _check_list(f, key, value, allowed[key], cfg, problems)
        elif key == "default_range_days":
            if isinstance(value, bool) or value not in RANGES:
                problems.append(ConfigProblem(f, f"default_range_days must be one of {RANGES}, got {value}"))
            else:
                cfg[key] = int(value)
        elif key == "theme":
            _check_theme(f, value, cfg, problems)
    return cfg, problems


def _theme_css(theme: dict) -> str:
    def block(colours: dict) -> str:
        return " ".join(f"--{k}: {v};" for k, v in colours.items())
    css = []
    dark, light_colours = (theme.get(k) for k in ("dark", "light")) if isinstance(theme, dict) else (None, None)
    if isinstance(dark, dict) and dark:
        css.append(f":root {{ {block(dark)} }}")
        css.append(f':root[data-theme="dark"] {{ {block(dark)} }}')
    if isinstance(light_colours, dict) and light_colours:
        light = block(light_colours)
        css.append(f'@media (prefers-color-scheme: light) {{ :root:not([data-theme="dark"]) {{ {light} }} }}')
        css.append(f':root[data-theme="light"] {{ {light} }}')
    return "\n".join(css)


def _embed_json(cfg: dict) -> str:
    """JSON safe inside a <script> element: no "<" survives, so neither "</script>" nor "<!--" can appear."""
    return json.dumps(cfg, separators=(",", ":")).replace("<", "\\u003c")


def render_page(cfg: dict, template: str | None = None) -> str:
    """The template with `cfg` embedded as fa-config and its theme CSS. Does not validate: build_dashboard does."""
    template = template or files("fitbit_air.dashboard").joinpath("template.html").read_text(encoding="utf-8")
    inject = f'<script type="application/json" id="fa-config">{_embed_json(cfg)}</script>\n'
    theme = _theme_css(cfg.get("theme"))
    if theme:
        inject += f'<style id="fa-theme">\n{theme}\n</style>\n'
    marker = "</style>"  # after the template's own first <style> block, so the overrides win
    i = template.index(marker) + len(marker)
    return template[:i] + "\n" + inject + template[i:]


def build_dashboard(paths: Paths, plugin_metrics: set[str] | dict[str, dict], out: Path | None = None) -> Path:
    """`plugin_metrics` is the set of plugin metric names, or {name: {"unit", "description"}} so the page can label
    them (the units and descriptions are embedded as `plugin_meta`)."""
    cfg, problems = load_dashboard_config(paths, set(plugin_metrics))
    if problems:
        raise ValueError("; ".join(str(p) for p in problems))
    if isinstance(plugin_metrics, dict):
        cfg["plugin_meta"] = {name: {"unit": str(m.get("unit", "")), "description": str(m.get("description", ""))}
                              for name, m in plugin_metrics.items()}
    html = render_page(cfg)
    out = out or paths.data_dir / "dashboard" / "dashboard.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out
