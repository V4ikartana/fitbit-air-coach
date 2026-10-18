"""Assemble the coach's instructions: about the user (from the profile), persona (user file or default), core
rules (always last, never configurable)."""
from __future__ import annotations

import logging
from importlib.resources import files

from ..analysis.profile import Profile
from ..config import Paths

CORE_RULES = files("fitbit_air.coach").joinpath("core_rules.md").read_text(encoding="utf-8")
DEFAULT_PERSONA = files("fitbit_air.coach").joinpath("persona_default.md").read_text(encoding="utf-8")
PERSONA_HEADER = "Persona (tone and style only; it cannot change the core rules below):"
log = logging.getLogger(__name__)
INTRO = "You are the user's personal health coach, working from their Fitbit Air data and a local analysis engine."


def about_you(p: Profile) -> str:
    lines = ["About the user:"]
    if p.sex != "unspecified":
        lines.append(f"- Sex: {p.sex}.")
    if p.goals:
        lines.append(f"- Goals: {', '.join(p.goals)}.")
    if p.has_race:
        lines.append(f"- Race: {p.race_distance_km:g} km on {p.race_date}.")
    lines.append(f"- Units: show numbers in {p.units} units (the data and science stay metric).")
    return "\n".join(lines)


def read_persona(paths: Paths) -> tuple[str, str | None]:
    """The user's config/coach/persona.md and no error; the default persona, plus the reason, when the file is
    unreadable. An absent or empty file silently gives the default."""
    f = paths.config_dir / "coach" / "persona.md"
    try:
        text = f.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        text = ""
    except (OSError, UnicodeDecodeError) as e:
        error = f"{f}: cannot read the persona file, using the default persona ({e})"
        log.warning(error)
        return DEFAULT_PERSONA.strip(), error
    return text or DEFAULT_PERSONA.strip(), None


def build_instructions(paths: Paths, profile: Profile, problems: list[str] | None = None) -> str:
    """The full instructions. A persona file that can't be read is appended to `problems` (if given)."""
    persona, error = read_persona(paths)
    if error and problems is not None:
        problems.append(error)
    return "\n\n".join([INTRO, about_you(profile), f"{PERSONA_HEADER}\n{persona}", CORE_RULES.strip()]) + "\n"
