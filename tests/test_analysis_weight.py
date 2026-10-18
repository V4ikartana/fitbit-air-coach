import pytest

from fitbit_air.analysis.evidence import load_registry
from fitbit_air.analysis.weight import bmi, daily_weighins, trend_series, weight_status

REG = load_registry()


def test_daily_weighins_keep_last_of_day():
    rows = [("2026-10-14", "2026-10-14T01:00:00Z", "weight_kg", 80.5),
            ("2026-10-14", "2026-10-14T13:00:00Z", "weight_kg", 80.0),
            ("2026-10-12", "2026-10-12T01:00:00Z", "weight_kg", 81.0)]
    assert daily_weighins(rows) == [("2026-10-12", 81.0), ("2026-10-14", 80.0)]


def test_ewma_trend_with_ten_day_half_life():
    trend = trend_series([("2026-10-01", 100.0), ("2026-10-11", 90.0)], REG)
    assert trend["2026-10-01"] == 100.0
    assert trend["2026-10-11"] == pytest.approx(95.0)  # alpha = 1 - 0.5**(10/10) = 0.5


def test_weight_status_rate_is_regression_slope_and_requirements():
    weighins = [(f"2026-10-{d:02d}", 100.0 - 0.1 * (d - 1)) for d in range(1, 15)]
    st = weight_status(weighins, "2026-10-14", REG)
    assert st["trend_kg"] < 100 and st["rate_kg_week"] == pytest.approx(-0.7)
    assert st["rate_pct_week"] == pytest.approx(st["rate_kg_week"] / st["trend_kg"] * 100)
    assert st["n_recent"] == 14  # inclusive 14-day window
    sparse = weight_status([("2026-10-14", 80.0)], "2026-10-14", REG)
    assert sparse["trend_kg"] == 80.0 and sparse["rate_kg_week"] is None and sparse["n_recent"] == 1
    assert weight_status([("2026-01-01", 80.0)], "2026-10-14", REG) is None  # older than 90 days


def test_weight_rate_no_false_loss_from_stale_anchor():
    weighins = [("2026-08-15", 84.0), ("2026-10-02", 80.0), ("2026-10-08", 80.0), ("2026-10-14", 80.0)]
    st = weight_status(weighins, "2026-10-14", REG)
    assert st["n_recent"] == 3
    assert st["rate_kg_week"] == pytest.approx(0.0, abs=1e-9) and st["rate_pct_week"] == pytest.approx(0.0, abs=1e-9)


def test_weight_rate_true_loss():
    weighins = [(f"2026-10-{d:02d}", 100.0 - 0.2 * (d - 5)) for d in (5, 7, 9, 12, 14)]  # 0.2 kg/day
    st = weight_status(weighins, "2026-10-14", REG)
    assert st["rate_kg_week"] == pytest.approx(-1.4)


def test_weight_rate_needs_a_week_span():
    weighins = [("2026-10-09", 100.0), ("2026-10-11", 99.5), ("2026-10-14", 99.0)]  # 5 days apart
    st = weight_status(weighins, "2026-10-14", REG)
    assert st["rate_kg_week"] is None and st["rate_pct_week"] is None and st["n_recent"] == 3
    edge = [("2026-10-08", 100.0), ("2026-10-11", 99.5), ("2026-10-15", 99.0)]
    assert weight_status(edge, "2026-10-15", REG)["rate_kg_week"] is not None  # exactly 7 days


def test_bmi():
    assert bmi(80.0, 1.75) == pytest.approx(26.12, abs=0.01)  # 80 / 1.75**2 = 26.1224
    assert bmi(80.0, None) is None
