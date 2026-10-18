import hashlib
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APACHE_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"


def test_license_is_the_official_apache_2_text():
    assert hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest() == APACHE_SHA256


def test_project_files_exist_and_disclaim():
    for name in ("NOTICE", "DISCLAIMER.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md",
                 ".github/workflows/ci.yml", ".github/PULL_REQUEST_TEMPLATE.md",
                 ".github/ISSUE_TEMPLATE/bug_report.md", ".github/ISSUE_TEMPLATE/feature_request.md"):
        assert (ROOT / name).is_file(), name
    d = (ROOT / "DISCLAIMER.md").read_text().lower()
    assert "not medical advice" in d and "not affiliated" in d and "fitbit" in d and "google" in d


def test_security_and_conduct_route_reports_through_github():
    for name in ("SECURITY.md", "CODE_OF_CONDUCT.md"):
        text = (ROOT / name).read_text()
        assert "github.com/v4ikartana/fitbit-air-coach" in text, name
        assert "@" not in text, name  # no e-mail contact is published


def test_pyproject_metadata():
    p = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert p["name"] == "fitbit-air" and p["scripts"]["fitbit-air"] == "fitbit_air.cli:app"
    assert p["license"] == "Apache-2.0" and p["authors"] == [{"name": "Vaikartana"}] and p["readme"] == "README.md"
    assert p["description"].startswith("Unofficial")
    assert "Operating System :: MacOS" in p["classifiers"]
    assert p["urls"]["Repository"] == "https://github.com/v4ikartana/fitbit-air-coach"
    assert p["urls"]["Issues"] == "https://github.com/v4ikartana/fitbit-air-coach/issues"


def test_ci_runs_tests_and_scanner():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "uv run --frozen pytest" in ci and "scripts/check_public.py" in ci and "macos" in ci and "ubuntu" in ci
    assert "uv sync --frozen" in ci and "astral-sh/setup-uv@v5" in ci


def test_disclaimer_covers_medical_affiliation_trademarks_and_warranty():
    text = (ROOT / "DISCLAIMER.md").read_text()
    d = " ".join(text.lower().split())
    assert "not medical advice" in d and "not a medical device" in d
    assert "not affiliated with, endorsed by or sponsored by fitbit, google or anthropic" in d
    assert "trademarks of google llc" in d and "trademark of anthropic" in d
    assert "## No warranty" in text and '"as is"' in d and "without warranties or conditions of any kind" in d
    assert "sections 7 and 8" in d and "](LICENSE)" in text
    assert "incomplete or wrong" in d and "responsible for your credentials and your data" in d
    assert "the package and command are `fitbit-air`" in d


def test_notice_text():
    lines = (ROOT / "NOTICE").read_text().splitlines()
    assert lines[:2] == ["fitbit-air-coach", "Copyright 2026 Vaikartana"]
    rest = " ".join(lines[2:]).lower()
    assert "unofficial" in rest and "not affiliated with or endorsed by fitbit or google" in rest


def test_ci_is_least_privilege_pinned_and_bounded():
    import re
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert re.search(r"^permissions:\n  contents: read\n", ci, re.MULTILINE)
    assert re.search(r'^\s+version: "\d+\.\d+\.\d+"$', ci, re.MULTILINE)  # uv itself is pinned
    assert re.search(r"^\s+timeout-minutes: 15$", ci, re.MULTILINE)
    assert re.search(r"^concurrency:\n  group: .+\n  cancel-in-progress: true$", ci, re.MULTILINE)


def test_issue_templates_keep_health_data_and_secrets_out():
    for name in ("bug_report.md", "feature_request.md"):
        t = (ROOT / ".github/ISSUE_TEMPLATE" / name).read_text()
        for item in ("client_secret.json", "health.db", "snapshot.json", "log files", "tokens"):
            assert item in t, (name, item)
    cfg = (ROOT / ".github/ISSUE_TEMPLATE/config.yml").read_text()
    assert "https://github.com/v4ikartana/fitbit-air-coach/security" in cfg


def test_security_scope_and_conduct_fallback():
    s = (ROOT / "SECURITY.md").read_text()
    assert "the latest commit on the default branch" in s and "`<home>/plugins`" in s
    c = (ROOT / "CODE_OF_CONDUCT.md").read_text()
    assert "https://support.github.com/contact/report-abuse" in c and "maintainer" in c
