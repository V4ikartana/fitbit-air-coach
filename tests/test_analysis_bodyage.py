import functools
import math

import pytest

from fitbit_air.analysis import bodyage
from fitbit_air.analysis.bodyage import component_years
from fitbit_air.analysis.evidence import load_registry

REG = load_registry()
B = 0.087
body_age = functools.partial(bodyage.body_age, estimated=frozenset())  # tests that don't care about provenance


def test_reference_values_give_zero_years():
    for comp, ref in (("vo2", 46.5), ("rhr", 66), ("steps", 6500), ("mvpa", 150), ("strength", 0),
                      ("sleep_min", 450), ("sri", 81), ("bmi", 26.5)):
        assert component_years(comp, ref, REG) == pytest.approx(0.0, abs=1e-9), comp


def test_component_conversions_match_verified_anchors():
    assert component_years("rhr", 76, REG) == pytest.approx(math.log(1.09) / B)
    assert component_years("vo2", 46.5 + 3.5, REG) == pytest.approx(math.log(0.87) / B)
    steps_7k = math.log(0.53 / REG.table("ding2025_steps").value(6500)) / B
    assert component_years("steps", 7000, REG) == pytest.approx(steps_7k)
    assert component_years("sri", 90, REG) == pytest.approx(math.log(0.70 / 0.75) / B)
    bmi_hr = REG.table("gbmc2016_bmi").value
    assert component_years("bmi", 36.6, REG) == pytest.approx(math.log(bmi_hr(36.6) / bmi_hr(26.5)) / B)
    assert component_years("bmi", 23.0, REG) == pytest.approx(math.log(1.0 / bmi_hr(26.5)) / B)
    assert bmi_hr(32.5) == pytest.approx(1.45) and bmi_hr(37.5) == pytest.approx(1.94)


def test_body_age_overlap_clamp_uncertainty_and_levers():
    inputs = {"vo2": 32.0, "rhr": 90.0, "steps": 6000.0, "mvpa": 60.0, "strength": 0.0, "sleep_min": 360.0,
              "sri": 75.0, "bmi": 32.0}
    out = body_age(26, inputs, days_of_data=10, reg=REG)
    assert out["status"] == "provisional"
    fitness = sorted((out["components"]["vo2"]["years"], out["components"]["rhr"]["years"]), key=abs, reverse=True)
    assert out["domains"]["fitness"] == pytest.approx(fitness[0] + 0.5 * fitness[1])  # both harmful: same sign
    raw = 26 + sum(out["domains"].values())
    assert out["body_age"] == pytest.approx(min(raw, 26 + 20))
    assert out["clamped"] == (raw > 46)
    assert out["flags"] == ["age_extrapolated"]  # age 26 is below the studies' typical ages
    assert out["uncertainty"] == pytest.approx((0.25 * sum(abs(v) for v in out["domains"].values()) + 2) * 1.5)
    assert out["levers"]["steps_7000"] < 0 and out["levers"]["rhr_minus_10"] < 0 and out["levers"]["bmi_minus_2"] < 0
    assert "mvpa_150" in out["levers"]


def test_insufficient_and_missing_components():
    few = body_age(26, {"rhr": 90.0, "steps": 6000.0, "strength": 0.0}, days_of_data=40, reg=REG)
    assert few["status"] == "insufficient_data" and few["body_age"] is None
    assert body_age(None, {"rhr": 90.0}, days_of_data=40, reg=REG)["status"] == "insufficient_data"
    ok = body_age(26, {"rhr": 66.0, "steps": 6500.0, "sleep_min": 450.0, "sri": 81.0}, days_of_data=120, reg=REG)
    assert ok["status"] == "ok" and ok["body_age"] == pytest.approx(26.0) and ok["confidence"] == 1.0


BASE = {"rhr": 66.0, "steps": 6500.0, "sleep_min": 450.0, "sri": 81.0, "bmi": 26.5}
KEYS = {"status", "body_age", "reason", "components", "domains", "levers", "uncertainty", "confidence", "clamped",
        "flags", "ignored", "caveat", "display", "levers_unclamped"}


def test_golden_example_with_estimated_vo2():
    inputs = {"vo2": 32.0, "rhr": 90.0, "steps": 9500.0, "mvpa": 60.0, "strength": 0.0, "sleep_min": 370.0,
              "sri": 75.0, "bmi": 36.6}
    out = body_age(26, inputs, days_of_data=40, reg=REG, estimated=frozenset({"vo2"}))
    assert out["status"] == "ok" and set(out) == KEYS
    assert round(out["body_age"], 1) == 36.5
    assert out["display"] == "37 (range 33\u201340)"
    vo2 = out["components"]["vo2"]
    assert vo2["excluded"] == "derived from resting HR" and vo2["years"] is None
    assert "uth2004_vo2max" in out["components"]["vo2"]["evidence"]
    assert "vo2_plus_3_5" not in out["levers"] and "fitness" in out["domains"]
    assert out["domains"]["fitness"] == pytest.approx(out["components"]["rhr"]["years"])  # vo2 not scored
    assert "risk profile" in out["caveat"] and out["caveat"].startswith("Population-statistics estimate")


def test_estimated_vo2_is_not_counted_toward_the_minimum():
    inputs = {"vo2": 32.0, "rhr": 90.0, "sleep_min": 450.0, "sri": 81.0, "strength": 0.0}
    assert body_age(40, inputs, 120, REG)["status"] == "ok"  # vo2, rhr, sleep, sri = 4 counted
    out = body_age(40, inputs, 120, REG, estimated=frozenset({"vo2"}))
    assert out["status"] == "insufficient_data" and "have 3" in out["reason"] and set(out) == KEYS


def test_underweight_bmi_is_older_and_gets_no_bmi_lever():
    assert component_years("bmi", 17.0, REG) > 0
    out = body_age(40, {**BASE, "bmi": 17.0}, 120, REG)
    assert out["components"]["bmi"]["years"] > 0 and "bmi_minus_2" not in out["levers"]


def test_no_cliffs_at_category_edges():
    def at(**kw):
        return body_age(40, {**BASE, **kw}, 120, REG)["body_age"]
    assert abs(at(sleep_min=359.0) - at(sleep_min=360.0)) < 0.2
    assert abs(at(sleep_min=539.0) - at(sleep_min=540.0)) < 0.2
    assert abs(at(mvpa=149.0) - at(mvpa=150.0)) < 0.2
    assert abs(at(mvpa=1499.0) - at(mvpa=1500.0)) < 0.2


def test_overlap_is_continuous_when_harm_and_benefit_swap_rank():
    def at(vo2):
        return body_age(40, {**BASE, "rhr": 76.0, "vo2": vo2}, 120, REG)["body_age"]
    assert abs(at(48.66) - at(48.68)) < 0.05


def test_implausible_and_unknown_inputs():
    bad = {**BASE, "vo2": float("nan"), "mvpa": 99999.0, "strength": -5.0}
    out = body_age(40, bad, 120, REG)
    assert out["status"] == "ok" and set(out["ignored"]) == {"vo2", "mvpa", "strength"}
    assert not {"vo2", "mvpa", "strength"} & set(out["components"])
    low = body_age(40, {**BASE, "rhr": 5.0, "sri": 400.0, "bmi": 3.0, "sleep_min": 5000.0, "steps": -1.0}, 120, REG)
    assert set(low["ignored"]) == {"rhr", "sri", "bmi", "sleep_min", "steps"} and low["status"] == "insufficient_data"
    with pytest.raises(ValueError, match="unknown"):
        body_age(40, {**BASE, "hrv": 50.0}, 120, REG)


def test_clamped_result_still_has_nonzero_levers():
    inputs = {"vo2": 20.0, "rhr": 110.0, "steps": 2000.0, "mvpa": 0.0, "sleep_min": 300.0, "sri": 60.0, "bmi": 45.0}
    out = body_age(40, inputs, 120, REG)
    assert out["clamped"] and out["body_age"] == pytest.approx(60.0) and out["levers_unclamped"] is True
    assert out["levers"]["steps_7000"] < 0 and out["levers"]["bmi_minus_2"] < 0 and out["levers"]["rhr_minus_10"] < 0
    assert body_age(40, BASE, 120, REG)["levers_unclamped"] is False


def test_age_gates_and_extrapolation():
    young = body_age(17, BASE, 120, REG)
    assert young["status"] == "insufficient_data" and "18+" in young["reason"] and set(young) == KEYS
    a26 = body_age(26, {**BASE, "bmi": 30.0}, 120, REG)
    a40 = body_age(40, {**BASE, "bmi": 30.0}, 120, REG)
    assert a26["flags"] == ["age_extrapolated"] and a40["flags"] == []
    assert a26["uncertainty"] == pytest.approx(1.5 * a40["uncertainty"])
    assert "risk profile" in a26["caveat"] and "risk profile" not in a40["caveat"]
    assert body_age(40, BASE, 6, REG)["status"] == "insufficient_data"
    assert body_age(40, BASE, 7, REG)["status"] == "provisional"


def test_rhr_lever_respects_the_floor():
    near = body_age(40, {**BASE, "rhr": 64.0}, 120, REG)["levers"]
    assert "rhr_minus_10" not in near and near["rhr_to_floor"] < 0
    assert not {"rhr_minus_10", "rhr_to_floor"} & set(body_age(40, {**BASE, "rhr": 60.0}, 120, REG)["levers"])


def test_every_evidence_id_exists_in_registry():
    from fitbit_air.analysis.bodyage import ESTIMATED_EVIDENCE, EVIDENCE
    for comp, ids in [*EVIDENCE.items(), *ESTIMATED_EVIDENCE.items()]:
        for source_id in ids:
            assert REG.has(source_id), (comp, source_id)


def test_estimated_must_be_passed_explicitly():
    with pytest.raises(TypeError):
        bodyage.body_age(40, BASE, 120, REG)


def test_display_rounds_half_up_and_caps_range_at_clamp():
    assert bodyage._whole(36.5) == 37 and bodyage._whole(37.5) == 38
    worst = body_age(40, {**BASE, "rhr": 95.0, "sri": 40.0, "bmi": 45.0, "sleep_min": 240.0, "steps": 2000.0}, 120,
                     REG)
    assert worst["clamped"] is True and worst["display"].startswith("60+ (range ")
    assert worst["display"].endswith("–60)")


def test_uncertainty_never_below_the_floor():
    near_reference = {"rhr": 66.0, "steps": 6500.0, "mvpa": 150.0, "sleep_min": 450.0, "sri": 81.0}
    out = body_age(40, near_reference, days_of_data=120, reg=REG)
    assert out["status"] == "ok" and out["uncertainty"] == pytest.approx(2.0)
    young = body_age(26, near_reference, days_of_data=10, reg=REG)  # floor first, then provisional and extrapolation
    assert young["uncertainty"] == pytest.approx((2.0 + 2) * 1.5)
