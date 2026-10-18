from pathlib import Path

from fitbit_air.config import CODE_ROOT, SCOPES, get_paths


def test_code_root_is_project_dir():
    assert (CODE_ROOT / "pyproject.toml").exists()


def test_paths_follow_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FITBIT_AIR_HOME", str(tmp_path))
    p = get_paths()
    assert p.db == tmp_path / "data" / "health.db"
    assert p.lock_file == tmp_path / "data" / ".sync.lock"
    assert p.client_secret == tmp_path / "secrets" / "client_secret.json"
    assert p.logs_dir == tmp_path / "data" / "logs"


def test_scopes_are_six_readonly_googlehealth_scopes():
    assert len(SCOPES) == 6
    assert all(s.startswith("https://www.googleapis.com/auth/googlehealth.") and s.endswith(".readonly") for s in SCOPES)


def test_home_env_expands_user(monkeypatch):
    monkeypatch.setenv("FITBIT_AIR_HOME", "~/somewhere")
    assert get_paths().root == Path.home() / "somewhere"
    assert isinstance(SCOPES, tuple)
