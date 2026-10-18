from datetime import date, datetime, timezone

from analysis_seed import history
from coach_seed import seed_sync
from fitbit_air.analysis import report
from fitbit_air.analysis.runner import run_analysis
from fitbit_air.coach import tools
from fitbit_air.coach.tools import Ctx
from fitbit_air.config import Paths
from fitbit_air.store import Store
from fitbit_air.userconfig import registry_for


def _home(tmp_path, thresholds: str | None):
    paths = Paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    if thresholds is not None:
        (paths.config_dir / "thresholds.toml").write_text(thresholds)
    return paths


def test_run_analysis_uses_overrides_and_records_config_errors(tmp_path):
    paths = _home(tmp_path, '[params.safety_rhr_bpm]\nvalue = 150\nreason = "x"\n')
    store = Store(paths.db)
    try:
        ds = history(store, "2026-09-01", 10)
        run_analysis(store, paths, date.fromisoformat(ds[0]))
        last = store.last_analysis()
        assert "config error" in (last["error"] or "") and "thresholds.toml" in last["error"]
    finally:
        store.close()


def test_status_evidence_and_report_show_overrides(tmp_path):
    paths = _home(tmp_path, '[params.steps_benchmark]\nvalue = 8000\nreason = "doctor"\n')
    store = Store(paths.db)
    try:
        ds = history(store, "2026-09-01", 10)
        seed_sync(store, "2026-09-10T04:00:00Z")
        reg, _ = registry_for(paths)
        ctx = Ctx(store=store, reg=reg, now=datetime(2026, 9, 10, 6, tzinfo=timezone.utc),
                  today=date(2026, 9, 10), paths=paths)
        st = tools.data_status(ctx)
        assert st["overrides"]["steps_benchmark"]["reason"] == "doctor" and st["config_problems"] == []
        ev = tools.get_evidence(ctx, ["override:steps_benchmark"])
        assert ev["sources"]["override:steps_benchmark"]["kind"] == "override"
        assert "steps_benchmark = 8000" in report.render(store, ds[-1], reg=reg)
    finally:
        store.close()


def test_get_evidence_unknown_override_and_plugin_ids_without_plugins(tmp_path):
    paths = _home(tmp_path, None)
    store = Store(paths.db)
    try:
        reg, _ = registry_for(paths)
        ctx = Ctx(store=store, reg=reg, now=datetime(2026, 9, 10, 6, tzinfo=timezone.utc),
                  today=date(2026, 9, 10), paths=paths)
        ev = tools.get_evidence(ctx, ["override:steps_benchmark", "plugin:whatever"])
        assert ev["sources"] == {} and ev["unknown"] == ["override:steps_benchmark", "plugin:whatever"]
        assert ctx.config_problems == [] and ctx.plugins is None
    finally:
        store.close()


def test_data_status_lists_config_problems(tmp_path):
    paths = _home(tmp_path, '[params.safety_rhr_bpm]\nvalue = 150\nreason = "x"\n')
    store = Store(paths.db)
    try:
        reg, problems = registry_for(paths)
        ctx = Ctx(store=store, reg=reg, now=datetime(2026, 9, 10, 6, tzinfo=timezone.utc),
                  today=date(2026, 9, 10), paths=paths, config_problems=[str(p) for p in problems])
        st = tools.data_status(ctx)
        assert st["overrides"] == {} and "thresholds.toml" in st["config_problems"][0]
    finally:
        store.close()


def test_run_analysis_resolves_config_before_recording_a_run(tmp_path, monkeypatch):
    import pytest

    from fitbit_air.analysis import runner

    paths = _home(tmp_path, None)
    store = Store(paths.db)
    try:
        monkeypatch.setattr(runner, "registry_for", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            run_analysis(store, paths, date(2026, 9, 1))
        assert store.last_analysis() is None  # no half-open run left behind
    finally:
        store.close()
