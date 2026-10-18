"""scripts/export_public.py, run only on temporary source trees (plus one dry run of this checkout into a temp
folder). Failure messages name paths and rules only."""
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
spec = importlib.util.spec_from_file_location("export_public", SCRIPTS / "export_public.py")
ex = importlib.util.module_from_spec(spec)
sys.modules["export_public"] = ex  # dataclasses look their module up here
spec.loader.exec_module(ex)

SHIPPED = {
    "src/fitbit_air/__init__.py", "tests/test_x.py", "tests/fixtures/api/sleep.json", "docs/configuration.md",
    "README.md", "pyproject.toml", "uv.lock", ".gitignore", "LICENSE", "scripts/check_public.py",
    ".github/workflows/ci.yml", "dashboard/README.md",
}
TEMPLATE = "src/fitbit_air/templates/minimal/config/profile.toml"  # a legitimate config/ folder inside src/


def _src(tmp_path):
    s = tmp_path / "src_repo"
    files = {
        "src/fitbit_air/__init__.py": "", "tests/test_x.py": "def test_x():\n    pass\n",
        "tests/fixtures/real/sleep.json": "{}", "tests/fixtures/api/sleep.json": "{}",
        "tests/test_real_fixtures.py": "real", "docs/configuration.md": "ok", "docs/superpowers/specs/x.md": "personal",
        "README.md": "readme", "pyproject.toml": "[project]\nname='x'\n", "uv.lock": "", ".gitignore": "data/\n",
        "data/health.db": "x", "secrets/client_secret.json": "{}", ".superpowers/a": "x", "LICENSE": "Apache",
        "scripts/check_public.py": (SCRIPTS / "check_public.py").read_text(), "scripts/notes.txt": "private",
        ".github/workflows/ci.yml": "name: ci", "dashboard/README.md": "dash", "dashboard/build/x.html": "x",
        "src/fitbit_air/__pycache__/m.cpython-312.pyc": "x", "src/fitbit_air/cache.db-wal": "x",
        "tests/.pytest_cache/v": "x", ".venv/lib/x.py": "x", "src/fitbit_air.egg-info/PKG": "x",
        "tests/fixtures/real/nested/a.json": "{}", "src/.DS_Store": "x", "config/profile.toml": "x",
        "src/.env": "X=1", "tests/data/x.json": "{}", TEMPLATE: "",
    }
    for rel, content in files.items():
        (s / rel).parent.mkdir(parents=True, exist_ok=True)
        (s / rel).write_text(content)
    return s


def _got(dest):
    return {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}


GIT_ISOLATION = ["-c", "init.defaultBranch=main", "-c", "user.name=Test", "-c", "user.email=test" + "@example.com",
                 "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]


def _git(cwd, *args):
    subprocess.run(["git", *GIT_ISOLATION, *args], cwd=cwd, check=True, capture_output=True)


def _commit(src, *paths):
    _git(src, "init", "-q")
    _git(src, "add", "--", *paths)
    _git(src, "commit", "-q", "-m", "init")
    return src


def _repo(tmp_path, *paths):
    """A temp checkout of _src with the given paths committed."""
    src = _src(tmp_path)
    (src / ".gitignore").write_text("data/\nsrc/fitbit_air/local_*.py\n")
    return _commit(src, *paths)


def _siblings(dest):
    return sorted(p.name for p in dest.parent.iterdir() if p.name == dest.name or p.name.startswith(dest.name + "."))


def test_allow_list_only(tmp_path):
    dest = tmp_path / "out"
    copied = ex.export(_src(tmp_path), dest, private=[])
    got = _got(dest)
    assert {p.relative_to(dest).as_posix() for p in copied} == got
    assert got == SHIPPED | {TEMPLATE}
    assert not (dest / ".git").exists() and _siblings(dest) == ["out"]


@pytest.mark.parametrize("rel", [
    "tests/fixtures/real/x.json", "tests/fixtures/real/a/b.json", "tests/test_real_fixtures.py",
    "docs/superpowers/plans/x.md", "src/a/__pycache__/m.pyc", "src/m.pyc", "src/x.db", "tests/h.db-shm",
    ".venv/x.py", "src/.git/x", "tests/.pytest_cache/x", "src/.ruff_cache/x", "src/x.sqlite",
    ".env", "src/.env", ".github/secrets.env", "src/prod.env", "src/.env.local", "src/secrets/x.py",
    "tests/data/x.json", "src/a.pem", "tests/a.key",
    "tests/client_secret_x.json", "src/token.json", "tests/tokens-old.json",
    "src/node_modules/x.js", "src/X.DB", "tests/fixtures/Real/x.json", "src/M.PYC", "Tests/Fixtures/REAL/x.json",
    "docs/SuperPowers/x.md",
])
def test_deny_beats_allow(rel):
    assert ex.selected(rel) is False


@pytest.mark.parametrize("rel", ["src/fitbit_air/a/b.py", "tests/fixtures/api/x.json", "docs/setup.md",
                                 ".github/ISSUE_TEMPLATE/bug_report.md", "scripts/make_fixtures.py", "NOTICE",
                                 "src/fitbit_air/templates/minimal/config/profile.toml", "src/fitbit_air/tokens.py",
                                 "scripts/make_demo_home.py", "scripts/dashboard_preview.py"])
def test_allowed(rel):
    assert ex.selected(rel) is True


@pytest.mark.parametrize("rel", ["docs/a/b.md", "scripts/other.py", "SETUP.md", "data/x.json", "secrets/x.json",
                                 "notes.md", "dashboard/index.html"])
def test_not_allowed(rel):
    assert ex.selected(rel) is False


def test_refuses_non_empty_dest_and_findings(tmp_path):
    src = _src(tmp_path)
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "x").write_text("x")
    with pytest.raises(ex.ExportError, match="not empty"):
        ex.export(src, dest, private=[])
    assert (dest / "x").read_text() == "x"  # nothing was deleted or overwritten
    (src / "docs" / "configuration.md").write_text("by Jane Example")
    with pytest.raises(ex.ExportError, match="1 finding") as err:
        ex.export(src, tmp_path / "out2", private=["Jane Example"])
    assert "docs/configuration.md:1" in str(err.value) and "Jane Example" not in str(err.value)
    blocked = tmp_path / "out2.blocked"
    assert (blocked / "docs" / "configuration.md").exists()  # left in place for inspection
    assert _siblings(tmp_path / "out2") == ["out2.blocked"]


def test_empty_existing_dest_is_used(tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()
    ex.export(_src(tmp_path), dest, private=[])
    assert "README.md" in _got(dest) and _siblings(dest) == ["out"]


def test_refuses_existing_staging_folders(tmp_path):
    src = _src(tmp_path)
    for suffix in (".partial", ".blocked"):
        leftover = tmp_path / f"out{suffix}"
        leftover.mkdir()
        with pytest.raises(ex.ExportError, match=suffix.replace(".", r"\.")):
            ex.export(src, tmp_path / "out", private=[])
        assert not (tmp_path / "out").exists()
        leftover.rename(tmp_path / f"kept{suffix}")


def test_crash_while_copying_leaves_the_partial_folder(tmp_path, monkeypatch):
    src = _src(tmp_path)
    calls = []
    real_copy = shutil.copyfile

    def flaky(a, b, **kw):
        calls.append(a)
        if len(calls) == 3:
            raise OSError("disk full")
        return real_copy(a, b, **kw)

    monkeypatch.setattr(ex.shutil, "copyfile", flaky)
    with pytest.raises(OSError, match="disk full"):
        ex.export(src, tmp_path / "out", private=[])
    assert _siblings(tmp_path / "out") == ["out.partial"] and len(_got(tmp_path / "out.partial")) == 2


def test_refuses_dest_inside_src(tmp_path):
    src = _src(tmp_path)
    with pytest.raises(ex.ExportError, match="inside the source"):
        ex.export(src, src / "tests" / "out", private=[])
    assert not (src / "tests" / "out").exists() and not (src / "tests" / "out.partial").exists()


def test_refuses_missing_source(tmp_path):
    with pytest.raises(ex.ExportError, match="not a folder"):
        ex.export(tmp_path / "nope", tmp_path / "out", private=[])


@pytest.mark.parametrize("kind", ["file", "folder", "dangling"])
def test_refuses_symlinks_before_copying(tmp_path, kind):
    src = _src(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside content")
    link = {"file": src / "src" / "fitbit_air" / "linked.py", "folder": src / "tests" / "linked",
            "dangling": src / "docs" / "gone.md"}[kind]
    target = {"file": outside / "secret.txt", "folder": outside, "dangling": outside / "missing.md"}[kind]
    link.symlink_to(target)
    with pytest.raises(ex.ExportError, match="1 symlink"):
        ex.export(src, tmp_path / "out", private=[])
    assert _siblings(tmp_path / "out") == []


def test_symlinks_outside_shipped_paths_are_ignored(tmp_path):
    for name, make in (("walk", _src), ("git", lambda t: _repo(t, *sorted(SHIPPED), TEMPLATE))):
        src = make(tmp_path / name)
        (src / "venv" / "bin").mkdir(parents=True)
        (src / "venv" / "bin" / "python").symlink_to(sys.executable)  # what `python -m venv venv` makes
        (src / "notes-link").symlink_to(src / "README.md")  # not allow-listed
        (src / "dashboard" / "build-link").symlink_to(src / "README.md")  # only dashboard/README.md ships
        ex.export(src, tmp_path / name / "out", private=[])
        assert _got(tmp_path / name / "out") == SHIPPED | {TEMPLATE}


def test_shared_skip_folders_are_never_shipped():
    tool_dirs = ex.check_public.TOOL_DIRS  # the scanner skips these, so the export must never ship them
    assert ex.PRUNE_ANYWHERE == tool_dirs and ex.PRUNE_TOP == ex.check_public.LOCAL_ENV_DIRS
    assert not [d for d in tool_dirs if ex.selected(f"src/{d}/x.py") or ex.selected(f"tests/a/{d}/x.py")]
    assert {"venv", "env", ".tox", "build", "dist"} <= ex.PRUNE_TOP


def test_local_env_folders_are_pruned_only_at_the_top(tmp_path):
    src = _src(tmp_path)
    (src / "src" / "fitbit_air" / "build").mkdir()
    (src / "src" / "fitbit_air" / "build" / "mod.py").write_text("x = 1\n")  # a package folder named build
    (src / "build").mkdir()
    (src / "build" / "junk.py").write_text("x = 1\n")
    ex.export(src, tmp_path / "out", private=[])
    assert _got(tmp_path / "out") == SHIPPED | {TEMPLATE, "src/fitbit_air/build/mod.py"}


@pytest.mark.parametrize("link,refused", [
    ("src", True), ("src/a/b", True), ("tests/linked", True), (".github", True), ("dashboard", True),
    ("docs/x.md", True), ("scripts", True), ("notes-link", False), ("dashboard/build-link", False),
    ("docs/linkdir", False), ("scripts/other-dir", False), ("venv", False),
])
def test_symlinks_refused_when_an_allow_pattern_reaches_under_them(link, refused):
    assert ex._would_ship(link) is refused


def test_git_export_copies_the_committed_content(tmp_path):
    src = _src(tmp_path)
    (src / "scripts" / "check_public.py").chmod(0o755)
    _commit(src, *sorted(SHIPPED), TEMPLATE)
    (src / "docs" / "configuration.md").write_text("by Jane Example")
    _git(src, "update-index", "--assume-unchanged", "docs/configuration.md")  # hides the edit from git status
    ex.export(src, tmp_path / "out", private=["Jane Example"])
    out = tmp_path / "out"
    assert (out / "docs" / "configuration.md").read_text() == "ok"  # what HEAD holds, not the working tree
    assert oct((out / "scripts" / "check_public.py").stat().st_mode & 0o777) == "0o755"
    assert oct((out / "README.md").stat().st_mode & 0o777) == "0o644"


def test_git_committed_symlinks_are_refused_even_when_the_working_tree_hides_them(tmp_path):
    src = _src(tmp_path)
    link = src / "src" / "fitbit_air" / "link.py"
    link.symlink_to(src / "README.md")
    _commit(src, *sorted(SHIPPED), TEMPLATE, "src/fitbit_air/link.py")
    link.unlink()  # the working tree now holds a plain file, and git is told not to look at it
    link.write_text("x = 1\n")
    _git(src, "update-index", "--skip-worktree", "src/fitbit_air/link.py")
    with pytest.raises(ex.ExportError, match="1 committed symlink"):
        ex.export(src, tmp_path / "out", private=[])
    assert _siblings(tmp_path / "out") == []


def test_git_checkout_uses_tracked_files_only(tmp_path):
    src = _repo(tmp_path, *sorted(SHIPPED), TEMPLATE, "tests/fixtures/real/sleep.json", "scripts/notes.txt")
    ex.export(src, tmp_path / "out", private=[])
    assert _got(tmp_path / "out") == SHIPPED | {TEMPLATE}  # tracked + allowed only
    (src / "src" / "fitbit_air" / "new.py").write_text("x = 1\n")
    with pytest.raises(ex.ExportError, match="untracked") as err:
        ex.export(src, tmp_path / "out2", private=[])
    assert "src/fitbit_air/new.py" in str(err.value) and _siblings(tmp_path / "out2") == []
    ex.export(src, tmp_path / "out3", private=[], skip_untracked=True)
    assert _got(tmp_path / "out3") == SHIPPED | {TEMPLATE}
    (src / "src" / "fitbit_air" / "new.py").rename(src / "src" / "fitbit_air" / "local_x.py")  # git-ignored
    with pytest.raises(ex.ExportError, match="untracked"):
        ex.export(src, tmp_path / "out4", private=[])


def test_git_checkout_refuses_missing_tracked_files(tmp_path):
    src = _repo(tmp_path, *sorted(SHIPPED), TEMPLATE)
    (src / "README.md").rename(tmp_path / "README.moved")
    with pytest.raises(ex.ExportError, match="missing"):
        ex.export(src, tmp_path / "out", private=[])


def test_git_checkout_refuses_uncommitted_changes(tmp_path):
    src = _repo(tmp_path, *sorted(SHIPPED), TEMPLATE)
    (src / "docs" / "configuration.md").write_text("changed")  # unstaged
    with pytest.raises(ex.ExportError, match="1 selected file.* uncommitted") as err:
        ex.export(src, tmp_path / "out", private=[])
    assert "docs/configuration.md" in str(err.value) and _siblings(tmp_path / "out") == []
    _git(src, "add", "docs/configuration.md")  # staged
    with pytest.raises(ex.ExportError, match="uncommitted"):
        ex.export(src, tmp_path / "out", private=[])
    ex.export(src, tmp_path / "out", private=[], allow_dirty=True)  # only the suite's self dry run does this
    _git(src, "commit", "-q", "-m", "docs")
    (src / "scripts" / "notes.txt").write_text("changed")  # tracked but not selected: fine
    ex.export(src, tmp_path / "out2", private=[])


def test_git_subfolder_is_not_a_source(tmp_path):
    src = _repo(tmp_path, *sorted(SHIPPED), TEMPLATE)
    with pytest.raises(ex.ExportError, match="top level"):
        ex.export(src / "src", tmp_path / "out", private=[])
    with pytest.raises(ex.ExportError, match="top level"):
        ex.export(ROOT / "tests", tmp_path / "out", private=[])


def test_pre_copy_errors_withhold_revealing_paths(tmp_path):
    src = _repo(tmp_path, *sorted(SHIPPED), TEMPLATE)
    (src / "docs" / ("notes-me" + "@" + "gmail.com.md")).write_text("x")
    (src / "docs" / "Jane Example.md").write_text("x")
    (src / "docs" / "plain.md").write_text("x")
    with pytest.raises(ex.ExportError, match="3 untracked") as err:
        ex.export(src, tmp_path / "out", private=["Jane Example"])
    msg = str(err.value)
    assert msg.count("<withheld path sha256:") == 2 and "docs/plain.md" in msg
    if "gmail" in msg or "Jane" in msg:
        pytest.fail("a pre-copy error showed a revealing path", pytrace=False)
    (src / "src" / "Jane Example.py").symlink_to(src / "README.md")
    with pytest.raises(ex.ExportError, match="1 symlink") as err:
        ex.export(src, tmp_path / "out", private=["Jane Example"])
    assert "<withheld path sha256:" in str(err.value) and "Jane" not in str(err.value)


def test_candidates_lists_what_would_ship_now(tmp_path):
    src = _repo(tmp_path, *sorted(SHIPPED), TEMPLATE)
    (src / "docs" / "draft.md").write_text("new")  # untracked: skipped
    (src / "README.md").write_text("edited")  # uncommitted: still listed
    assert set(ex.candidates(src)) == SHIPPED | {TEMPLATE}
    assert set(ex.candidates(_src(tmp_path / "plain"))) == SHIPPED | {TEMPLATE}  # no git: a folder walk


def test_candidates_list_the_index_before_the_first_commit(tmp_path):
    src = _src(tmp_path)
    _git(src, "init", "-q")
    _git(src, "add", "--", *sorted(SHIPPED), TEMPLATE)
    assert set(ex.candidates(src)) == SHIPPED | {TEMPLATE}  # no HEAD yet
    with pytest.raises(ex.ExportError, match="commit"):
        ex.export(src, tmp_path / "out", private=[])  # the export itself needs a commit


def test_candidates_include_staged_files(tmp_path):
    src = _repo(tmp_path, *sorted(SHIPPED), TEMPLATE)
    (src / "docs" / "new.md").write_text("draft")
    _git(src, "add", "docs/new.md")
    assert "docs/new.md" in ex.candidates(src)
    with pytest.raises(ex.ExportError, match="uncommitted"):
        ex.export(src, tmp_path / "out", private=[])


def test_generic_finding_blocks_export(tmp_path):
    src = _src(tmp_path)
    (src / "README.md").write_text("see /" + "Users/somebody/x")
    with pytest.raises(ex.ExportError, match="README.md:1 home-path"):
        ex.export(src, tmp_path / "out", private=[])


def test_this_checkout_exports_clean(tmp_path):
    if (ROOT / ".git").exists() and subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=ROOT,
                                                   capture_output=True).returncode != 0:
        pytest.skip("this checkout has no commit yet, and export() copies from HEAD")
    # The suite runs on work in progress: new files are skipped and edits are tolerated (HEAD's content is copied).
    copied = ex.export(ROOT, tmp_path / "out", private=[], skip_untracked=True, allow_dirty=True)
    got = _got(tmp_path / "out")
    assert len(copied) == len(got) > 100 and "scripts/export_public.py" in got
    assert not any(p.startswith(("tests/fixtures/real/", "docs/superpowers/")) for p in got)


def _plist(tmp_path, text="# test list\nJane Example\n"):
    p = tmp_path / "list.txt"
    p.write_text(text)
    return str(p)


def test_main_success_and_failure(tmp_path, capsys):
    src = _src(tmp_path)
    plist = _plist(tmp_path)
    assert ex.main([str(tmp_path / "out"), "--src", str(src), "--private", plist]) == 0
    out = capsys.readouterr().out
    assert "13 file(s)" in out and "clean" in out
    (src / "README.md").write_text("Jane Example")
    assert ex.main([str(tmp_path / "out2"), "--src", str(src), "--private", plist]) == 1
    captured = capsys.readouterr()
    assert "README.md:1" in captured.out + captured.err
    if "Jane Example" in captured.out + captured.err:
        pytest.fail("main echoed a private pattern", pytrace=False)


def test_main_refuses_without_a_private_list(tmp_path, capsys):
    src = _src(tmp_path)
    assert ex.main([str(tmp_path / "a"), "--src", str(src), "--private", str(tmp_path / "missing.txt")]) == 2
    assert ex.main([str(tmp_path / "b"), "--src", str(src), "--private", _plist(tmp_path, "# only comments\n")]) == 2
    err = capsys.readouterr().err
    assert err.count("--no-private-list") == 2 and _siblings(tmp_path / "a") == [] and _siblings(tmp_path / "b") == []
    assert ex.main([str(tmp_path / "c"), "--src", str(src), "--no-private-list"]) == 0


def test_main_reports_os_errors_without_traceback(tmp_path, capsys):
    src = _src(tmp_path)
    (src / "README.md").chmod(0)
    try:
        assert ex.main([str(tmp_path / "out"), "--src", str(src), "--private", _plist(tmp_path)]) == 2
    finally:
        (src / "README.md").chmod(0o600)
    err = capsys.readouterr().err
    assert err.startswith("error:") and "Traceback" not in err
    assert _siblings(tmp_path / "out") == ["out.partial"]  # an unfinished copy is never deleted
