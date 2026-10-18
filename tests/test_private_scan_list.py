"""scripts/build_private_scan_list.py, run only against temporary homes with fake data and a fake git identity.
Every main() call passes --home, --real, --out and --user-home pointing at temporary paths. Samples are built from
pieces so this file never matches the public scanner's rules. Failure messages name indexes and sources only."""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
spec = importlib.util.spec_from_file_location("bpsl", SCRIPTS / "build_private_scan_list.py")
bpsl = importlib.util.module_from_spec(spec)
sys.modules["bpsl"] = bpsl  # dataclasses look their module up here
spec.loader.exec_module(bpsl)

LOAD = bpsl.check_public.load_private  # the builder reuses the scanner's list reader
NAME = "Jane Example"
EMAIL = "jane" + "@" + "realmail.test"
USER_HOME = "/" + "Users/jane"
CLIENT_ID = "123456789012-" + "abcdefghijklmnopqrstuvwxyz012345" + ".apps.google" + "usercontent.com"
DEVICE = {"name": "users/me/pairedDevices/DEV12345", "deviceId": "devid-998877", "serialNumber": "SN0011223344",
          "macAddress": "AA:BB:CC:DD:EE:F1", "battery": "HIGH", "nested": {"id": "inner-id-4455"},
          "manufacturerId": "google", "platformId": "android", "valid": "fitbit-air", "serial": "abc12",
          "mac": "nodigits"}


def _home(tmp_path, n_points=1, profile_dir="config", devices=True):
    home = tmp_path / "home"
    (home / "data").mkdir(parents=True)
    (home / "config").mkdir()
    con = sqlite3.connect(home / "data" / "health.db")
    con.execute("create table snapshots (kind text, fetched_at text, payload text)")
    con.execute("insert into snapshots values ('profile', 'x', ?)", (json.dumps({"name": "users/123456789"}),))
    if devices:
        con.execute("insert into snapshots values ('paired_devices', 'x', ?)",
                    (json.dumps({"pairedDevices": [DEVICE]}),))
    con.execute("create table raw_points (data_type text, point_key text)")
    for i in range(n_points):
        con.execute("insert into raw_points values ('sleep', ?)",
                    (f"users/123456789/dataTypes/sleep/dataPoints/{987654321 + i}",))
    con.commit()
    con.close()
    (home / profile_dir / "profile.toml").write_text('sex = "male"\nrace_date = "2031-03-04"\n')
    real = tmp_path / "real"
    real.mkdir()
    (real / "sleep.json").write_text(json.dumps({"dataPointName": "users/me/dataTypes/sleep/dataPoints/5550001"}))
    return home, real


def _check_has(pats, needles, text=None):
    missing = [i for i, n in enumerate(needles) if n not in pats or (text is not None and n not in text)]
    if missing:
        pytest.fail(f"missing needles at indexes {missing}", pytrace=False)


def _check_lacks(pats, needles):
    present = [i for i, n in enumerate(needles) if n in pats]
    if present:
        pytest.fail(f"unexpected patterns at indexes {present}", pytrace=False)


def _main(home, real, out, *extra):
    return bpsl.main(["--home", str(home), "--real", str(real), "--out", str(out), "--user-home", USER_HOME, *extra])


def test_builds_patterns_from_sources(tmp_path):
    home, real = _home(tmp_path)
    out = tmp_path / "private-scan.txt"
    pats = bpsl.build(home, real, out, git_identity=(NAME, EMAIL), user_home=USER_HOME)
    text = out.read_text()
    _check_has(pats, (NAME, EMAIL, USER_HOME, "123456789", "987654321", "5550001", "2031-03-04"), text)
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    assert text.startswith("# generated") and "do not commit" in text.splitlines()[0]
    assert len(pats) == len(set(pats))


def test_device_ids_from_paired_devices(tmp_path):
    home, real = _home(tmp_path)
    (real / "dev.json").write_text(json.dumps({"name": "users/me/pairedDevices/FIXDEV778899"}))
    pats = bpsl.build(home, real, tmp_path / "o.txt", git_identity=(NAME, EMAIL), user_home=USER_HOME)
    _check_has(pats, ("DEV12345", "devid-998877", "SN0011223344", "AA:BB:CC:DD:EE:F1", "inner-id-4455",
                      "FIXDEV778899"))
    _check_lacks(pats, ("HIGH", "google", "android", "fitbit-air", "abc12", "nodigits"))


def test_race_date_in_every_common_format(tmp_path):
    home, real = _home(tmp_path)
    pats = bpsl.build(home, real, tmp_path / "o.txt", git_identity=(NAME, EMAIL), user_home=USER_HOME)
    _check_has(pats, ("2031-03-04", "20310304", "04/03/2031", "03/04/2031", "2031/03/04", "4/3/2031", "3/4/2031",
                      "04.03.2031"))


def test_date_forms_skip_duplicates_and_non_dates():
    assert bpsl._date_forms("2031-12-11") == ["2031-12-11", "20311211", "11/12/2031", "12/11/2031", "2031/12/11",
                                              "11.12.2031"]  # unpadded forms equal the padded ones here
    assert bpsl._date_forms("someday") == ["someday"]


def test_defaults_follow_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("FITBIT_AIR_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("FITBIT_AIR_PRIVATE_SCAN", str(tmp_path / "list.txt"))
    assert bpsl.default_paths() == (tmp_path / "h", tmp_path / "list.txt")
    monkeypatch.setenv("FITBIT_AIR_HOME", "")
    monkeypatch.delenv("FITBIT_AIR_PRIVATE_SCAN")
    home, out = bpsl.default_paths()  # paths only: nothing is read
    assert home == Path("~/.fitbit-air").expanduser() and out == home / "private-scan.txt"


def test_home_basename_only_when_longer_than_four(tmp_path):
    home, real = _home(tmp_path)
    pats = bpsl.build(home, real, tmp_path / "a.txt", git_identity=("", ""), user_home=USER_HOME)
    assert "jane" not in pats  # 4 characters: too generic to scan for
    pats = bpsl.build(home, real, tmp_path / "b.txt", git_identity=("", ""), user_home=USER_HOME + "doe")
    assert "janedoe" in pats


def test_short_patterns_skipped_and_profile_fallback(tmp_path):
    home, real = _home(tmp_path, profile_dir="data")
    (real / "x.json").write_text(json.dumps({"a": "users/me/dataTypes/x/dataPoints/42", "b": "users/12345"}))
    pats = bpsl.build(home, real, tmp_path / "o.txt", git_identity=("Al", "x"), user_home=USER_HOME)
    assert "2031-03-04" in pats  # data/profile.toml is still read
    assert all(len(p) >= 3 for p in pats)
    _check_lacks(pats, ("42", "12345", "Al"))  # purely numeric patterns need 6 or more digits


def test_data_point_ids_capped(tmp_path):
    home, real = _home(tmp_path, n_points=250)
    report = bpsl.build_report(home, real, tmp_path / "o.txt", git_identity=("", ""), user_home=USER_HOME)
    db_ids = [p for p in report.patterns if p.isdigit() and int(p) >= 987654321]
    assert len(db_ids) == 200 and report.counts["db point ids"] == 200


def test_missing_sources_are_fine(tmp_path):
    out = tmp_path / "o.txt"
    pats = bpsl.build(tmp_path / "nohome", tmp_path / "noreal", out, git_identity=(NAME, EMAIL), user_home=USER_HOME)
    assert pats == [NAME, EMAIL, USER_HOME] and oct(out.stat().st_mode & 0o777) == "0o600"


def test_counts_per_source(tmp_path):
    home, real = _home(tmp_path)
    report = bpsl.build_report(home, real, tmp_path / "o.txt", git_identity=(NAME, EMAIL), user_home=USER_HOME)
    assert report.counts == {"git": 2, "home": 1, "db user ids": 1, "db point ids": 1, "device ids": 5,
                             "fixture ids": 1, "race date": 8, "client secret": 0, "extras": 0}
    assert report.problems == []


def test_problems_are_named_without_values(tmp_path):
    home, real = _home(tmp_path)
    con = sqlite3.connect(home / "data" / "health.db")
    con.execute("delete from snapshots")
    con.execute("drop table raw_points")
    con.commit()
    con.close()
    (home / "config" / "profile.toml").write_text("race_date = = broken\n")
    report = bpsl.build_report(home, real, tmp_path / "o.txt", git_identity=("", ""), user_home=USER_HOME)
    sources = sorted(p.split(":")[0] for p in report.problems)
    assert sources == ["db", "db", "git", "profile"]
    assert not any(USER_HOME in p or "5550001" in p for p in report.problems)


def test_main_warns_and_exits_non_zero_on_problems(tmp_path, capsys, monkeypatch):
    home, real = _home(tmp_path)
    (home / "config" / "profile.toml").write_text("not toml [\n")
    monkeypatch.setattr(bpsl, "_git_identity", lambda: ("", ""))
    assert _main(home, real, tmp_path / "o.txt") == 1
    err = capsys.readouterr().err
    assert "warning: git:" in err and "warning: profile:" in err


def test_rewrite_tightens_permissions(tmp_path):
    home, real = _home(tmp_path)
    out = tmp_path / "o.txt"
    out.write_text("old")
    out.chmod(0o644)
    bpsl.build(home, real, out, git_identity=(NAME, EMAIL), user_home=USER_HOME)
    assert oct(out.stat().st_mode & 0o777) == "0o600" and "old" not in out.read_text()


def test_extras_survive_a_rebuild(tmp_path):
    home, real = _home(tmp_path)
    out = tmp_path / "list" / "private-scan.txt"
    out.parent.mkdir()
    (out.parent / "private-scan.extra.txt").write_text("# manual\nOld Street 12\n")
    more = tmp_path / "more.txt"
    more.write_text("Second Extra\n")
    report = bpsl.build_report(home, real, out, git_identity=(NAME, EMAIL), user_home=USER_HOME, extra_files=[more])
    _check_has(report.patterns, ("Old Street 12", "Second Extra"), out.read_text())
    assert report.counts["extras"] == 2
    report = bpsl.build_report(home, real, out, git_identity=(NAME, EMAIL), user_home=USER_HOME,
                               extra_files=[tmp_path / "missing.txt"])
    assert [p.split(":")[0] for p in report.problems] == ["extras"]


def test_main_accepts_extra_files(tmp_path, monkeypatch):
    home, real = _home(tmp_path)
    more = tmp_path / "more.txt"
    more.write_text("Manual Pattern\n")
    out = tmp_path / "o.txt"
    monkeypatch.setattr(bpsl, "_git_identity", lambda: (NAME, EMAIL))
    assert _main(home, real, out, "--extra", str(more)) == 0
    _check_has(LOAD(out), ("Manual Pattern",))


def test_public_brand_is_never_listed(tmp_path):
    home, real = _home(tmp_path)
    out = tmp_path / "o.txt"
    (tmp_path / "private-scan.extra.txt").write_text("v4ikartana\nby VAIKARTANA studio\nKeep Me\n")
    pats = bpsl.build(home, real, out, git_identity=("Vaikartana", "x" + "@" + "v4ikartana.dev"),
                      user_home=USER_HOME)
    assert not [p for p in pats if "vaikartana" in p.casefold() or "v4ikartana" in p.casefold()]
    _check_has(pats, ("Keep Me",))


BRAND_IDENTITY = ("Vai" + "kartana", "x" + "@" + "v4i" + "kartana.dev")


def test_brand_git_identity_is_a_problem_without_extras(tmp_path, capsys, monkeypatch):
    home, real = _home(tmp_path)
    report = bpsl.build_report(home, real, tmp_path / "o.txt", git_identity=BRAND_IDENTITY, user_home=USER_HOME)
    assert [p for p in report.problems if p.startswith("git:")] == [
        "git: identity is the public brand; list the real name and e-mail in private-scan.extra.txt"]
    monkeypatch.setattr(bpsl, "_git_identity", lambda: BRAND_IDENTITY)
    assert _main(home, real, tmp_path / "o2.txt") == 1


def test_brand_git_identity_is_fine_with_extras(tmp_path, monkeypatch):
    home, real = _home(tmp_path)
    (tmp_path / "private-scan.extra.txt").write_text("# the real identity\nReal Person Name\n")
    report = bpsl.build_report(home, real, tmp_path / "o.txt", git_identity=BRAND_IDENTITY, user_home=USER_HOME)
    assert report.problems == []
    monkeypatch.setattr(bpsl, "_git_identity", lambda: BRAND_IDENTITY)
    assert _main(home, real, tmp_path / "o2.txt") == 0


def test_client_secret_ids_listed(tmp_path):
    home, real = _home(tmp_path)
    (home / "secrets").mkdir()
    (home / "secrets" / "client_secret.json").write_text(json.dumps(
        {"installed": {"client_id": CLIENT_ID, "project_id": "fake-project-424242", "client_" + "secret": "x"}}))
    report = bpsl.build_report(home, real, tmp_path / "o.txt", git_identity=(NAME, EMAIL), user_home=USER_HOME)
    _check_has(report.patterns, (CLIENT_ID, "fake-project-424242"))
    assert report.counts["client secret"] == 2


def test_refuses_output_inside_the_repo(tmp_path):
    home, real = _home(tmp_path)
    target = ROOT / "private-scan-test-output.txt"
    with pytest.raises(bpsl.BuildError, match="inside the repo"):
        bpsl.build(home, real, target, git_identity=(NAME, EMAIL), user_home=USER_HOME)
    assert not target.exists()


def test_refuses_symlinked_output(tmp_path):
    home, real = _home(tmp_path)
    target = tmp_path / "target.txt"
    target.write_text("untouched")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(OSError):
        bpsl.build(home, real, link, git_identity=(NAME, EMAIL), user_home=USER_HOME)
    assert target.read_text() == "untouched"


def test_creates_private_parent_folders(tmp_path):
    home, real = _home(tmp_path)
    out = tmp_path / "newdir" / "deeper" / "o.txt"
    bpsl.build(home, real, out, git_identity=(NAME, EMAIL), user_home=USER_HOME)
    assert [oct(d.stat().st_mode & 0o777) for d in (out.parent.parent, out.parent)] == ["0o700", "0o700"]


def test_main_reports_only_counts(tmp_path, capsys, monkeypatch):
    home, real = _home(tmp_path)
    out = tmp_path / "list.txt"
    monkeypatch.setattr(bpsl, "_git_identity", lambda: (NAME, EMAIL))
    assert _main(home, real, out) == 0
    captured = capsys.readouterr()
    printed = (captured.out + captured.err).replace(str(out), "<out>")  # the temp path may hold digits
    patterns = LOAD(out)
    assert f"{len(patterns)} pattern(s)" in captured.out and "db point ids 1" in captured.out
    leaked = [i for i, p in enumerate(patterns) if p in printed]
    if leaked:
        pytest.fail(f"main printed private patterns at indexes {leaked}", pytrace=False)


def test_main_reports_os_errors_without_traceback(tmp_path, capsys, monkeypatch):
    home, real = _home(tmp_path)
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    monkeypatch.setattr(bpsl, "_git_identity", lambda: (NAME, EMAIL))
    try:
        assert _main(home, real, locked / "o.txt") == 2
    finally:
        locked.chmod(0o700)
    err = capsys.readouterr().err
    assert err.startswith("error:") and "Traceback" not in err
