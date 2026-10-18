"""User prompt files: config/coach/prompts/<name>.md replaces a built-in prompt or adds one."""
from __future__ import annotations

import keyword
import os
import re
import string
from dataclasses import dataclass
from datetime import date, timedelta
from types import SimpleNamespace

from ..config import Paths
from ..userconfig import ConfigProblem

PLACEHOLDERS = {"today", "yesterday", "week_ending", "next_week", "args"}
# The built-in prompts and their arguments (the server's prompt functions; a test checks they match).
BUILTIN_PROMPT_ARGS = {"morning_checkin": [], "weekly_review": ["week_ending"], "plan_next_run": [],
                       "explain_score": ["score", "date"]}
NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


@dataclass(frozen=True)
class PromptFile:
    name: str
    text: str
    args: list[str]


def _parse(name: str, raw: str) -> PromptFile:
    args: list[str] = []
    first, _, rest = raw.partition("\n")
    if first.startswith("args:"):
        args = [a.strip() for a in first.removeprefix("args:").split(",") if a.strip()]
        raw = rest
    text = raw.strip()
    if "week_ending" not in args and _uses_week(text):  # {week_ending}/{next_week} need the argument
        args.append("week_ending")
    return PromptFile(name, text, args)


def _uses_week(text: str) -> bool:
    try:
        return any(f and f.partition(".")[0] in ("week_ending", "next_week")
                   for _, f, _, _ in string.Formatter().parse(text))
    except ValueError:  # bad syntax is reported by _check
        return False


def _check(p: PromptFile) -> str | None:
    for a in p.args:
        if not NAME.match(a) or keyword.iskeyword(a):
            return f"argument name {a!r} must be lowercase letters, digits or _ and not a Python keyword"
    if len(set(p.args)) != len(p.args):
        return "an argument is declared twice on the 'args:' line"
    try:
        fields = list(string.Formatter().parse(p.text))
    except ValueError as e:  # a stray { or }
        return f"bad placeholder syntax ({e}); write {{{{ and }}}} for literal braces"
    for _, field_name, format_spec, conversion in fields:
        if field_name is None:
            continue
        root, _, attr = field_name.partition(".")
        if root not in PLACEHOLDERS:
            return f"unknown placeholder {{{field_name}}}; allowed: {', '.join(sorted(PLACEHOLDERS))}"
        if root == "args" and attr not in p.args:
            return f"{{{field_name}}} is not declared on the 'args:' line"
        if root != "args" and attr:
            return f"{{{field_name}}}: only {{args.<name>}} has a dotted part"
        if format_spec or conversion:
            return f"{{{field_name}}}: format options (:… or !…) are not supported"
    return None


def load_prompt_files(paths: Paths) -> tuple[dict[str, PromptFile], list[ConfigProblem]]:
    folder = paths.config_dir / "coach" / "prompts"
    prompts: dict[str, PromptFile] = {}
    problems: list[ConfigProblem] = []
    if not folder.is_dir():
        return prompts, problems
    if not os.access(folder, os.R_OK | os.X_OK):
        problems.append(ConfigProblem(str(folder), "cannot read the prompts folder (check its permissions)"))
        return prompts, problems
    for f in sorted(folder.glob("*.md")):
        if not NAME.match(f.stem):
            problems.append(ConfigProblem(str(f), "file name must be lowercase letters, digits or _ (e.g. weekly.md)"))
            continue
        try:
            raw = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            problems.append(ConfigProblem(str(f), f"cannot read the file: {e}"))
            continue
        p = _parse(f.stem, raw)
        err = _check(p)
        if err:
            problems.append(ConfigProblem(str(f), err))
        else:
            prompts[p.name] = p
    return prompts, problems


def render_prompt(p: PromptFile, today: date, args: dict[str, str | None]) -> str:
    """Fill the placeholders. An invalid week_ending argument raises ValueError."""
    week_ending = date.fromisoformat(args["week_ending"]) if args.get("week_ending") else today
    values = {"today": today.isoformat(), "yesterday": (today - timedelta(days=1)).isoformat(),
              "week_ending": week_ending.isoformat(), "next_week": (week_ending + timedelta(days=7)).isoformat(),
              "args": SimpleNamespace(**{a: (args.get(a) or "") for a in p.args})}
    return p.text.format(**values)


def builtin_arg_problems(paths: Paths, prompts: dict[str, PromptFile]) -> list[ConfigProblem]:
    """A prompt file that replaces a built-in prompt must declare the built-in's arguments: the client may pass them.
    The file is still used; this is reported by `config check` and the coach's data_status alike."""
    folder = paths.config_dir / "coach" / "prompts"
    out = []
    for name, p in sorted(prompts.items()):
        missing = [a for a in BUILTIN_PROMPT_ARGS.get(name, []) if a not in p.args]
        if missing:
            out.append(ConfigProblem(str(folder / f"{name}.md"),
                                     f"replaces the built-in prompt but does not declare its argument(s) "
                                     f"{', '.join(missing)} on an 'args:' line"))
    return out
