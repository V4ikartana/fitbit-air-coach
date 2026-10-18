"""Registry of the studies, guidelines and design choices behind every score (evidence.toml)."""
from __future__ import annotations

import difflib
import math
import tomllib
from dataclasses import dataclass
from functools import cache
from importlib import resources


@dataclass(frozen=True)
class Source:
    id: str
    citation: str
    doi: str | None
    design_choice: bool
    used_for: tuple[str, ...]
    effect: str
    ref: str | None = None  # optional page/table reference for numeric anchors


_TABLE_KINDS = ("step", "linear", "loglinear")


@dataclass(frozen=True)
class Table:
    """Anchor points (x, y) with x strictly increasing. Outside the anchors, y is held at the end value.

    step: y of the last anchor with x_i <= x. linear: linear interpolation. loglinear: linear interpolation of
    ln(y) (for hazard ratios), so midpoints are geometric."""
    kind: str
    points: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        if self.kind not in _TABLE_KINDS:
            raise ValueError(f"table kind must be one of {_TABLE_KINDS}, got {self.kind!r}")
        if not isinstance(self.points, (list, tuple)) or len(self.points) < 2:
            raise ValueError("table needs at least two points")
        pts: list[tuple[float, float]] = []
        for p in self.points:
            if (not isinstance(p, (list, tuple)) or len(p) != 2
                    or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                           for v in p)):
                raise ValueError("each table point must be (x, y) finite numbers")
            pts.append((float(p[0]), float(p[1])))
        if any(b[0] <= a[0] for a, b in zip(pts, pts[1:])):
            raise ValueError("table x values must be strictly increasing")
        if self.kind == "loglinear" and any(y <= 0 for _, y in pts):
            raise ValueError("loglinear table y values must be positive")
        object.__setattr__(self, "points", tuple(pts))

    def value(self, x: float) -> float:
        if not math.isfinite(x):
            raise ValueError(f"non-finite table input: {x!r}")
        pts = self.points
        if x <= pts[0][0]:
            return pts[0][1]
        if x >= pts[-1][0]:
            return pts[-1][1]
        if self.kind == "step":
            return max((p for p in pts if p[0] <= x), key=lambda p: p[0])[1]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if x0 <= x <= x1:
                t = (x - x0) / (x1 - x0)
                if self.kind == "linear":
                    return y0 + t * (y1 - y0)
                assert self.kind == "loglinear"
                return math.exp(math.log(y0) + t * (math.log(y1) - math.log(y0)))
        raise AssertionError("unreachable")


# Safety rails. "locked": a user override is refused (data sanity or the definition of a rail); "lower"/"higher":
# the override may only move the value in that direction, the stricter one. Every param that safety.py or
# signals.py reads must be listed here (tests/test_userconfig_thresholds.py checks it).
SAFETY_DIRECTIONS: dict[str, str] = {
    # locked: plausibility ranges decide which readings count at all, the rest define a rail's window or baselines
    "plaus_spo2_min": "locked", "plaus_spo2_max": "locked", "plaus_rhr_min": "locked", "plaus_rhr_max": "locked",
    "plaus_vo2_min": "locked", "plaus_vo2_max": "locked", "plaus_bmi_min": "locked", "plaus_bmi_max": "locked",
    "plaus_sleep_min_max": "locked", "plaus_steps_max": "locked", "plaus_mvpa_max": "locked",
    "safety_window_days": "locked", "weekly_lookback_min_days": "locked", "spike_lookback_days": "locked",
    "weight_rate_window_days": "locked", "min_n_signals": "locked",
    "floor_rhr": "locked", "floor_resp": "locked", "floor_temp": "locked", "floor_spo2": "locked",
    "floor_hrv": "locked",
    # lower: a lower value makes the alarm fire sooner or more often
    "safety_rhr_bpm": "lower", "safety_rhr_nights": "lower", "safety_spo2_nights": "lower",
    "safety_strain_days": "lower", "forecast_growth": "lower", "spike_pct": "lower", "weight_rate_max_pct": "lower",
    "sig_rhr_z": "lower", "sig_rhr_bpm": "lower", "sig_resp_br": "lower", "sig_resp_z": "lower",
    "sig_temp_dev_c": "lower", "sig_temp_z": "lower", "sig_spo2_drop": "lower", "cusum_k": "lower",
    "cusum_h": "lower", "strain_persist_nights": "lower", "weight_min_weighins": "lower",
    "forecast_cutback_every": "lower", "hrv_band_halfwidth_sd": "lower",  # narrower band: "below" fires sooner
    # higher: a higher value makes the alarm fire sooner or more often
    "safety_spo2_pct": "higher", "sig_hrv_z": "higher", "strain_max_skipped_nights": "higher",
    "forecast_taper_weeks": "higher",  # a longer taper starts the build earlier: the plan gets more conservative
}
_WHOLE_SUFFIXES = ("_days", "_nights", "_weeks", "_every", "_weighins")

# Endpoint pairs (x0, x1) that the analysis passes to mathutil.lin() (sleep.py), or that bound a range it tests.
# After overrides, each pair's ends must still differ and keep this order: equal ends make lin() raise, and swapped
# ends invert a score.
LIN_PAIRS: tuple[tuple[str, str], ...] = (
    ("sleep_dur_zero_h", "sleep_dur_ok_min_h"),
    ("sleep_dur_ok_min_h", "sleep_dur_ok_max_h"),
    ("sleep_dur_ok_max_h", "sleep_dur_long_h"),
    ("eff_zero", "eff_full"),
    ("sri_zero", "sri_full"),
    ("timing_full_min", "timing_zero_min"),
)


def safety_direction(name: str) -> str | None:
    """'locked', 'lower', 'higher' or None; any plaus_* param is locked even if not listed."""
    return SAFETY_DIRECTIONS.get(name) or ("locked" if name.startswith("plaus_") else None)


class Registry:
    def __init__(self, sources: dict[str, Source], params: dict[str, float], tables: dict[str, Table] | None = None,
                 bounds: dict[str, tuple[float, float]] | None = None, overrides: dict[str, dict] | None = None):
        self._sources = sources
        self._params = params
        self._tables = tables or {}
        self._bounds = bounds or {}  # explicit [bounds] entries
        self._overrides = overrides or {}  # name -> {value, default, reason}

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(self._sources)

    def has(self, source_id: str) -> bool:
        return source_id in self._sources

    def source(self, source_id: str) -> Source:
        try:
            return self._sources[source_id]
        except KeyError:
            raise KeyError(f"unknown evidence source {source_id!r}") from None

    def param(self, name: str) -> float:
        try:
            return self._params[name]
        except KeyError:
            raise KeyError(f"missing evidence param {name!r}") from None

    def table(self, name: str) -> Table:
        try:
            return self._tables[name]
        except KeyError:
            raise KeyError(f"missing evidence table {name!r}") from None

    def param_names(self) -> list[str]:
        return sorted(self._params)

    def table_names(self) -> list[str]:
        return sorted(self._tables)

    def override(self, name: str) -> dict | None:
        return self._overrides.get(name)

    def overrides(self) -> dict[str, dict]:
        return {k: dict(v) for k, v in self._overrides.items()}


def bounds_for(reg: Registry, name: str) -> tuple[float, float]:
    """Explicit [bounds] entry, else [v/4, v*4] for a positive default (percentages capped at 100)."""
    if name in reg._bounds:
        return reg._bounds[name]
    v = reg.param(name)
    if v <= 0:
        raise ValueError(f"evidence param {name!r} needs an explicit [bounds] entry")
    hi = v * 4
    if name.endswith("_pct"):
        hi = min(hi, 100.0)
    return v / 4, hi


_SOURCE_KEYS = frozenset({"id", "citation", "doi", "used_for", "effect", "design_choice", "ref"})


def _validate_source(entry: dict) -> Source:
    name = entry.get("id", "?")
    unknown = set(entry) - _SOURCE_KEYS
    if unknown:
        raise ValueError(f"evidence source {name!r} has unknown keys: {sorted(unknown)}")
    for key in ("id", "citation", "effect"):
        if key not in entry:
            raise ValueError(f"evidence source {name!r} is missing {key!r}")
        if not isinstance(entry[key], str) or not entry[key].strip():
            raise ValueError(f"evidence source {name!r}: {key!r} must be a non-empty string")
    for key in ("doi", "ref"):
        if key in entry and not isinstance(entry[key], str):
            raise ValueError(f"evidence source {name!r}: {key!r} must be a string")
    design_choice = entry.get("design_choice", False)
    if not isinstance(design_choice, bool):
        raise ValueError(f"evidence source {name!r}: 'design_choice' must be true or false")
    used_for = entry.get("used_for", [])
    if not isinstance(used_for, list) or not all(isinstance(u, str) for u in used_for):
        raise ValueError(f"evidence source {name!r}: 'used_for' must be a list of strings")
    if name.startswith("design_") and not design_choice:
        raise ValueError(f"evidence source {name!r} must set design_choice = true")
    return Source(
        id=entry["id"],
        citation=entry["citation"],
        doi=entry.get("doi"),
        design_choice=design_choice,
        used_for=tuple(used_for),
        effect=entry["effect"],
        ref=entry.get("ref"),
    )


def _validate_table(name: str, entry: dict) -> Table:
    if set(entry) != {"kind", "points"}:
        raise ValueError(f"evidence table {name!r} must have exactly 'kind' and 'points'")
    raw = entry["points"]
    if not isinstance(raw, list) or not all(isinstance(p, list) and len(p) == 2 for p in raw):
        raise ValueError(f"evidence table {name!r}: points must be a list of [x, y] pairs")
    try:
        return Table(kind=entry["kind"], points=tuple((p[0], p[1]) for p in raw))
    except ValueError as exc:
        raise ValueError(f"evidence table {name!r}: {exc}") from None


@cache
def _bundled() -> Registry:
    text = resources.files("fitbit_air.analysis").joinpath("evidence.toml").read_text(encoding="utf-8")
    return load_registry(text)


def load_registry(text: str | None = None) -> Registry:
    """Load the bundled evidence.toml (cached) or `text` (for tests), and validate it."""
    if text is None:
        return _bundled()
    raw = tomllib.loads(text)
    sources: dict[str, Source] = {}
    for entry in raw.get("source", []):
        source = _validate_source(entry)
        if source.id in sources:
            raise ValueError(f"duplicate evidence source {source.id!r}")
        sources[source.id] = source
    params: dict[str, float] = {}
    for k, v in raw.get("params", {}).items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"evidence param {k!r} must be a finite number, got {v!r}")
        params[k] = float(v)
    raw_tables = raw.get("tables", {})
    if not isinstance(raw_tables, dict) or not all(isinstance(e, dict) for e in raw_tables.values()):
        raise ValueError("evidence 'tables' must be a table of tables")
    tables = {name: _validate_table(name, entry) for name, entry in raw_tables.items()}
    bounds: dict[str, tuple[float, float]] = {}
    for k, v in raw.get("bounds", {}).items():
        if k not in params:
            raise ValueError(f"evidence bounds entry {k!r} is not a known param")
        if (not isinstance(v, list) or len(v) != 2
                or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in v)
                or v[0] > v[1]):
            raise ValueError(f"evidence bounds {k!r} must be [lo, hi] finite numbers with lo <= hi")
        bounds[k] = (float(v[0]), float(v[1]))
    return Registry(sources, params, tables, bounds)


_PARAM_KEYS = frozenset({"value", "reason"})
_TABLE_KEYS = frozenset({"kind", "points", "reason"})


def _entry_problem(kind: str, name: str, entry, allowed: frozenset[str]) -> str | None:
    if not isinstance(entry, dict):
        return f"{kind} {name!r}: must be a table with {sorted(allowed)}"
    extra = sorted(set(entry) - allowed)
    if extra:
        return f"{kind} {name!r}: unknown keys {extra}; allowed are {sorted(allowed)}"
    if not str(entry.get("reason", "")).strip():
        return f"{kind} {name!r}: needs a non-empty reason"
    return None


def apply_overrides(base: Registry, raw: dict) -> Registry:
    """A new Registry with user overrides applied; ValueError (all problems joined) if any is invalid."""
    problems: list[str] = []
    params = dict(base._params)
    tables = dict(base._tables)
    recorded: dict[str, dict] = {}
    sections = {}
    for key in ("params", "tables"):
        section = raw.get(key) or {}
        if not isinstance(section, dict):
            problems.append(f"[{key}] must be a table of [{key}.<name>] entries")
            section = {}
        sections[key] = section
    for name, entry in sections["params"].items():
        if name not in params:
            near = difflib.get_close_matches(name, params, n=1)
            problems.append(f"unknown param {name!r}" + (f"; did you mean {near[0]!r}?" if near else ""))
            continue
        bad = _entry_problem("param", name, entry, _PARAM_KEYS)
        if bad:
            problems.append(bad)
            continue
        direction = safety_direction(name)
        if direction == "locked":
            problems.append(f"param {name!r} is fixed (data sanity / safety rail definition) and can't be overridden")
            continue
        value = entry.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            problems.append(f"param {name!r}: value must be a finite number")
            continue
        default = base._params[name]
        if name.endswith(_WHOLE_SUFFIXES) and float(default).is_integer() and value != int(value):
            problems.append(f"param {name!r}: value must be a whole number")
            continue
        lo, hi = bounds_for(base, name)
        if not lo <= value <= hi:
            problems.append(f"param {name!r}: value must be between {lo:g} and {hi:g}")
            continue
        if (direction == "lower" and value > default) or (direction == "higher" and value < default):
            problems.append(f"param {name!r} is a safety rail and can only be made stricter "
                            f"({direction} than {default:g})")
            continue
        params[name] = float(value)
        recorded[name] = {"value": float(value), "default": default, "reason": str(entry["reason"]).strip()}
    for a, b in LIN_PAIRS:
        if (a in recorded or b in recorded) and not params[a] < params[b]:
            problems.append(f"params {a!r} ({params[a]:g}) and {b!r} ({params[b]:g}) must stay in order: "
                            f"{a} below {b}")
    for name, entry in sections["tables"].items():
        if name not in tables:
            problems.append(f"unknown table {name!r}")
            continue
        bad = _entry_problem("table", name, entry, _TABLE_KEYS)
        if bad:
            problems.append(bad)
            continue
        try:
            tables[name] = _validate_table(name, {"kind": entry.get("kind"), "points": entry.get("points")})
        except ValueError as exc:
            problems.append(str(exc))
            continue
        recorded[f"table:{name}"] = {"value": entry.get("points"), "default": None,
                                     "reason": str(entry["reason"]).strip()}
    if problems:
        raise ValueError("; ".join(problems))
    return Registry(base._sources, params, tables, base._bounds, recorded)
