"""User config files under <home>/config: load, validate (with file and line), and fall back safely."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .analysis.evidence import Registry, apply_overrides, load_registry
from .config import Paths


@dataclass(frozen=True)
class ConfigProblem:
    file: str
    message: str
    line: int | None = None

    def __str__(self) -> str:
        where = f"{self.file}:{self.line}" if self.line else self.file
        return f"{where}: {self.message}"


def read_toml(path: Path) -> tuple[dict, list[ConfigProblem]]:
    """{} and no problems when the file is absent; a syntax error carries its line number."""
    if not path.exists():
        return {}, []
    try:
        return tomllib.loads(path.read_text(encoding="utf-8")), []
    except tomllib.TOMLDecodeError as exc:
        line = getattr(exc, "lineno", None)
        if line is None:  # Python < 3.14 only puts it in the message: "... (at line N, column M)"
            m = re.search(r"\(at line (\d+), column", str(exc))
            line = int(m.group(1)) if m else None
        return {}, [ConfigProblem(str(path), str(exc), line)]
    except (OSError, UnicodeDecodeError) as exc:
        return {}, [ConfigProblem(str(path), f"cannot read file: {exc}")]


NOT_APPLIED = " (none of this file's overrides are applied; using built-in defaults until fixed)"


def _not_applied(p: ConfigProblem) -> ConfigProblem:
    return ConfigProblem(p.file, p.message + NOT_APPLIED, p.line)


def load_thresholds(paths: Paths) -> tuple[dict, list[ConfigProblem]]:
    path = paths.config_dir / "thresholds.toml"
    raw, problems = read_toml(path)
    unknown = set(raw) - {"params", "tables"}
    if unknown:
        problems.append(ConfigProblem(str(path), f"unknown sections {sorted(unknown)}; use [params.<name>] or "
                                                  "[tables.<name>]"))
    return raw, [_not_applied(p) for p in problems]


def registry_for(paths: Paths) -> tuple[Registry, list[ConfigProblem]]:
    """The bundled registry with the user's overrides; on any problem, the bundled registry and the problems.
    Never raises: a broken config file must not break a sync, a report or the coach."""
    path = str(paths.config_dir / "thresholds.toml")
    try:
        base = load_registry()
    except Exception as exc:  # the bundled file is broken: nothing to fall back to, so let the caller see it
        raise RuntimeError(f"bundled evidence registry is invalid: {exc}") from exc
    try:
        raw, problems = load_thresholds(paths)
        if problems or not raw:
            return base, problems
        return apply_overrides(base, raw), []
    except ValueError as exc:
        return base, [_not_applied(ConfigProblem(path, str(exc)))]
    except Exception as exc:
        return base, [_not_applied(ConfigProblem(path, f"unexpected error: {exc!r}"))]


def read_profile(paths: Paths):
    """(profile, problems), never creating the file. A profile that can't be loaded gives the built-in defaults;
    a race with only one of its two keys is a problem but the profile is still used."""
    from .analysis.profile import Profile, load_profile, profile_path

    pfile = profile_path(paths)
    if not pfile.exists():
        return Profile(), []
    _, problems = read_toml(pfile)  # a syntax error keeps its line number
    if problems:
        return Profile(), problems
    try:
        prof = load_profile(pfile)
    except (ValueError, OSError) as exc:
        return Profile(), [ConfigProblem(str(pfile), str(exc))]
    if (prof.race_date is None) != (prof.race_distance_km is None):
        missing, present = (("race_distance_km", "race_date") if prof.race_distance_km is None
                            else ("race_date", "race_distance_km"))
        problems.append(ConfigProblem(
            str(pfile), f"{present} is set but {missing} is not; set both race_date and race_distance_km to get a "
                        "race plan"))
    return prof, problems


def _check_profile(paths: Paths) -> tuple[bool, list[ConfigProblem]]:
    """(plugins enabled, problems) for the profile file. Never creates the file."""
    prof, problems = read_profile(paths)
    return prof.plugins_enabled, problems


def check_all(paths: Paths) -> list[ConfigProblem]:
    """Every problem in the user's config, plugins and prompt files. Read-only: creates nothing."""
    from .coach.persona import read_persona
    from .coach.prompt_files import builtin_arg_problems, load_prompt_files
    from .dashboard_build import load_dashboard_config
    from .plugins import load_plugins

    enabled, problems = _check_profile(paths)
    problems += registry_for(paths)[1]
    plugins = load_plugins(paths, enabled)
    problems += plugins.problems
    problems += load_dashboard_config(paths, plugins.metric_names())[1]
    prompt_files, prompt_problems = load_prompt_files(paths)
    problems += prompt_problems + builtin_arg_problems(paths, prompt_files)
    persona_file = paths.config_dir / "coach" / "persona.md"
    persona_error = read_persona(paths)[1]
    if persona_error:
        problems.append(ConfigProblem(str(persona_file), persona_error.removeprefix(f"{persona_file}: ")))
    return problems


BROKEN = " (file has problems; not applied; run config check)"


def show_all(paths: Paths) -> list[str]:
    """The effective configuration, one line each, with where every value comes from. Read-only, and honest: a
    file that fails to load is shown as built-in defaults, never as if it had been applied."""
    from dataclasses import fields

    from .analysis.profile import Profile, load_profile, profile_path
    from .coach.persona import read_persona
    from .coach.prompt_files import load_prompt_files
    from .dashboard_build import DASHBOARD_DEFAULTS, load_dashboard_config
    from .plugins import load_plugins

    lines = [f"Home: {paths.root}" + ("  (legacy repo home; run `fitbit-air home migrate`)" if paths.legacy else "")]
    pfile = profile_path(paths)
    raw, _ = read_toml(pfile)
    valid = True
    prof = Profile()
    if pfile.exists():
        try:
            prof = load_profile(pfile)
        except (ValueError, OSError):
            valid = False
    if valid:
        lines.append(f"Profile ({pfile}):")
    else:
        lines.append("Profile: invalid, showing built-in defaults (run fitbit-air config check)")
    for f in fields(Profile):
        listed = valid and (f.name in raw or (f.name == "plugins_enabled" and "plugins" in raw))
        lines.append(f"  {f.name} = {getattr(prof, f.name)}  ({pfile.name if listed else 'default'})")
    reg, reg_problems = registry_for(paths)
    lines.append("Threshold overrides:" + ("" if reg.overrides() else " none") + (BROKEN if reg_problems else ""))
    for name, o in sorted(reg.overrides().items()):
        lines.append(f"  {name} = {o['value']}  (default {o['default']}): {o['reason']}")
    persona_file = paths.config_dir / "coach" / "persona.md"
    persona, persona_error = read_persona(paths)
    used = persona_error is None and persona_file.is_file() and persona_file.read_text(encoding="utf-8").strip()
    lines.append(f"Coach persona: {persona_file if used else 'built-in default'}"
                 + (" (persona.md cannot be read; run config check)" if persona_error else ""))
    prompts, prompt_problems = load_prompt_files(paths)
    lines.append("Custom prompts: " + (", ".join(sorted(prompts)) or "none")
                 + (f" ({len(prompt_problems)} problem(s); run config check)" if prompt_problems else ""))
    plugins = load_plugins(paths, prof.plugins_enabled)
    lines.append("Plugins: " + (f"{len(plugins.files)} loaded, {len(plugins.problems)} problems"
                                if prof.plugins_enabled else "disabled"))
    dfile = paths.config_dir / "dashboard.toml"
    cfg, dash_problems = load_dashboard_config(paths, plugins.metric_names())
    changed = {k: v for k, v in cfg.items() if v != DASHBOARD_DEFAULTS[k]}
    lines.append("Dashboard" + (f" ({dfile})" if dfile.exists() else "") + ": "
                 + (", ".join(f"{k}={v}" for k, v in changed.items()) or "defaults")
                 + (BROKEN if dash_problems else ""))
    return lines
