import pytest

from fitbit_air.analysis.baseline import Baseline, compute_baseline, hrv_band_status, rolling_mean, zscore


def series(values: list[float], start_day: int = 1) -> dict[str, float]:
    return {f"2026-10-{start_day + i:02d}": v for i, v in enumerate(values)}


def test_median_and_mad_scale_exclude_today_and_invalid_days():
    s = series([60, 62, 58, 61, 59, 90])  # 2026-10-01..06; the 06th is "today"
    valid = set(s) - {"2026-10-03"}
    b = compute_baseline(s, "2026-10-06", valid, floor=1.0, window_days=60, full_n=28)
    assert b.n == 4  # 01, 02, 04, 05
    assert b.center == pytest.approx(60.5)
    assert b.scale == pytest.approx(1.4826 * 1.0)


def test_scale_floor_and_window():
    s = series([60] * 10)
    b = compute_baseline(s, "2026-10-11", set(s), floor=1.5, window_days=5, full_n=28)
    assert b.n == 5 and b.scale == 1.5


def test_no_history_returns_none():
    assert compute_baseline({}, "2026-10-11", set(), floor=1.5, window_days=60, full_n=28) is None


def test_confidence_grows_with_n_and_completeness():
    s = series([60] * 14)
    b = compute_baseline(s, "2026-10-15", set(s), floor=1.5, window_days=60, full_n=28)
    assert b.confidence == pytest.approx(0.5)  # 14 of 28 days, complete since the first data day
    gappy = {k: v for i, (k, v) in enumerate(s.items()) if i % 2 == 0}  # 7 of 14 days
    b2 = compute_baseline(gappy, "2026-10-15", set(gappy), floor=1.5, window_days=60, full_n=28)
    assert b2.confidence == pytest.approx(7 / 28 * 7 / 14)


def test_zscore():
    assert zscore(63, Baseline(60, 1.5, 10, 1.0)) == pytest.approx(2.0)


def test_rolling_mean_includes_today_and_needs_min_n():
    s = series([1, 2, 3, 4, 5, 6, 7])
    assert rolling_mean(s, "2026-10-07", days=7, min_n=4) == pytest.approx(4.0)
    assert rolling_mean(series([1, 2, 3]), "2026-10-07", days=7, min_n=4) is None


def test_hrv_band_status():
    b = Baseline(center=3.7, scale=0.2, n=20, confidence=0.7)
    assert hrv_band_status(3.55, b, 0.5) == "below"
    assert hrv_band_status(3.70, b, 0.5) == "within"
    assert hrv_band_status(3.85, b, 0.5) == "above"


def test_rolling_mean_valid_days_filter():
    s = series([1, 2, 3, 4, 5, 6, 7])
    valid = set(s) - {"2026-10-07", "2026-10-06"}
    assert rolling_mean(s, "2026-10-07", days=7, min_n=4, valid_days=valid) == pytest.approx(3.0)
    assert rolling_mean(s, "2026-10-07", days=7, min_n=6, valid_days=valid) is None


def test_floor_must_be_positive():
    with pytest.raises(ValueError):
        compute_baseline(series([60, 61]), "2026-10-05", set(series([60, 61])), floor=0, window_days=60, full_n=28)
    with pytest.raises(ValueError):
        compute_baseline(series([60, 61]), "2026-10-05", set(series([60, 61])), floor=-1.0, window_days=60, full_n=28)


def test_first_data_day_has_no_baseline_and_next_day_has_one_point():
    s = series([60])
    assert compute_baseline(s, "2026-10-01", set(s), floor=1.5, window_days=60, full_n=28) is None
    b = compute_baseline(s, "2026-10-02", set(s), floor=1.5, window_days=60, full_n=28)
    assert b.n == 1 and b.scale == 1.5 and b.confidence == pytest.approx(1 / 28, abs=1e-3)
