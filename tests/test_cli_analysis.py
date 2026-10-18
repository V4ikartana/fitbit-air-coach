import logging
import os
from datetime import date, timedelta

import pytest
from typer.testing import CliRunner

from analysis_seed import history
from fitbit_air import cli
from fitbit_air.analysis import daily as analysis_daily
from fitbit_air.analysis.profile import profile_path
from fitbit_air.config import get_paths
from fitbit_air.store import Store

runner = CliRunner()


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FITBIT_AIR_HOME", str(tmp_path))
    monkeypatch.setattr(cli.auth_mod, "auth_state", lambda: "not logged in")
    old_umask = os.umask(0o022)
    os.umask(old_umask)
    yield tmp_path
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "_fitbit_air", False)]:
        root.removeHandler(h)
        h.close()
    os.umask(old_umask)


def seeded(n=30):
    start = (date.today() - timedelta(days=n - 1)).isoformat()
    store = Store(get_paths().db)
    try:
        return history(store, start, n)
    finally:
        store.close()


class EmptyClient:
    def iter_points(self, dt, start, end):
        yield []

    def get_profile(self):
        return {"age": 26}

    def list_paired_devices(self):
        return {}

    def close(self):
        pass


def test_analyze_then_report():
    ds = seeded()
    r = runner.invoke(cli.app, ["analyze", "--since", ds[-3]])
    assert r.exit_code == 0, r.output
    assert "Analysed" in r.output
    r = runner.invoke(cli.app, ["report", "--date", ds[-1]])
    assert r.exit_code == 0 and "Recovery" in r.output and "Not medical advice" in r.output


def test_analyze_rejects_bad_date():
    assert runner.invoke(cli.app, ["analyze", "--since", "yesterday"]).exit_code != 0


def test_report_rejects_bad_date():
    assert runner.invoke(cli.app, ["report", "--date", "nope"]).exit_code != 0


def test_analyze_with_a_bad_profile_file_still_analyses_and_reports_it(home):
    seeded()
    path = profile_path(get_paths())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('sex = "robot"\n')
    r = runner.invoke(cli.app, ["analyze"])
    assert r.exit_code == 0 and "Analysed" in r.output  # the safety rails don't depend on the profile
    assert "Config problem:" in r.output and "sex" in r.output and "Traceback" not in r.output


def test_profile_show_and_set():
    r = runner.invoke(cli.app, ["profile", "set", "sleep_need_h", "8"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli.app, ["profile", "show"])
    assert "sleep_need_h = 8.0" in r.output
    assert runner.invoke(cli.app, ["profile", "set", "sex", "robot"]).exit_code == 1


def test_sync_runs_analysis_and_status_shows_it(monkeypatch):
    monkeypatch.setattr(cli, "_make_client", lambda: EmptyClient())
    r = runner.invoke(cli.app, ["sync"])
    assert r.exit_code == 0, r.output
    status = runner.invoke(cli.app, ["status"])
    assert "Analysis: last" in status.output
    assert "ERROR" not in status.output and "did not finish" not in status.output


def test_sync_survives_analysis_failure(monkeypatch):
    monkeypatch.setattr(cli, "_make_client", lambda: EmptyClient())

    def boom(*a, **k):
        raise RuntimeError("analysis exploded")

    monkeypatch.setattr(analysis_daily, "analyze", boom)
    r = runner.invoke(cli.app, ["sync"])
    assert r.exit_code == 0, r.output
    status = runner.invoke(cli.app, ["status"])
    assert "analysis exploded" in status.output


def test_sync_survives_bad_profile_file(monkeypatch):
    monkeypatch.setattr(cli, "_make_client", lambda: EmptyClient())
    path = profile_path(get_paths())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('sex = "robot"\n')
    r = runner.invoke(cli.app, ["sync"])
    assert r.exit_code == 0, r.output
    assert "ERROR" in runner.invoke(cli.app, ["status"]).output


def test_status_analysis_never():
    r = runner.invoke(cli.app, ["status"])
    assert "Analysis: never" in r.output


def test_status_analysis_unfinished_run():
    store = Store(get_paths().db)
    store.start_analysis_run("2026-08-01")
    store.close()
    r = runner.invoke(cli.app, ["status"])
    assert r.exit_code == 0, r.output
    assert "did not finish (interrupted or still running)" in r.output
    assert "Analysis: last" in r.output


def test_phase_b_failure_is_shown_in_status(monkeypatch):
    ds = seeded()

    def boom(*a, **k):
        raise RuntimeError("phase b exploded")

    monkeypatch.setattr(analysis_daily.fitness, "compute", boom)
    r = runner.invoke(cli.app, ["analyze", "--since", ds[-1]])
    assert r.exit_code == 0, r.output
    status = runner.invoke(cli.app, ["status"]).output
    assert "phase B failed: RuntimeError('phase b exploded')" in status


def test_analyze_unexpected_error_exits_1_without_traceback(monkeypatch):
    seeded()

    def boom(*a, **k):
        raise RuntimeError("kaput")

    monkeypatch.setattr(analysis_daily, "analyze", boom)
    r = runner.invoke(cli.app, ["analyze"])
    assert r.exit_code == 1 and "Error: RuntimeError('kaput')" in r.output
    assert "Traceback" not in r.output


def test_analyze_and_report_print_config_problems_after_their_output(home):
    seeded()
    (home / "config").mkdir(exist_ok=True)
    (home / "config" / "thresholds.toml").write_text("[params.steps_benchmark]\nvalue = 8000\n")  # no reason
    (home / "config" / "profile.toml").write_text('sex = "robot"\n')
    r = runner.invoke(cli.app, ["analyze"])
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[0].startswith("Analysed")
    problems = [ln for ln in lines if ln.startswith("Config problem: ")]
    assert len(problems) == 2
    assert any("thresholds.toml" in p and "reason" in p for p in problems)
    assert any("profile.toml" in p and "sex must be one of" in p for p in problems)
    r = runner.invoke(cli.app, ["report"])
    assert r.exit_code == 0, r.output
    tail = r.output.rstrip().splitlines()[-2:]
    assert all(t.startswith("Config problem: ") for t in tail)
