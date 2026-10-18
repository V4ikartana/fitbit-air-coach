"""scripts/register_claude_desktop.py, loaded from its file. CONFIG always points into tmp_path; nothing is opened."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "register_claude_desktop.py"


@pytest.fixture
def reg(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("register_claude_desktop", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "CONFIG", tmp_path / "claude_desktop_config.json")
    monkeypatch.setattr(mod, "open_claude", lambda: None)
    return mod


def test_entry_has_env_only_when_home_is_set(reg):
    assert "env" not in reg.build_entry({})
    entry = reg.build_entry({"FITBIT_AIR_HOME": "/tmp/example-home"})
    assert entry["env"] == {"FITBIT_AIR_HOME": str(Path("/tmp/example-home").resolve())}
    tilde = reg.build_entry({"FITBIT_AIR_HOME": "~/.fitbit-air/../.fitbit-air"})["env"]["FITBIT_AIR_HOME"]
    assert tilde == str((Path.home() / ".fitbit-air").resolve())  # absolute and normalised
    assert entry["args"][-3:] == ["--frozen", "fitbit-air", "mcp"]


def test_refuses_while_claude_runs(reg, monkeypatch):
    monkeypatch.setattr(reg, "claude_running", lambda: True)
    assert reg.main() == 1
    assert not reg.CONFIG.exists()


def test_writes_env_and_compares_it(reg, monkeypatch, capsys):
    monkeypatch.setattr(reg, "claude_running", lambda: False)
    reg.CONFIG.write_text(json.dumps({"mcpServers": {"fitbit-coach": reg.build_entry({})}}))
    monkeypatch.setenv("FITBIT_AIR_HOME", "/tmp/example-home")
    assert reg.main() == 0
    saved = json.loads(reg.CONFIG.read_text())["mcpServers"]["fitbit-coach"]
    # the env change counted as a change
    assert saved["env"] == {"FITBIT_AIR_HOME": str(Path("/tmp/example-home").resolve())}
    assert list(reg.CONFIG.parent.glob("*.bak-*"))
    assert reg.main() == 0
    assert "already registered" in capsys.readouterr().out
