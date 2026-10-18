"""User profile for the analysis: facts Google doesn't provide, kept in config/profile.toml."""
from __future__ import annotations

import math
import os
import tempfile
import tomllib
from dataclasses import dataclass, field, fields
from datetime import date
from pathlib import Path
from zoneinfo import available_timezones

from ..config import Paths

VALID_SEX = ("male", "female", "unspecified")
VALID_UNITS = ("metric", "imperial")
MAX_GOALS, MAX_GOAL_CHARS = 10, 60  # goals go into the coach's instructions: keep them short
_FLOAT_KEYS = {"race_distance_km", "sleep_need_h", "height_m"}
_INT_KEYS = {"hrmax_override"}
_BOOL_KEYS = {"plugins_enabled"}
_OPTIONAL_KEYS = {"hrmax_override", "height_m", "race_date", "race_distance_km", "timezone"}
_KEY_NOTES = {"units": "  # used by the coach for wording; report and dashboard show metric for now",
              "timezone": "  # reserved (not used yet)"}


@dataclass
class Profile:
    sex: str = "unspecified"
    race_date: str | None = None
    race_distance_km: float | None = None
    sleep_need_h: float = 7.5
    goals: list[str] = field(default_factory=list)
    hrmax_override: int | None = None
    height_m: float | None = None
    units: str = "metric"
    timezone: str | None = None
    plugins_enabled: bool = False

    @property
    def has_race(self) -> bool:
        return self.race_date is not None and self.race_distance_km is not None

    def validate(self) -> None:
        if self.sex not in VALID_SEX:
            raise ValueError(f"sex must be one of {', '.join(VALID_SEX)}")
        if not isinstance(self.plugins_enabled, bool):
            raise ValueError("plugins_enabled must be true or false")
        if self.units not in VALID_UNITS:
            raise ValueError(f"units must be one of {', '.join(VALID_UNITS)}")
        if len(self.goals) > MAX_GOALS:
            raise ValueError(f"goals: at most {MAX_GOALS} goals, got {len(self.goals)}")
        long_goals = [g for g in self.goals if len(g) > MAX_GOAL_CHARS]
        if long_goals:
            raise ValueError(f"goals: each goal must be at most {MAX_GOAL_CHARS} characters "
                             f"({long_goals[0][:20]!r}…)")
        texts = [self.sex, self.units, *self.goals, *(t for t in (self.race_date, self.timezone) if t)]
        for text in texts:
            if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
                raise ValueError("profile text values must not contain control characters")
        if self.race_date is not None and date.fromisoformat(self.race_date).isoformat() != self.race_date:
            raise ValueError("race_date must be YYYY-MM-DD")  # strictly YYYY-MM-DD
        if self.race_distance_km is not None and not (
                math.isfinite(self.race_distance_km) and 0 < self.race_distance_km <= 100):
            raise ValueError("race_distance_km must be a number above 0 and at most 100")
        if not 3 <= self.sleep_need_h <= 12:
            raise ValueError("sleep_need_h must be between 3 and 12")
        if self.hrmax_override is not None and not 120 <= self.hrmax_override <= 230:
            raise ValueError("hrmax_override must be between 120 and 230")
        if self.height_m is not None and not 1.0 <= self.height_m <= 2.5:
            raise ValueError("height_m must be between 1.0 and 2.5")
        if self.timezone is not None:
            if self.timezone not in available_timezones():  # exact names, so wrong case fails on every OS
                raise ValueError(f"timezone {self.timezone!r} is not a known IANA zone (e.g. Europe/London)")


def profile_path(paths: Paths) -> Path:
    """config/profile.toml; an old data/profile.toml is still read until `home migrate` moves it."""
    if not paths.profile_file.exists() and (paths.data_dir / "profile.toml").exists():
        return paths.data_dir / "profile.toml"
    return paths.profile_file


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} to TOML")


def save_profile(path: Path, profile: Profile) -> None:
    """Validate and write the profile atomically (temp file in the same directory, then os.replace)."""
    profile.validate()
    lines = [
        "# Fitbit Air analysis profile. Rewritten by `fitbit-air profile set KEY VALUE`;"
        " comments and formatting are not preserved."
    ]
    for f in fields(Profile):
        if f.name == "plugins_enabled":
            continue
        value = getattr(profile, f.name)
        note = _KEY_NOTES.get(f.name, "")
        lines.append(f"# {f.name} (not set){note}" if value is None else f"{f.name} = {_toml_value(value)}{note}")
    lines += ["", "[plugins]", f"enabled = {_toml_value(profile.plugins_enabled)}"]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _number(key: str, value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number, got {value!r}")
    return float(value)


def _normalise(data: dict) -> dict:
    """Coerce TOML-native types to the Profile field types; every problem is a ValueError."""
    out = dict(data)
    for key in _FLOAT_KEYS & out.keys():
        out[key] = _number(key, out[key])
    if "hrmax_override" in out:
        value = _number("hrmax_override", out["hrmax_override"])
        if not value.is_integer():
            raise ValueError(f"hrmax_override must be a whole number, got {out['hrmax_override']!r}")
        out["hrmax_override"] = int(value)
    if type(out.get("race_date")) is date:  # native TOML date
        out["race_date"] = out["race_date"].isoformat()
    for key in ("sex", "race_date", "timezone", "units"):
        if key in out and not isinstance(out[key], str):
            raise ValueError(f"{key} must be a string, got {out[key]!r}")
    if "goals" in out and not (isinstance(out["goals"], list) and all(isinstance(g, str) for g in out["goals"])):
        raise ValueError(f"goals must be a list of strings, got {out['goals']!r}")
    return out


def load_profile(path: Path) -> Profile:
    """Load the profile, creating it with defaults on first use."""
    if not path.exists():
        profile = Profile()
        save_profile(path, profile)
        return profile
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if "plugins_enabled" in data:
        raise ValueError("plugins_enabled isn't a top-level key; use a [plugins] table with `enabled = true|false`")
    plugins = data.pop("plugins", {})
    extra = sorted(set(plugins) - {"enabled"}) if isinstance(plugins, dict) else []
    if extra:
        raise ValueError(f"[plugins] takes only `enabled = true|false`; keys written after [plugins] belong to that "
                         f"table; move {', '.join(extra)} above the [plugins] line")
    if not isinstance(plugins, dict) or not isinstance(plugins.get("enabled", False), bool):
        raise ValueError("[plugins] takes only `enabled = true|false`")
    data["plugins_enabled"] = plugins.get("enabled", False)
    unknown = set(data) - {f.name for f in fields(Profile)}
    if unknown:
        raise ValueError(f"unknown profile keys: {sorted(unknown)}")
    profile = Profile(**_normalise(data))
    profile.validate()
    return profile


def set_value(path: Path, key: str, raw: str) -> Profile:
    """Set one key from a command-line string, validate, and save."""
    names = [f.name for f in fields(Profile)]
    if key not in names:
        raise ValueError(f"unknown key {key!r}; valid keys: {', '.join(names)}")
    profile = load_profile(path)
    if key in _OPTIONAL_KEYS and raw.strip().lower() in ("", "none"):
        value = None
    elif key in _BOOL_KEYS:
        if raw.strip().lower() not in ("true", "false"):
            raise ValueError(f"{key} must be true or false")
        value = raw.strip().lower() == "true"
    elif key in _FLOAT_KEYS or key in _INT_KEYS:
        try:
            value = float(raw) if key in _FLOAT_KEYS else int(raw)
        except ValueError as exc:
            raise ValueError(f"{key}: {exc}") from None
    elif key == "goals":
        value = [g.strip() for g in raw.split(",") if g.strip()]
    elif key in ("sex", "units"):
        value = raw.strip().lower()
    else:
        value = raw.strip()
    setattr(profile, key, value)
    profile.validate()
    save_profile(path, profile)
    return profile
