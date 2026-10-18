from pathlib import Path

from fitbit_air import config


def test_env_home_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("FITBIT_AIR_HOME", str(tmp_path / "h"))
    p = config.get_paths()
    assert p.root == tmp_path / "h" and p.legacy is False
    assert p.config_dir == tmp_path / "h" / "config" and p.plugins_dir == tmp_path / "h" / "plugins"
    assert p.profile_file == tmp_path / "h" / "config" / "profile.toml"


def test_default_home_when_no_legacy(tmp_path, monkeypatch):
    monkeypatch.delenv("FITBIT_AIR_HOME", raising=False)
    monkeypatch.setattr(config, "DEFAULT_HOME", tmp_path / ".fitbit-air")
    monkeypatch.setattr(config, "CODE_ROOT", tmp_path / "repo")
    assert config.get_paths().root == tmp_path / ".fitbit-air"


def test_legacy_repo_home_kept_until_migrated(tmp_path, monkeypatch):
    monkeypatch.delenv("FITBIT_AIR_HOME", raising=False)
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    (repo / "data" / "health.db").write_bytes(b"")
    new = tmp_path / ".fitbit-air"
    monkeypatch.setattr(config, "DEFAULT_HOME", new)
    monkeypatch.setattr(config, "CODE_ROOT", repo)
    p = config.get_paths()
    assert p.root == repo and p.legacy is True
    new.mkdir()  # a stray or empty folder must not move us off the live data
    assert config.get_paths().root == repo
    (new / "data").mkdir()
    (new / "data" / "health.db").write_bytes(b"")  # the new home wins once it holds data
    assert config.get_paths().root == new and config.get_paths().legacy is False


def test_env_home_equal_to_code_root_is_legacy(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(config, "CODE_ROOT", repo)
    monkeypatch.setenv("FITBIT_AIR_HOME", str(repo / "." / ".." / "repo"))
    assert config.get_paths().legacy is True
    monkeypatch.setenv("FITBIT_AIR_HOME", str(tmp_path / "elsewhere"))
    assert config.get_paths().legacy is False


def test_legacy_stays_authoritative_until_migrate_has_switched_launchd(tmp_path, monkeypatch):
    import json

    monkeypatch.delenv("FITBIT_AIR_HOME", raising=False)
    repo, new = tmp_path / "repo", tmp_path / ".fitbit-air"
    for root in (repo, new):
        (root / "data").mkdir(parents=True)
        (root / "data" / "health.db").write_bytes(b"")
    monkeypatch.setattr(config, "DEFAULT_HOME", new)
    monkeypatch.setattr(config, "CODE_ROOT", repo)
    state = new / config.MIGRATE_STATE_FILE
    for content, legacy in [({"source": str(repo), "copied": True}, True), ("not json", True),
                            ({"scheduled": True}, False), ({"done": True}, False)]:
        state.write_text(content if isinstance(content, str) else json.dumps(content))
        p = config.get_paths()
        assert (p.root, p.legacy) == ((repo, True) if legacy else (new, False)), content
