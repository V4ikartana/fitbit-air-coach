import pytest

from fitbit_air.analysis.evidence import SAFETY_DIRECTIONS, apply_overrides, bounds_for, load_registry
from fitbit_air.config import Paths
from fitbit_air.userconfig import registry_for

REG = load_registry()


def test_every_param_has_usable_bounds():
    for name in REG.param_names():
        lo, hi = bounds_for(REG, name)
        assert lo <= REG.param(name) <= hi, name


def test_override_applies_and_is_recorded():
    reg = apply_overrides(REG, {"params": {"steps_benchmark": {"value": 8000, "reason": "doctor's target"}}})
    assert reg.param("steps_benchmark") == 8000
    assert reg.override("steps_benchmark") == {"value": 8000.0, "default": 7000.0, "reason": "doctor's target"}
    assert REG.param("steps_benchmark") == 7000  # the bundled registry is untouched
    assert set(reg.overrides()) == {"steps_benchmark"}


@pytest.mark.parametrize("raw,match", [
    ({"params": {"steps_benchmrk": {"value": 8000, "reason": "x"}}}, "did you mean 'steps_benchmark'"),
    ({"params": {"steps_benchmark": {"value": 8000}}}, "reason"),
    ({"params": {"steps_benchmark": {"value": 10 ** 9, "reason": "x"}}}, "between"),
    ({"params": {"safety_rhr_bpm": {"value": 120, "reason": "x"}}}, "between 80 and 100"),
    ({"params": {"safety_spo2_pct": {"value": 88, "reason": "x"}}}, "between 92 and 96"),
    ({"params": {"forecast_growth": {"value": 1.2, "reason": "x"}}}, "between 1.01 and 1.1"),
    ({"tables": {"nope": {"kind": "linear", "points": [[0, 1], [1, 2]], "reason": "x"}}}, "unknown table"),
])
def test_override_errors(raw, match):
    with pytest.raises(ValueError, match=match):
        apply_overrides(REG, raw)


def test_safety_rails_may_tighten():
    reg = apply_overrides(REG, {"params": {"safety_rhr_bpm": {"value": 95, "reason": "stricter"},
                                           "safety_spo2_pct": {"value": 94, "reason": "stricter"}}})
    assert reg.param("safety_rhr_bpm") == 95 and reg.param("safety_spo2_pct") == 94
    assert set(SAFETY_DIRECTIONS) >= {"safety_rhr_bpm", "safety_spo2_pct", "forecast_growth", "spike_pct",
                                       "weekly_lookback_min_days", "weight_rate_max_pct"}


def test_table_override_validated_like_bundled(tmp_path):
    reg = apply_overrides(REG, {"tables": {"ding2025_steps": {"kind": "loglinear",
                                                              "points": [[2000, 1.0], [7000, 0.55]], "reason": "x"}}})
    assert reg.table("ding2025_steps").value(7000) == pytest.approx(0.55)
    with pytest.raises(ValueError, match="ding2025_steps"):
        apply_overrides(REG, {"tables": {"ding2025_steps": {"kind": "loglinear", "points": [[5, 1]], "reason": "x"}}})


def test_registry_for_reads_file_and_falls_back_on_errors(tmp_path):
    paths = Paths(tmp_path)
    reg, problems = registry_for(paths)
    assert problems == [] and reg.overrides() == {}
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "thresholds.toml").write_text('[params.steps_benchmark]\nvalue = 8000\nreason = "x"\n')
    reg, problems = registry_for(paths)
    assert reg.param("steps_benchmark") == 8000 and problems == []
    (paths.config_dir / "thresholds.toml").write_text('[params.safety_rhr_bpm]\nvalue = 150\nreason = "x"\n')
    reg, problems = registry_for(paths)
    assert reg.param("safety_rhr_bpm") == REG.param("safety_rhr_bpm")  # fell back to defaults
    assert problems and "thresholds.toml" in problems[0].file and "between" in problems[0].message
    (paths.config_dir / "thresholds.toml").write_text("not = [toml")
    _, problems = registry_for(paths)
    assert problems and problems[0].line is not None


@pytest.mark.parametrize("bounds", ['x = [0, 1]', 'a = [2, 1]', 'a = [0]', 'a = [0, "b"]'])
def test_bad_bounds_entries_rejected(bounds):
    with pytest.raises(ValueError, match="bounds"):
        load_registry(f"[params]\na = 0\n[bounds]\n{bounds}\n")


def test_param_with_zero_default_needs_explicit_bounds():
    reg = load_registry("[params]\na = 0\n")
    with pytest.raises(ValueError, match="explicit"):
        bounds_for(reg, "a")
    assert bounds_for(load_registry("[params]\na = 0\n[bounds]\na = [-1, 1]\n"), "a") == (-1.0, 1.0)


# ---- safety rails: locked, tighten-only, bounds, malformed input -------------------------------------------------

import re
from pathlib import Path

LOCKED = ["safety_window_days", "weekly_lookback_min_days", "spike_lookback_days", "weight_rate_window_days",
          "min_n_signals", "floor_rhr", "floor_resp", "floor_temp", "floor_spo2", "floor_hrv",
          "plaus_rhr_max", "plaus_spo2_min", "plaus_vo2_max", "plaus_steps_max"]
LOWER = ["sig_rhr_z", "sig_rhr_bpm", "sig_resp_br", "sig_resp_z", "sig_temp_dev_c", "sig_temp_z", "sig_spo2_drop",
         "cusum_k", "cusum_h", "strain_persist_nights", "weight_min_weighins", "forecast_cutback_every",
         "hrv_band_halfwidth_sd", "safety_rhr_bpm", "safety_rhr_nights", "safety_spo2_nights", "safety_strain_days",
         "forecast_growth",
         "spike_pct", "weight_rate_max_pct"]
HIGHER = ["safety_spo2_pct", "sig_hrv_z", "strain_max_skipped_nights"]


def _ov(name, value):
    return {"params": {name: {"value": value, "reason": "x"}}}


def test_every_plaus_param_is_locked():
    plaus = [n for n in REG.param_names() if n.startswith("plaus_")]
    assert plaus and all(SAFETY_DIRECTIONS.get(n) == "locked" for n in plaus)


@pytest.mark.parametrize("name", LOCKED)
def test_locked_params_are_refused(name):
    assert SAFETY_DIRECTIONS[name] == "locked"
    for value in (REG.param(name) * 0.9, REG.param(name) * 1.1, REG.param(name)):
        with pytest.raises(ValueError, match="is fixed .* and can't be overridden"):
            apply_overrides(REG, _ov(name, value))


def test_plausibility_ranges_cannot_hide_alarms():
    with pytest.raises(ValueError, match="fixed"):
        apply_overrides(REG, _ov("plaus_rhr_max", 100))
    with pytest.raises(ValueError, match="fixed"):
        apply_overrides(REG, _ov("plaus_spo2_min", 93))


@pytest.mark.parametrize("name", LOWER)
def test_lower_params_refuse_loosening_and_accept_tightening(name):
    default = REG.param(name)
    lo, hi = bounds_for(REG, name)
    assert SAFETY_DIRECTIONS[name] == "lower"
    if default < hi:
        value = min(hi, default * 1.2 if default > 0 else hi)
        if name == "forecast_cutback_every" or name.endswith(("_nights", "_days", "_weighins")):
            value = float(round(value))
        if value > default:
            with pytest.raises(ValueError, match="only be made stricter"):
                apply_overrides(REG, _ov(name, value))
    tighter = max(lo, default * 0.9)
    if name in ("forecast_cutback_every", "safety_rhr_nights", "safety_spo2_nights", "safety_strain_days",
                "strain_persist_nights", "weight_min_weighins"):
        tighter = default - 1
    assert lo <= tighter < default
    assert apply_overrides(REG, _ov(name, tighter)).param(name) == tighter


@pytest.mark.parametrize("name", HIGHER)
def test_higher_params_refuse_loosening_and_accept_tightening(name):
    default = REG.param(name)
    lo, hi = bounds_for(REG, name)
    assert SAFETY_DIRECTIONS[name] == "higher"
    loose = default - 1 if name == "strain_max_skipped_nights" else default - 0.5
    with pytest.raises(ValueError, match="between|only be made stricter"):  # bounds may already refuse it
        apply_overrides(REG, _ov(name, loose))
    tight = default + 1 if name == "strain_max_skipped_nights" else min(hi, default + 0.5)
    assert apply_overrides(REG, _ov(name, tight)).param(name) == tight


def test_every_param_read_by_safety_and_signals_has_a_direction():
    src = Path(__file__).parent.parent / "src" / "fitbit_air" / "analysis"
    used = set()
    for f in ("safety.py", "signals.py"):
        used |= set(re.findall(r"param\([\"']([a-z0-9_]+)[\"']\)", (src / f).read_text()))
    used = {n for n in used if not n.startswith("stress_")}  # experimental trend-only index, not a safety rail
    assert used >= {"safety_rhr_bpm", "plaus_rhr_max", "cusum_k"}
    assert sorted(n for n in used if n not in SAFETY_DIRECTIONS) == []


@pytest.mark.parametrize("name,value", [("forecast_growth", 1.0), ("forecast_growth", 1.5),
                                         ("forecast_cutback_every", 1), ("safety_rhr_bpm", 60),
                                         ("safety_spo2_pct", 98), ("spike_pct", 2), ("weight_rate_max_pct", 0.1)])
def test_crash_proof_bounds(name, value):
    with pytest.raises(ValueError, match="between"):
        apply_overrides(REG, _ov(name, value))


def test_explicit_bounds_contain_defaults_and_are_as_specified():
    assert bounds_for(REG, "forecast_growth") == (1.01, 1.10)
    assert bounds_for(REG, "forecast_cutback_every") == (2, 4)
    assert bounds_for(REG, "safety_rhr_bpm") == (80, 100)
    assert bounds_for(REG, "safety_spo2_pct") == (92, 96)
    assert bounds_for(REG, "spike_pct") == (5, 10)
    assert bounds_for(REG, "weight_rate_max_pct") == (0.5, 1.0)


@pytest.mark.parametrize("raw", [{"params": 5}, {"tables": [1]}, {"params": {"steps_benchmark": 5}},
                                 {"tables": {"ding2025_steps": 5}},
                                 {"params": {"steps_benchmark": {"value": 8000, "reason": "x", "extra": 1}}},
                                 {"tables": {"ding2025_steps": {"kind": "linear", "points": [[0, 1], [1, 2]],
                                                                 "reason": "x", "bogus": 1}}}])
def test_malformed_sections_are_value_errors_not_crashes(raw):
    with pytest.raises(ValueError):
        apply_overrides(REG, raw)


@pytest.mark.parametrize("text", ["params = 5\n", "tables = [1]\n", '[params.steps_benchmark]\nvalue = 8000\n'
                                  'reason = "x"\nextra = 1\n'])
def test_registry_for_never_raises_on_malformed_sections(tmp_path, text):
    paths = Paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "thresholds.toml").write_text(text)
    reg, problems = registry_for(paths)
    assert reg.overrides() == {} and len(problems) == 1


def test_registry_for_is_total_even_for_unexpected_errors(tmp_path, monkeypatch):
    import fitbit_air.userconfig as uc
    monkeypatch.setattr(uc, "apply_overrides", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    paths = Paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "thresholds.toml").write_text('[params.steps_benchmark]\nvalue = 8000\nreason = "x"\n')
    reg, problems = uc.registry_for(paths)
    assert reg.overrides() == {} and "boom" in problems[0].message


@pytest.mark.parametrize("text", ["not = [toml", "bogus = 1\n", '[params.safety_rhr_bpm]\nvalue = 150\nreason = "x"\n'])
def test_every_thresholds_problem_says_what_happened(tmp_path, text):
    paths = Paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "thresholds.toml").write_text(text)
    _, problems = registry_for(paths)
    assert problems and all(p.message.endswith(
        " (none of this file's overrides are applied; using built-in defaults until fixed)") for p in problems)


def test_parse_error_line_number_is_the_line_not_a_column(tmp_path):
    paths = Paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "thresholds.toml").write_text("# one\n# two\nnot = [toml\n")
    _, problems = registry_for(paths)
    assert problems[0].line == 3


@pytest.mark.parametrize("name,value", [("safety_strain_days", 4.5), ("hrv_below_days", 2.5),
                                         ("forecast_cutback_every", 3.5), ("weight_min_weighins", 2.5)])
def test_whole_number_params_need_whole_overrides(name, value):
    with pytest.raises(ValueError, match="whole number"):
        apply_overrides(REG, _ov(name, value))
    assert apply_overrides(REG, _ov(name, float(round(value - 0.5)))).param(name) == round(value - 0.5)


def test_overrides_returns_copies():
    reg = apply_overrides(REG, _ov("steps_benchmark", 8000))
    reg.overrides()["steps_benchmark"]["value"] = 1
    reg.override("steps_benchmark")  # a lookup must not expose the stored record either
    assert reg.overrides()["steps_benchmark"]["value"] == 8000.0
