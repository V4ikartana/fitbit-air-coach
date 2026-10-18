import pytest

from fitbit_air.analysis.evidence import load_registry
from fitbit_air.analysis.recovery import band, recovery_score

REG = load_registry()


def test_neutral_day():
    value, comps, weights = recovery_score(hrv_roll_z=0, hrv_night_z=0, rhr_z=0, sleep_last=100, debt_h=0, reg=REG)
    assert comps == {"hrv": pytest.approx(50), "rhr": pytest.approx(50), "sleep": pytest.approx(100)}
    assert value == pytest.approx(62.5)
    assert weights == {"hrv": 0.5, "rhr": 0.25, "sleep": 0.25}


def test_direction_of_each_marker():
    good, _, _ = recovery_score(2, 2, -2, 100, 0, REG)
    bad, comps, _ = recovery_score(-2, -2, 2, 30, 7, REG)
    assert good > 67 and bad < 33
    assert comps["rhr"] == pytest.approx(100 * 0.02275, abs=0.01)


def test_missing_night_values_fall_back():
    value, comps, _ = recovery_score(0, None, 0, None, 0, REG)
    assert comps["hrv"] == pytest.approx(50) and comps["sleep"] == pytest.approx(100)


def test_sleep_component_uses_whichever_input_is_present():
    _, only_last, _ = recovery_score(0, 0, 0, 80, None, REG)
    assert only_last["sleep"] == pytest.approx(80)
    _, only_debt, _ = recovery_score(0, 0, 0, None, 3.5, REG)
    assert only_debt["sleep"] == pytest.approx(50)
    _, both, _ = recovery_score(0, 0, 0, 80, 3.5, REG)
    assert both["sleep"] == pytest.approx(0.6 * 80 + 0.4 * 50)


def test_no_sleep_inputs_renormalises_weights():
    value, comps, weights = recovery_score(1, None, -0.5, None, None, REG)
    assert comps["sleep"] is None
    assert value == pytest.approx((0.5 * comps["hrv"] + 0.25 * comps["rhr"]) / 0.75)
    assert weights == {"hrv": pytest.approx(2 / 3), "rhr": pytest.approx(1 / 3), "sleep": None}
    assert value == pytest.approx(weights["hrv"] * comps["hrv"] + weights["rhr"] * comps["rhr"])


def test_bands():
    assert band(67, REG) == "green" and band(66.9, REG) == "yellow" and band(33, REG) == "red"
    assert band(33.5, REG) == "yellow"
