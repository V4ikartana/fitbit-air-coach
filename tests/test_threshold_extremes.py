"""No threshold override that validation accepts may break the analysis or silence a safety rail.

Every overridable param is pushed to the ends of its bounds (in its allowed direction only) and the analysis is run
on a history that trips the resting-HR safety rail."""
import math
from datetime import date, datetime, timedelta

import pytest

from analysis_seed import history
from fitbit_air.analysis.daily import analyze
from fitbit_air.analysis.evidence import _WHOLE_SUFFIXES, apply_overrides, bounds_for, load_registry, safety_direction
from fitbit_air.store import Store
from profiles import RUNNER

REG = load_registry()


def _cases():
    out = []
    for name in REG.param_names():
        direction = safety_direction(name)
        if direction == "locked":
            continue
        lo, hi = bounds_for(REG, name)
        default = REG.param(name)
        if name.endswith(_WHOLE_SUFFIXES) and float(default).is_integer():
            lo, hi = math.ceil(lo), math.floor(hi)
        values = {"lower": [lo], "higher": [hi], None: [lo, hi]}[direction]
        out += [(name, v) for v in values if v != default]
    return out


CASES = _cases()


def test_every_overridable_param_is_covered():
    names = {n for n, _ in CASES}
    overridable = {n for n in REG.param_names() if safety_direction(n) != "locked"}
    assert len(overridable) > 100
    assert overridable - names <= {n for n in overridable if bounds_for(REG, n)[0] == bounds_for(REG, n)[1]}


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("extremes") / "health.db")
    ds = history(store, "2026-08-31", 10, rhr=105)  # persistently high resting HR: the safety rail must fire
    for i, d in enumerate(ds[::3]):
        start = datetime.fromisoformat(f"{d}T01:00:00+00:00")
        store.replace_clean("exercise", [d], {"exercise_sessions": [
            {"id": f"run-{d}", "exercise_type": "RUNNING", "display_name": "Run",
             "start_utc": start.isoformat().replace("+00:00", "Z"),
             "end_utc": (start + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"), "local_date": d,
             "duration_min": 30.0, "active_min": 30.0, "avg_hr": 150.0, "calories_kcal": 300.0, "steps": 4000.0,
             "distance_m": (3 + i * 0.3) * 1000, "azm": 20.0}]})
    store.save_snapshot("profile", {"age": 40})
    yield store, ds
    store.close()


@pytest.mark.parametrize(("name", "value"), CASES, ids=[f"{n}={v:g}" for n, v in CASES])
def test_extreme_override_keeps_analysis_and_safety(seeded, name, value):
    store, ds = seeded
    try:
        reg = apply_overrides(REG, {"params": {name: {"value": value, "reason": "test"}}})
    except ValueError:
        return  # refused by validation (e.g. a pair check): `config check` reports it, the analysis never sees it
    end = date.fromisoformat(ds[-1])
    summary = analyze(store, RUNNER, reg, end, end)
    assert summary.error is None, summary.error
    safety = [i for i in store.analysis_for_day(ds[-1])["insights"] if i["kind"] == "safety"]
    assert safety, f"{name}={value} silenced the safety rail"


def test_lin_pairs_are_in_order_by_default_and_cover_every_lin_endpoint_param():
    from fitbit_air.analysis.evidence import LIN_PAIRS

    for a, b in LIN_PAIRS:
        assert REG.param(a) < REG.param(b), (a, b)
    named = {p for pair in LIN_PAIRS for p in pair}
    assert {"timing_full_min", "timing_zero_min", "eff_zero", "eff_full", "sri_zero", "sri_full",
            "sleep_dur_zero_h", "sleep_dur_ok_min_h", "sleep_dur_ok_max_h", "sleep_dur_long_h"} <= named


@pytest.mark.parametrize(("a", "b"), [("timing_full_min", "timing_zero_min"), ("eff_zero", "eff_full"),
                                      ("sri_zero", "sri_full"), ("sleep_dur_zero_h", "sleep_dur_ok_min_h"),
                                      ("sleep_dur_ok_max_h", "sleep_dur_long_h")])
def test_pair_ends_must_differ_and_keep_their_order(a, b):
    va, vb = REG.param(a), REG.param(b)
    for override in ({a: vb}, {b: va}, {a: vb, b: va}):  # equal ends, and swapped ends
        raw = {"params": {k: {"value": v, "reason": "test"} for k, v in override.items()}}
        with pytest.raises(ValueError, match="must stay in order"):
            apply_overrides(REG, raw)
    mid = (va + vb) / 2
    apply_overrides(REG, {"params": {a: {"value": mid, "reason": "test"}}})  # still in order: fine


def test_planner_rails_taper_only_grows_and_first_run_is_bounded():
    def ok(name, value):
        try:
            apply_overrides(REG, {"params": {name: {"value": value, "reason": "test"}}})
            return True
        except ValueError:
            return False
    assert safety_direction("forecast_taper_weeks") == "higher"
    assert not ok("forecast_taper_weeks", 1) and ok("forecast_taper_weeks", 3)  # more taper is more conservative
    assert bounds_for(REG, "forecast_first_run_km") == (1.0, 5.0)
    assert ok("forecast_first_run_km", 1.0) and not ok("forecast_first_run_km", 6.0)
