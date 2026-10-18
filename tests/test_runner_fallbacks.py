"""A config problem never stops the analysis: thresholds that crash it, or a broken profile, fall back to defaults."""
from datetime import date

from analysis_seed import history
from fitbit_air.analysis import daily, runner
from fitbit_air.config import Paths
from fitbit_air.store import Store


def _home(tmp_path):
    paths = Paths(tmp_path)
    store = Store(paths.db)
    history(store, "2026-09-20", 20, rhr=105)
    return paths, store


def _safety(store):
    return [i for i in store.analysis_for_day("2026-10-09")["insights"] if i["kind"] == "safety"]  # last seeded day


def test_analysis_that_fails_with_overrides_reruns_with_built_in_defaults(tmp_path, monkeypatch):
    paths, store = _home(tmp_path)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "thresholds.toml").write_text('[params.steps_benchmark]\nvalue = 8000\nreason = "mine"\n')
    real = daily.analyze
    seen = []

    def analyze(store_, profile, reg, since, today, plugins=None):
        seen.append(bool(reg.overrides()))
        if reg.overrides():
            raise ValueError("x0 and x1 must differ")
        return real(store_, profile, reg, since, today, plugins=plugins)
    monkeypatch.setattr(runner.daily, "analyze", analyze)
    runner.run_analysis(store, paths, date(2026, 10, 9))
    assert seen == [True, False]
    run = store.last_analysis()
    assert run["finished_at"] and "analysis failed with your threshold overrides" in run["error"]
    assert "x0 and x1 must differ" in run["error"] and "used built-in defaults" in run["error"]
    assert _safety(store)  # the built-in rows, safety included, were written
    store.close()


def test_a_failure_without_overrides_still_raises_and_is_recorded(tmp_path, monkeypatch):
    import pytest

    paths, store = _home(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(runner.daily, "analyze", boom)
    with pytest.raises(RuntimeError):
        runner.run_analysis(store, paths, date(2026, 10, 9))
    assert "bug" in store.last_analysis()["error"]
    store.close()


def test_broken_profile_falls_back_to_defaults_and_is_recorded(tmp_path):
    paths, store = _home(tmp_path)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.profile_file.write_text('sex = "robot"\n')
    runner.run_analysis(store, paths, date(2026, 10, 9))
    run = store.last_analysis()
    assert "config error" in run["error"] and "profile.toml" in run["error"] and "sex must be one of" in run["error"]
    assert _safety(store)
    paths.profile_file.write_text('sex = "male"\nrace_date = [\n')  # a syntax error too
    runner.run_analysis(store, paths, date(2026, 10, 9))
    assert "config error" in store.last_analysis()["error"] and _safety(store)
    store.close()


def test_a_past_today_scores_that_day_and_nothing_after(tmp_path):
    paths, store = _home(tmp_path)
    runner.run_analysis(store, paths, date(2026, 9, 20), today=date(2026, 10, 1))
    days = {r[0] for r in store.conn.execute("SELECT DISTINCT local_date FROM scores_daily")}
    assert "2026-10-01" in days and max(days) == "2026-10-01"
    store.close()
