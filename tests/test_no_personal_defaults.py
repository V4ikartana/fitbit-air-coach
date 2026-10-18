"""Generic guard: tracked files carry no personal paths or e-mail addresses.
The maintainer's personal patterns (name, IDs, dates) live outside the repo in ~/.fitbit-air/private-scan.txt
(see scripts/build_private_scan_list.py) and are checked by scripts/check_public.py at export time. This test also
applies that list when it exists, and never prints a private pattern: a hit names only the file and the pattern's
number."""
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_check():
    spec = importlib.util.spec_from_file_location("check_public", ROOT / "scripts" / "check_public.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_public"] = mod  # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


_CHECK = _load_check()
EXCLUDED_PREFIXES = ("docs/superpowers/", "tests/fixtures/real/")  # internal plans/specs; private real fixtures
EXCLUDED_DIRS = _CHECK.TOOL_DIRS | _CHECK.LOCAL_ENV_DIRS | {"data", "secrets"}  # shared with the scanner
GENERIC = _CHECK.GENERIC_PATTERNS  # home paths and e-mail; shared with the export scanner
PRIVATE_LIST = Path(_CHECK.default_private_list()).expanduser()  # FITBIT_AIR_PRIVATE_SCAN overrides it


def _tracked_names() -> list[str] | None:
    """Tracked and not-ignored files from git, or None when this tree is not a git checkout."""
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=ROOT, capture_output=True, check=True,
                             text=True).stdout.strip()
        if Path(top).resolve() != ROOT:
            return None
        out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                             capture_output=True, check=True).stdout.decode("utf-8")
    except (OSError, subprocess.CalledProcessError):
        return None
    return [n for n in out.split("\0") if n]


def _files() -> list[Path]:
    names = _tracked_names()
    if names is None:  # an exported tree has no .git
        names = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*")
                 if p.is_file() and not EXCLUDED_DIRS & set(p.relative_to(ROOT).parts)]
    return [ROOT / n for n in sorted(names) if not n.startswith(EXCLUDED_PREFIXES) and (ROOT / n).is_file()]


def _read(path: Path) -> str | None:
    data = path.read_bytes()
    if b"\0" in data[:8192]:  # binary
        return None
    return data.decode("utf-8", errors="ignore")


def _hits(patterns: list[str]) -> list[str]:
    out = []
    for f in _files():
        text = _read(f)
        if text is None:
            continue
        text = _CHECK.strip_allowed_emails(text)  # whole addresses only, as the scanner does
        out += [f"{f.relative_to(ROOT)}: {p}" for p in patterns if re.search(p, text)]
    return out


def _matches(sample: str) -> bool:
    return any(re.search(p, sample) for p in GENERIC)


def test_scan_covers_the_tree():
    names = {f.relative_to(ROOT).as_posix() for f in _files()}
    assert len(names) > 80
    assert "pyproject.toml" in names and not any(n.startswith(EXCLUDED_PREFIXES) for n in names)


def test_no_generic_personal_values():
    assert _hits(GENERIC) == []


def test_generic_patterns_catch_paths_and_addresses():
    at = "@"  # samples are built from pieces so this file does not match its own patterns
    for sample in ("/" + "Users/bob/project", "/" + "home/bob/project", "C:" + "\\Users\\bob\\x",
                   "bob" + at + "gmail.com", "bob" + at + "163.com", "bob" + at + "126.com",
                   "x" + at + "example.comcast.net", "x" + at + "example.com.evil.org",
                   "noreply" + at + "anthropic.com"):
        assert _matches(sample), sample


def test_generic_patterns_allow_examples_and_version_pins():
    at = "@"
    for sample in ("x" + at + "example.com", "x" + at + "example.com.", "12345+x" + at + "users.noreply.github.com",
                   "chart.js" + at + "4.4.1", "astral-sh/setup-uv" + at + "v3.1", "actions/checkout" + at + "v4",
                   "pkg" + at + "4.4.1.", "pkg" + at + "v3.1,",
                   "/" + "Users/<you>/", "C:" + "\\Users\\<you>"):
        assert not _matches(sample), sample


def test_no_maintainer_private_values():
    if not PRIVATE_LIST.exists():
        pytest.skip("no private pattern list on this machine")
    patterns = list(enumerate(_CHECK.load_private(PRIVATE_LIST), 1))  # numbered over patterns, as the scanner does
    hits = []
    for f in _files():
        text = _read(f)
        if text is None:
            continue
        rel = f.relative_to(ROOT).as_posix()
        shown = _CHECK.withheld(rel) if any(p.casefold() in rel.casefold() for _, p in patterns) else rel
        hits += [f"{shown}: private pattern #{i}" for i, pattern in patterns
                 if re.search(re.escape(pattern), text, re.IGNORECASE)]
    if hits:
        pytest.fail("\n".join(hits), pytrace=False)
