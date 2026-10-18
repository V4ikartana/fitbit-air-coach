import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import keyring.errors
import pytest
from oauthlib.oauth2.rfc6749.errors import OAuth2Error
from typer.testing import CliRunner

from fitbit_air import cli
from fitbit_air.config import get_paths
from fitbit_air.errors import NormalizeError, ReauthRequired
from fitbit_air.lock import SyncLock
from fitbit_air.registry import REGISTRY
from fitbit_air.store import Store, SyncState

runner = CliRunner()


@pytest.fixture(autouse=True)
def clean_logging():
    old_umask = os.umask(0o022)
    os.umask(old_umask)
    levels = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    yield
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "_fitbit_air", False)]:
        root.removeHandler(h)
        h.close()
    for n, level in levels.items():
        logging.getLogger(n).setLevel(level)
    os.umask(old_umask)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FITBIT_AIR_HOME", str(tmp_path))
    monkeypatch.setattr(cli.auth_mod, "auth_state", lambda: "not logged in")
    return tmp_path


class ExpiredClient:
    def __init__(self):
        self.closed = False

    def iter_points(self, dt, start, end):
        raise ReauthRequired("invalid_grant")

    def get_profile(self):
        return {}

    def list_paired_devices(self):
        return {}

    def close(self):
        self.closed = True


def test_status_on_fresh_install():
    r = runner.invoke(cli.app, ["status"])
    assert r.exit_code == 0, r.output
    assert "Auth: not logged in" in r.output
    assert "Last run: never" in r.output
    assert "never synced" in r.output


def test_status_shows_pending_rebuilds_and_deep_scan():
    name = REGISTRY[0].name
    store = Store(get_paths().db)
    store.save_state(SyncState(name, status="ok", deep_scan_utc=datetime(2026, 10, 1, 6, 0, tzinfo=UTC)))
    store.add_pending(name, ["2026-09-01", "2026-09-02"], "boom")
    store.close()
    r = runner.invoke(cli.app, ["status"])
    assert r.exit_code == 0, r.output
    assert "pending rebuild: 2 dates" in r.output
    assert "2026-10-01" in r.output and "deep re-scan" in r.output


def test_sync_reauth_notifies_and_exits_2(monkeypatch):
    sent = []
    client = ExpiredClient()
    monkeypatch.setattr(cli, "_make_client", lambda: client)
    monkeypatch.setattr(cli, "notify", lambda title, message: sent.append(message))
    r = runner.invoke(cli.app, ["sync"])
    assert r.exit_code == 2
    assert sent and "fitbit-air auth" in sent[0]
    assert "needs attention" in sent[0]
    assert "reauth required: invalid_grant" in r.output  # the underlying reason is shown
    assert client.closed


def test_sync_rejects_unknown_type():
    r = runner.invoke(cli.app, ["sync", "--types", "nope"])
    assert r.exit_code == 1 and "nope" in r.output
    assert "Valid: " in r.output and REGISTRY[0].name in r.output


def test_sync_exits_quietly_when_already_running():
    lock = SyncLock(get_paths().lock_file)
    assert lock.acquire()
    try:
        r = runner.invoke(cli.app, ["sync"])
        assert r.exit_code == 0 and "already running" in r.output
    finally:
        lock.release()


def test_logging_quiets_http_libraries():
    cli._setup_logging(get_paths())
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_console_progress_handler_only_on_a_tty(monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)
    cli._setup_logging(get_paths())
    root = logging.getLogger()
    assert not any(type(h) is logging.StreamHandler and getattr(h, "_fitbit_air", False) for h in root.handlers)
    for h in [h for h in root.handlers if getattr(h, "_fitbit_air", False)]:
        root.removeHandler(h)
        h.close()
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)
    cli._setup_logging(get_paths())
    streams = [h for h in root.handlers if type(h) is logging.StreamHandler and getattr(h, "_fitbit_air", False)]
    assert len(streams) == 1 and streams[0].level == logging.INFO


def test_auth_reports_errors_without_traceback(monkeypatch):
    def boom(paths):
        raise FileNotFoundError("OAuth client JSON not found at /x")

    monkeypatch.setattr(cli.auth_mod, "login", boom)
    r = runner.invoke(cli.app, ["auth"])
    assert r.exit_code == 1 and "Error: OAuth client JSON not found" in r.output


def test_schedule_install_and_uninstall_report_errors(monkeypatch):
    def fail(*a, **kw):
        raise subprocess.CalledProcessError(5, ["launchctl"])

    monkeypatch.setattr(cli.schedule_mod, "install", fail)
    monkeypatch.setattr(cli.schedule_mod, "uninstall", lambda **k: (_ for _ in ()).throw(RuntimeError("nope")))
    r = runner.invoke(cli.app, ["schedule", "install"])
    assert r.exit_code == 1 and "Error: " in r.output and "launchctl" in r.output
    r = runner.invoke(cli.app, ["schedule", "uninstall"])
    assert r.exit_code == 1 and "Error: nope" in r.output


def test_sync_help_documents_exit_codes():
    r = runner.invoke(cli.app, ["sync", "--help"])
    assert "login needs attention" in r.output and "2" in r.output


def test_auth_warns_about_missing_scopes(monkeypatch):
    monkeypatch.setattr(cli.auth_mod, "login", lambda paths: SimpleNamespace())
    monkeypatch.setattr(cli.auth_mod, "missing_scopes", lambda creds: ["https://x/sleep.readonly"])
    r = runner.invoke(cli.app, ["auth"])
    assert r.exit_code == 0, r.output
    assert "sleep.readonly" in r.output and "fitbit-air auth" in r.output


def test_auth_without_missing_scopes_has_no_warning(monkeypatch):
    monkeypatch.setattr(cli.auth_mod, "login", lambda paths: SimpleNamespace())
    monkeypatch.setattr(cli.auth_mod, "missing_scopes", lambda creds: [])
    r = runner.invoke(cli.app, ["auth"])
    assert r.exit_code == 0, r.output
    assert "Logged in" in r.output and "WARNING" not in r.output


def test_rebuild_on_empty_db():
    r = runner.invoke(cli.app, ["rebuild"])
    assert r.exit_code == 0, r.output


def test_rebuild_clears_pending_dates():
    name = REGISTRY[0].name
    store = Store(get_paths().db)
    store.add_pending(name, ["2026-09-01"], "boom")
    store.close()
    r = runner.invoke(cli.app, ["rebuild"])
    assert r.exit_code == 0, r.output
    store = Store(get_paths().db)
    try:
        assert store.pending_dates(name) == []
    finally:
        store.close()


def test_rebuild_normalize_error_exits_1_and_keeps_pending(monkeypatch):
    first, second = REGISTRY[0].name, REGISTRY[1].name
    store = Store(get_paths().db)
    store.add_pending(first, ["2026-09-01"], "boom")
    store.add_pending(second, ["2026-09-02"], "boom")
    store.close()

    def fake_rebuild(store, dt, dates):
        if dt.name == first:
            raise NormalizeError(f"{dt.name}: all 3 points failed to normalize")
        return 0

    monkeypatch.setattr(cli.normalize, "rebuild", fake_rebuild)
    r = runner.invoke(cli.app, ["rebuild"])
    assert r.exit_code == 1
    assert "failed to normalize" in r.output
    store = Store(get_paths().db)
    try:
        assert store.pending_dates(first) == ["2026-09-01"]  # still pending
        assert store.pending_dates(second) == []  # later types still ran
    finally:
        store.close()


def test_rebuild_refuses_while_a_sync_is_running():
    lock = SyncLock(get_paths().lock_file)
    assert lock.acquire()
    try:
        r = runner.invoke(cli.app, ["rebuild"])
        assert r.exit_code == 1 and "A sync is running; try again shortly." in r.output
    finally:
        lock.release()


def test_rebuild_rejects_bad_since():
    r = runner.invoke(cli.app, ["rebuild", "--since", "yesterday"])
    assert r.exit_code != 0 and "YYYY-MM-DD" in r.output


def test_rebuild_since_only_clears_pending_in_range():
    name = REGISTRY[0].name
    store = Store(get_paths().db)
    store.add_pending(name, ["2026-01-01", "2026-09-01"], "boom")
    store.close()
    r = runner.invoke(cli.app, ["rebuild", "--since", "2026-06-01"])
    assert r.exit_code == 0, r.output
    store = Store(get_paths().db)
    try:
        assert store.pending_dates(name) == ["2026-01-01"]
    finally:
        store.close()


def test_rebuild_does_not_clear_dates_that_became_pending_during_the_rebuild(monkeypatch):
    name = REGISTRY[0].name
    store = Store(get_paths().db)
    store.add_pending(name, ["2026-09-01"], "boom")
    store.close()

    def fake_rebuild(store, dt, dates):
        if dt.name == name:
            store.add_pending(name, ["2026-09-05"], "new")
        return 0

    monkeypatch.setattr(cli.normalize, "rebuild", fake_rebuild)
    assert runner.invoke(cli.app, ["rebuild"]).exit_code == 0
    store = Store(get_paths().db)
    try:
        assert store.pending_dates(name) == ["2026-09-05"]
    finally:
        store.close()


def test_rebuild_refreshes_failed_sync_status_when_pending_is_cleared():
    name, other = REGISTRY[0].name, REGISTRY[1].name
    store = Store(get_paths().db)
    store.save_state(SyncState(name, status="failed", last_error="boom"))
    store.add_pending(name, ["2026-09-01"], "boom")
    store.save_state(SyncState(other, status="failed", last_error="something else"))
    store.close()
    r = runner.invoke(cli.app, ["rebuild"])
    assert r.exit_code == 0, r.output
    store = Store(get_paths().db)
    try:
        st = store.get_state(name)
        assert st.status == "ok" and st.last_error is None
        assert store.get_state(other).status == "failed"  # unrelated failures are left alone
    finally:
        store.close()


def test_cli_sets_a_private_umask():
    runner.invoke(cli.app, ["status"])
    current = os.umask(0o022)
    assert current == 0o077


def test_rebuild_removes_clean_rows_of_orphaned_pending_dates(monkeypatch):
    name = REGISTRY[0].name
    store = Store(get_paths().db)
    store.add_pending(name, ["2026-09-01"], "boom")  # no raw rows for this date
    store.close()
    seen = []
    monkeypatch.setattr(cli.normalize, "rebuild", lambda store, dt, dates: seen.append((dt.name, list(dates))) or 0)
    assert runner.invoke(cli.app, ["rebuild"]).exit_code == 0
    assert (name, ["2026-09-01"]) in seen


@pytest.mark.parametrize("exc", [
    OAuth2Error("access_denied"),
    keyring.errors.KeyringError("locked"),
])
def test_auth_reports_oauth_and_keyring_errors(monkeypatch, exc):
    def boom(paths):
        raise exc

    monkeypatch.setattr(cli.auth_mod, "login", boom)
    r = runner.invoke(cli.app, ["auth"])
    assert r.exit_code == 1 and "Error: " in r.output
    db = get_paths().db
    if db.exists():
        store = Store(db)
        try:
            assert store.get_meta("auth_completed_at") is None
        finally:
            store.close()


def test_auth_records_completion_time(monkeypatch):
    monkeypatch.setattr(cli.auth_mod, "login", lambda paths: SimpleNamespace())
    monkeypatch.setattr(cli.auth_mod, "missing_scopes", lambda creds: [])
    assert runner.invoke(cli.app, ["auth"]).exit_code == 0
    store = Store(get_paths().db)
    try:
        assert store.get_meta("auth_completed_at").endswith("Z")
    finally:
        store.close()


def test_mcp_command_runs_the_server(monkeypatch):
    called = []
    monkeypatch.setattr("fitbit_air.coach.server.main", lambda: called.append(True))
    assert runner.invoke(cli.app, ["mcp"]).exit_code == 0 and called


def test_snapshot_command_writes_file_and_reports():
    store = Store(get_paths().db)
    store.close()
    result = runner.invoke(cli.app, ["snapshot"])
    out = get_paths().data_dir / "dashboard" / "snapshot.json"
    assert result.exit_code == 0 and out.exists() and str(out) in result.output


def test_snapshot_command_without_database_fails_cleanly():
    result = runner.invoke(cli.app, ["snapshot"])
    assert result.exit_code == 1 and "fitbit-air sync" in result.output


def test_snapshot_command_out_option_and_os_errors(tmp_path, monkeypatch):
    Store(get_paths().db).close()
    target = tmp_path / "elsewhere" / "snap.json"
    result = runner.invoke(cli.app, ["snapshot", "--out", str(target)])
    assert result.exit_code == 0 and target.exists() and str(target) in result.output

    blocked = tmp_path / "file"
    blocked.write_text("x")  # a file where the directory should be: mkdir fails with an OSError
    result = runner.invoke(cli.app, ["snapshot", "--out", str(blocked / "snap.json")])
    assert result.exit_code == 1 and "Error:" in result.output and "Traceback" not in result.output
