"""scripts/check_public.py: generic rules, the private list, and the CLI. Samples are built from pieces so this file
never matches the scanner's own rules (the export scans it too). Failure messages name paths and rules only."""
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
AT = "@"
HOME_MAC = "/" + "Users/somebody"
HOME_LINUX = "/" + "home/somebody"
HOME_WIN = "C:" + "\\Users\\somebody"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


check = _load("check_public")


def _tree(tmp_path, files: dict[str, str | bytes]):
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
    return tmp_path


def _fail_if_shown(findings, secret):
    shown = [i for i, f in enumerate(findings) if secret.casefold() in (f.path + f.detail).casefold()]
    if shown:
        pytest.fail(f"findings at indexes {shown} show private text", pytrace=False)


def test_clean_tree_passes(tmp_path):
    text = "Use ~/.fitbit-air. Mail: someone" + AT + "example.com. Pin chart.js" + AT + "4.4.1, setup-uv" + AT + "v3.1"
    assert check.scan(_tree(tmp_path, {"README.md": text, "empty.py": ""}), []) == []


@pytest.mark.parametrize("rel,content,rule", [
    ("a.py", "p = '" + HOME_MAC + "/x'", "home-path"),
    ("a2.py", "p = '" + HOME_LINUX + "/x'", "home-path"),
    ("a3.txt", "p = " + HOME_WIN + "\\x", "home-path"),
    ("a4.json", '{"p": "\\/' + 'Users\\/somebody\\/x"}', "home-path"),
    ("a5.txt", "url?path=%2F" + "Users%2Fsomebody%2Fx", "home-path"),
    ("a6.txt", "url?path=%2f" + "users%2fsomebody", "home-path"),
    ("a7.json", '{"p": "C:' + '\\\\Users\\\\somebody"}', "home-path"),
    ("b.md", "write to me at real.person" + AT + "gmail.com", "email"),
    ("b2.md", "write to me at someone" + AT + "163.com", "email"),
    ("c.json", '{"client_' + 'secret": "' + "GOCSP" + "X-abc" + '"}', "secret"),
    ("d.txt", "token " + "ya" + "29.a0AfH6SMBxyz", "secret"),
    ("e.txt", "-----BEGIN " + "PRIVATE KEY-----", "secret"),
    ("e2.txt", "-----BEGIN PGP " + "PRIVATE KEY BLOCK-----", "secret"),
    ("f.txt", "AI" + "zaSyA1234567890abcdefghijklmnopqrstuv", "secret"),
    ("g.txt", "refresh " + "1/" + "/0gAbCdEfGhIjKlMnOpQrStUv", "secret"),
    ("h.txt", "id 123456789012-" + "abcdefghijklmnopqrstuvwxyz012345" + ".apps.google" + "usercontent.com", "secret"),
])
def test_generic_rules(tmp_path, rel, content, rule):
    findings = check.scan(_tree(tmp_path, {rel: content}), [])
    assert [f.rule for f in findings] == [rule] and findings[0].path == rel and findings[0].line == 1
    assert content not in findings[0].detail


def test_sqlite_and_db_files_blocked(tmp_path):
    db = tmp_path / "x.db"
    sqlite3.connect(db).execute("create table t(a)")
    (tmp_path / "renamed.bin").write_bytes(db.read_bytes())
    rules = sorted((f.path, f.rule) for f in check.scan(tmp_path, []))
    assert ("renamed.bin", "sqlite") in rules and ("x.db", "sqlite") in rules


def test_private_patterns_and_line_numbers(tmp_path):
    t = _tree(tmp_path, {"docs/x.md": "line one\nby Jane Example here\n"})
    f = check.scan(t, ["Jane Example"])
    assert len(f) == 1 and f[0].line == 2 and f[0].rule == "private" and "Jane Example" not in f[0].detail
    assert f[0].detail == "private pattern #1"


def test_private_patterns_ignore_case_and_scan_binaries(tmp_path):
    t = _tree(tmp_path, {"a.md": "by JANE EXAMPLE", "b.bin": b"\xff\xfe\x00Jane Example\x00"})
    f = check.scan(t, ["unused", "Jane Example"])
    assert sorted((x.path, x.rule, x.detail) for x in f) == [
        ("a.md", "private", "private pattern #2"), ("b.bin", "binary", "not UTF-8 text"),
        ("b.bin", "private", "private pattern #2")]


def test_private_patterns_across_line_breaks(tmp_path):
    t = _tree(tmp_path, {"a.md": "by Jane\n   Example here", "b.md": "Jane Example\nJane\nExample"})
    f = check.scan(t, ["Jane Example"])
    assert sorted((x.path, x.line, x.detail) for x in f) == [
        ("a.md", 0, "private pattern #1 (across lines)"), ("b.md", 1, "private pattern #1")]


def test_private_patterns_across_hyphen_wraps_and_markup(tmp_path):
    name = "Jane " + "Example"
    t = _tree(tmp_path, {"a.md": "by Jane Exam" + "-\n   ple here", "b.md": "by **Jane** _" + "Example_",
                         "c.md": "`Jane` " + "Example", "d.md": "id fake" + "-\nproject-42"})
    f = check.scan(t, [name, "fake-project-42"])
    assert sorted((x.path, x.line, x.detail) for x in f) == [
        ("a.md", 0, "private pattern #1 (across lines)"), ("b.md", 0, "private pattern #1 (across lines)"),
        ("c.md", 0, "private pattern #1 (across lines)"), ("d.md", 0, "private pattern #2 (across lines)")]


def test_binary_files_flagged_unless_allowed(tmp_path, monkeypatch):
    t = _tree(tmp_path, {"a.bin": b"\x00\x01\x02", "b.bin": b"\xff\xfe", "empty.txt": b"", "ok.txt": "café"})
    assert sorted((f.path, f.rule) for f in check.scan(t, [])) == [("a.bin", "binary"), ("b.bin", "binary")]
    monkeypatch.setattr(check, "BINARY_OK", {"a.bin"})
    assert [(f.path, f.rule) for f in check.scan(t, [])] == [("b.bin", "binary")]


def test_file_names_matching_private_patterns_are_withheld(tmp_path):
    t = _tree(tmp_path, {"notes/Jane Example.md": "by Jane Example", "ok.md": "fine"})
    f = check.scan(t, ["Jane Example"])
    assert sorted((x.line, x.rule, x.detail) for x in f) == [
        (0, "private", "file name matches private pattern #1"), (1, "private", "private pattern #1")]
    assert all(x.path.startswith("<withheld path sha256:") and len(x.path) == len("<withheld path sha256:>") + 12
               for x in f)
    _fail_if_shown(f, "Jane Example")


def test_file_names_matching_generic_rules_are_withheld(tmp_path):
    t = _tree(tmp_path, {"mail-me" + AT + "gmail.com.txt": "fine"})
    f = check.scan(t, [])
    assert [(x.rule, x.detail) for x in f] == [("email", "file name matches email")]
    assert f[0].path.startswith("<withheld path sha256:")


def test_allowed_emails_are_shared_and_honoured(tmp_path, monkeypatch):
    assert check.ALLOWED_EMAILS == []
    allowed = "maintainer" + AT + "project.dev"
    monkeypatch.setattr(check, "ALLOWED_EMAILS", [allowed])
    t = _tree(tmp_path, {"a.md": "write to " + allowed, "b.md": allowed + " or other" + AT + "gmail.com"})
    assert [(f.path, f.rule) for f in check.scan(t, [])] == [("b.md", "email")]


def test_allowed_emails_strip_whole_addresses_only(tmp_path, monkeypatch):
    allowed = "a" + AT + "x.org"
    monkeypatch.setattr(check, "ALLOWED_EMAILS", [allowed])
    t = _tree(tmp_path, {"a.md": "mail " + allowed + ", thanks", "b.md": "mail b" + allowed,
                         "c.md": "mail " + allowed + ".evil.com", "d.md": "<" + allowed + ">"})
    assert sorted(f.path for f in check.scan(t, [])) == ["b.md", "c.md"]
    assert check.strip_allowed_emails("x b" + allowed) == "x b" + allowed


def test_skips_tool_directories(tmp_path):
    t = _tree(tmp_path, {".git/config": HOME_MAC, ".venv/x.py": HOME_MAC, "__pycache__/x.pyc": HOME_MAC})
    assert check.scan(t, []) == []


def test_load_private_list_skips_comments_and_blanks(tmp_path):
    p = tmp_path / "list.txt"
    p.write_text("# generated\n\n  Jane Example  \n  # indented comment\nab\n")
    assert check.load_private(p) == ["Jane Example", "ab"]
    assert check.load_private(tmp_path / "missing.txt") == []


def test_no_stale_module_default():
    assert not hasattr(check, "DEFAULT_PRIVATE_LIST")  # read at call time via default_private_list()


def test_empty_env_override_means_default(monkeypatch):
    monkeypatch.setenv("FITBIT_AIR_PRIVATE_SCAN", "")
    assert check.default_private_list() == "~/.fitbit-air/private-scan.txt"
    monkeypatch.setenv("FITBIT_AIR_PRIVATE_SCAN", "/tmp/elsewhere.txt")
    assert check.default_private_list() == "/tmp/elsewhere.txt"


def test_cli_exit_codes(tmp_path, capsys):
    tree = tmp_path / "tree"
    _tree(tree, {"ok.md": "fine"})
    missing = str(tmp_path / "no-list.txt")  # never the real list
    assert check.main([str(tree), "--private", missing]) == 0
    assert check.main([str(tree), "--private", "/dev/null"]) == 0  # how CI runs it
    (tree / "bad.md").write_text(HOME_MAC + "/")
    assert check.main([str(tree), "--private", missing]) == 1
    assert "bad.md:1" in capsys.readouterr().out


def test_cli_reports_os_errors_without_traceback(tmp_path, capsys):
    missing = str(tmp_path / "no-list.txt")
    assert check.main([str(tmp_path / "nope"), "--private", missing]) == 2
    tree = _tree(tmp_path / "tree", {"locked.md": "x"})
    (tree / "locked.md").chmod(0)
    try:
        assert check.main([str(tree), "--private", missing]) == 2
    finally:
        (tree / "locked.md").chmod(0o600)
    err = capsys.readouterr().err
    assert err.count("error:") == 2 and "Traceback" not in err


def test_cli_never_prints_private_text(tmp_path, capsys):
    tree = _tree(tmp_path / "tree", {"x.md": "by Jane Example", "Jane Example.txt": "x"})
    plist = tmp_path / "list.txt"
    plist.write_text("Jane Example\n")
    assert check.main([str(tree), "--private", str(plist)]) == 1
    captured = capsys.readouterr()
    assert "x.md:1" in captured.out and "private pattern #1" in captured.out
    if "Jane Example" in captured.out + captured.err:
        pytest.fail("the CLI echoed a private pattern", pytrace=False)


def test_repo_scripts_and_tests_are_clean():
    root = Path(__file__).resolve().parents[1]
    findings = [f for d in ("scripts", "tests") for f in check.scan(root / d, [])
                if not f.path.startswith("fixtures/real")]
    if findings:
        pytest.fail("\n".join(f"{f.path}:{f.line}: {f.rule}" for f in findings), pytrace=False)
