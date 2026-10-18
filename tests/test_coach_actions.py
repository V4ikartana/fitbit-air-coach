import logging
import subprocess
from datetime import date, datetime, timezone

import pytest

from fitbit_air.analysis.evidence import load_registry
from fitbit_air.analysis.profile import profile_path
from fitbit_air.coach import actions
from fitbit_air.coach.tools import Ctx
from fitbit_air.config import Paths
from fitbit_air.lock import SyncLock
from fitbit_air.store import Store

NOW = datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def ctx(tmp_path):
    paths = Paths(tmp_path)
    s = Store(paths.db)
    yield Ctx(store=s, reg=load_registry(), now=NOW, today=date(2026, 10, 17), paths=paths)
    s.close()


def test_log_and_get_journal(ctx):
    out = actions.log_journal(ctx, feeling=4, illness=True, notes="cold")
    assert out["entry"]["local_date"] == "2026-10-17" and out["entry"]["illness"] == 1 and "freshness" in out
    actions.log_journal(ctx, date="2026-10-16", alcohol_units=3)
    got = actions.get_journal(ctx, start="2026-10-16")
    assert [e["local_date"] for e in got["entries"]] == ["2026-10-16", "2026-10-17"]
    assert "feeling" in actions.log_journal(ctx, feeling=9)["error"]
    assert "at least one" in actions.log_journal(ctx)["error"]


def test_notes_save_list_update(ctx):
    n = actions.save_coach_note(ctx, kind="target", text="3 runs this week", review_on="2026-10-24")["note"]
    assert actions.get_coach_notes(ctx)["notes"][0]["id"] == n["id"]
    assert actions.get_coach_notes(ctx, due_by="2026-10-20")["notes"] == []
    done = actions.update_coach_note(ctx, id=n["id"], status="done")["note"]
    assert done["status"] == "done"
    assert actions.get_coach_notes(ctx)["notes"] == []
    assert "unknown note" in actions.update_coach_note(ctx, id=999, status="done")["error"]
    assert "kind" in actions.save_coach_note(ctx, kind="goal", text="x")["error"]


def test_get_coach_notes_rejects_unknown_status(ctx):
    out = actions.get_coach_notes(ctx, status="finished")
    assert "status" in out["error"] and "active" in out["error"] and "dropped" in out["error"]
    assert actions.get_coach_notes(ctx, status=None)["notes"] == []


def test_set_profile_validates_saves_and_reanalyses(ctx, monkeypatch):
    calls = []
    monkeypatch.setattr(actions.runner, "run_analysis", lambda store, paths: calls.append(paths) or "8 days: ...")
    out = actions.set_profile(ctx, key="height_m", value="1.75")
    assert out["profile"]["height_m"] == 1.75 and out["analysis"] == "8 days: ..." and calls
    assert "height_m" in actions.set_profile(ctx, key="height_m", value="9")["error"]


def test_set_profile_cannot_enable_plugins(ctx, monkeypatch):
    monkeypatch.setattr(actions.runner, "run_analysis", lambda store, paths: "ok")
    path = profile_path(ctx.paths)
    actions.set_profile(ctx, key="sleep_need_h", value="8")
    before = path.read_text()
    out = actions.set_profile(ctx, key="plugins_enabled", value="true")
    assert "error" in out and "plugins_enabled" in out["error"]
    assert path.read_text() == before and "enabled = false" in before
    assert "plugins_enabled" not in actions.PROFILE_KEYS


def test_set_profile_accepts_numbers_and_lists(ctx, monkeypatch):
    monkeypatch.setattr(actions.runner, "run_analysis", lambda store, paths: "ok")
    assert actions.set_profile(ctx, key="height_m", value=1.75)["profile"]["height_m"] == 1.75
    assert actions.set_profile(ctx, key="hrmax_override", value=190)["profile"]["hrmax_override"] == 190
    out = actions.set_profile(ctx, key="goals", value=["sleep", "weight"])
    assert out["profile"]["goals"] == ["sleep", "weight"]


def test_set_profile_whole_floats_become_ints_and_none_clears(ctx, monkeypatch):
    monkeypatch.setattr(actions.runner, "run_analysis", lambda store, paths: "analysed")
    assert actions.set_profile(ctx, key="hrmax_override", value=190.0)["profile"]["hrmax_override"] == 190
    assert actions.set_profile(ctx, key="sleep_need_h", value=7.5)["profile"]["sleep_need_h"] == 7.5
    assert actions.set_profile(ctx, key="hrmax_override", value="none")["profile"]["hrmax_override"] is None


def test_set_profile_analysis_failure_keeps_the_saved_profile(ctx, monkeypatch, caplog):
    def boom(store, paths):
        raise RuntimeError("db locked")
    monkeypatch.setattr(actions.runner, "run_analysis", boom)
    with caplog.at_level(logging.ERROR):
        out = actions.set_profile(ctx, key="sleep_need_h", value="8")
    assert "error" not in out and out["profile"]["sleep_need_h"] == 8.0
    assert out["analysis"].startswith("failed: RuntimeError('db locked')") and "profile saved" in out["analysis"]
    assert any("db locked" in (r.exc_text or "") for r in caplog.records)


def test_set_profile_while_sync_runs_skips_analysis(ctx, monkeypatch):
    monkeypatch.setattr(actions.runner, "run_analysis", lambda store, paths: pytest.fail("must not run"))
    with SyncLock(ctx.paths.lock_file):
        out = actions.set_profile(ctx, key="sleep_need_h", value="8")
    assert out["profile"]["sleep_need_h"] == 8.0 and "sync is running" in out["analysis"]
    assert "run analyze" in out["analysis"]


def test_refresh_already_running(ctx, monkeypatch):
    monkeypatch.setattr(actions, "subprocess_run", lambda *a, **k: pytest.fail("must not start a sync"))
    with SyncLock(ctx.paths.lock_file):
        assert actions.refresh_data(ctx)["status"] == "already_running"


def test_refresh_success_auth_failure_and_timeout(ctx, monkeypatch):
    seen = {}

    monkeypatch.setenv("FITBIT_AIR_HOME", str(ctx.paths.root))

    def ok(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return subprocess.CompletedProcess(cmd, 0, stdout="steps  ok  10\n", stderr="")
    monkeypatch.setattr(actions, "subprocess_run", ok)
    out = actions.refresh_data(ctx)
    assert out["status"] == "ok" and seen["cmd"][1:] == ["-m", "fitbit_air", "sync"]
    kw = seen["kw"]
    assert kw["capture_output"] is True and kw["timeout"] == actions.SYNC_TIMEOUT_S
    assert kw["env"]["FITBIT_AIR_HOME"] == str(ctx.paths.root)
    assert kw["stdin"] is subprocess.DEVNULL  # under stdio, fd 0 is the MCP request stream

    monkeypatch.setattr(actions, "subprocess_run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 2, stdout="Google login needs attention",
                                                                      stderr=""))
    bad = actions.refresh_data(ctx)
    assert "login" in bad["error"] and "fitbit-air auth" in bad["hint"]

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 900)
    monkeypatch.setattr(actions, "subprocess_run", slow)
    timed_out = actions.refresh_data(ctx)
    assert "15 minutes" in timed_out["error"] and str(ctx.paths.logs_dir / "sync.log") in timed_out["hint"]

    monkeypatch.setattr(actions, "subprocess_run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="sleep failed", stderr=""))
    assert actions.refresh_data(ctx)["status"] == "partial"


def test_set_profile_bad_value_is_a_plain_error_not_internal(ctx, monkeypatch, caplog):
    monkeypatch.setattr(actions.runner, "run_analysis", lambda store, paths: pytest.fail("must not run"))
    with caplog.at_level(logging.ERROR):
        bad = actions.set_profile(ctx, key="height_m", value="9")
        unknown = actions.set_profile(ctx, key="shoe_size", value="42")
        junk = actions.set_profile(ctx, key="sleep_need_h", value="lots")
    assert not caplog.records
    for out in (bad, unknown, junk):
        assert not out["error"].startswith("internal error")
    assert "height_m" in bad["error"] and "profile.toml" in bad["hint"]
    assert "shoe_size" in unknown["error"] and "valid keys" in unknown["hint"] and "race_date" in unknown["hint"]
    assert "sleep_need_h" in junk["error"]
    assert "unknown key" in unknown["error"] and "profile.toml" in junk["hint"]


def test_refresh_race_and_long_output(ctx, monkeypatch):
    monkeypatch.setattr(actions, "subprocess_run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout="Sync already running; exiting.", stderr=""))
    assert actions.refresh_data(ctx)["status"] == "already_running"

    long_out = "x" * 10_000 + "THE END"
    monkeypatch.setattr(actions, "subprocess_run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout=long_out, stderr=""))
    out = actions.refresh_data(ctx)
    assert len(out["output"]) == actions.OUTPUT_TAIL and out["output"].endswith("THE END")


def test_get_journal_accepts_days(ctx):
    actions.log_journal(ctx, date="2026-10-15", feeling=3)
    actions.log_journal(ctx, date="2026-10-10", feeling=4)
    out = actions.get_journal(ctx, days=3)
    assert out["start"] == "2026-10-15" and [e["local_date"] for e in out["entries"]] == ["2026-10-15"]
