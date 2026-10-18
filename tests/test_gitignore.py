"""What `home migrate` leaves in an old repo-root home must never be committed."""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["data.migrated-2030-01-15/health.db", "secrets.migrated-2030-01-15/x.json",
                                  "data.migrated-2030-01-15-2/logs/sync.log", "launchd-backup/old.plist",
                                  "config/profile.toml", "plugins/mine.py", "data/health.db",
                                  "secrets/client_secret.json"])
def test_migrated_folders_are_ignored(name):
    if not shutil.which("git"):
        pytest.skip("git is not available")
    probe = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--is-inside-work-tree"], capture_output=True,
                           text=True)
    if probe.returncode != 0:
        pytest.skip("not a git checkout")
    r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", "--no-index", name], capture_output=True)
    assert r.returncode == 0, f"{name} is not ignored"


def test_package_folders_named_config_or_plugins_are_not_ignored():
    if not shutil.which("git"):
        pytest.skip("git is not available")
    r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", "--no-index",
                        "src/fitbit_air/templates/minimal/config/profile.toml"], capture_output=True)
    if r.returncode == 128:
        pytest.skip("not a git checkout")
    assert r.returncode == 1  # only the repo-root home folders are ignored
