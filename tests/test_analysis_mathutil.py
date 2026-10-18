import math

import pytest

from fitbit_air.analysis.mathutil import circular_sd_minutes, lin, phi


def test_lin_interpolates_and_clamps():
    assert lin(6, 4, 0, 7, 100) == pytest.approx(66.667, abs=1e-3)
    assert lin(3, 4, 0, 7, 100) == 0
    assert lin(9, 4, 0, 7, 100) == 100
    assert lin(10, 9, 100, 11, 70) == pytest.approx(85)


def test_lin_rejects_degenerate_segment():
    with pytest.raises(ValueError):
        lin(1, 2, 0, 2, 1)


def test_phi():
    assert phi(0) == pytest.approx(0.5)
    assert phi(1.96) == pytest.approx(0.975, abs=1e-3)
    assert phi(-1.96) == pytest.approx(0.025, abs=1e-3)


def test_circular_sd_handles_midnight_wrap():
    assert circular_sd_minutes([1430, 10]) == pytest.approx(10.0, abs=0.1)
    assert circular_sd_minutes([180, 180, 180]) < 0.01


def test_circular_sd_needs_values():
    with pytest.raises(ValueError):
        circular_sd_minutes([])


def test_circular_sd_opposite_times_is_finite_and_large():
    # Exactly opposite clock times give a zero mean resultant length; must not raise a domain error.
    sd = circular_sd_minutes([29, 749])
    assert math.isfinite(sd) and sd > 120


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_lin_rejects_non_finite_inputs(bad):
    with pytest.raises(ValueError, match="finite"):
        lin(bad, 0, 0, 1, 1)
    with pytest.raises(ValueError, match="finite"):
        lin(0.5, 0, bad, 1, 1)
    with pytest.raises(ValueError, match="finite"):
        lin(0.5, 0, 0, bad, 1)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_circular_sd_rejects_non_finite_inputs(bad):
    with pytest.raises(ValueError, match="finite"):
        circular_sd_minutes([100, bad])
