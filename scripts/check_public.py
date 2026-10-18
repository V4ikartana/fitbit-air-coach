"""Scan a tree for anything that must not be published.

Usage: uv run python scripts/check_public.py <tree> [--private ~/.fitbit-air/private-scan.txt]

Exit 1 on findings, 2 on an error. A finding names only the file, the line, the rule and a fixed description, never
the matched text, so the output can be shared. Private-list matches name the pattern's number in the list, counted
over patterns from 1 ("private pattern #3"); tests/test_no_personal_defaults.py numbers them the same way.
- Generic rules: home paths (macOS, Linux, Windows; also JSON-escaped and URL-encoded), e-mail addresses (not
  example.com, GitHub noreply addresses, ALLOWED_EMAILS or version pins such as name@v3.1), credential-like strings
  and Google OAuth client ids, SQLite databases (by magic bytes or file suffix), and binary files (NUL bytes or not
  UTF-8) unless listed in BINARY_OK.
- File names and paths are checked too. A path that matches a rule or a private pattern is shown only as
  "<withheld path sha256:...>" (the first 12 hex digits of the repo-relative path's sha256).
- The private list (one literal per line, "#" comments) is built outside the repo by build_private_scan_list.py and
  is matched case-insensitively, per line and across line breaks (whitespace runs collapsed, line-end hyphens
  joined, markdown emphasis * _ ` removed).
  FITBIT_AIR_PRIVATE_SCAN overrides its default path; an empty value means the default.
- A missing private list only warns here (CI runs this with --private /dev/null); export_public.py refuses instead.
- Every file is scanned, binaries included (decoded leniently), except tool directories in TOOL_DIRS (which the
  export never ships).
The regex sources below are assembled so this file never matches its own rules (the export scans it too).
tests/test_no_personal_defaults.py imports GENERIC_PATTERNS and ALLOWED_EMAILS, so the two cannot drift."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

HOME_PATTERNS = [
    r"\\?/Users\\?/[A-Za-z][\w.-]+",  # macOS home, also JSON-escaped
    r"\\?/home\\?/[a-z][\w.-]+",  # Linux home, also JSON-escaped
    r"(?i:%2f(?:users|home)%2f)[A-Za-z][\w.-]+",  # URL-encoded home
    r"\b[A-Za-z]:\\{1,2}Users\\{1,2}[A-Za-z][\w.-]*",  # Windows home, also with escaped backslashes
]
# an e-mail address: not a version pin (name@v3.1, name@4.4.1) and not an example/noreply address
EMAIL_PATTERN = (r"[\w.+-]+@(?![vV]?\d+(?:\.\d+)*(?![\w-]|\.\w))(?!(?:example\.com|users\.noreply\.github\.com)"
                 r"(?![\w-]|\.\w))[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
GENERIC_PATTERNS = [*HOME_PATTERNS, EMAIL_PATTERN]
ALLOWED_EMAILS: list[str] = []  # explicit public addresses that may appear; the regexes stay strict
SECRET_PATTERNS = [
    r'"client_secret"\s*:',  # a pasted OAuth client file
    "GOCSP" "X-",  # Google OAuth client secret prefix
    r"ya29\.[\w-]+",  # Google access token
    r"1//[\w-]{20,}",  # Google refresh token
    r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----",
    r"AIza[\w-]{35}",  # Google API key
    r"\d{6,}-[a-z0-9]{20,}\.apps\." r"google" r"usercontent\.com",  # Google OAuth client id
]
RULES = [
    ("home-path", re.compile("|".join(HOME_PATTERNS)), "home directory path"),
    ("email", re.compile(EMAIL_PATTERN), "e-mail address"),
    ("secret", re.compile("|".join(SECRET_PATTERNS)), "credential-like string"),
]
PATH_RULES = RULES[:2]  # rules also applied to file names and paths
SQLITE_MAGIC = b"SQLite format 3\x00"
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
BINARY_OK: set[str] = set()  # repo-relative paths of binaries that may ship (none today)
# Folders shared by the scanner (skipped), the export (pruned and denied) and the guard test (skipped).
TOOL_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "node_modules"}
LOCAL_ENV_DIRS = {"venv", "env", ".tox", "build", "dist"}  # local environments and build output; never walked
_FALLBACK_PRIVATE_LIST = "~/.fitbit-air/private-scan.txt"
_WS = re.compile(r"\s+")
_HYPHEN_WRAP = re.compile(r"-[ \t]*\r?\n[ \t]*")
_MARKUP = re.compile(r"[*_`]")


def default_private_list() -> str:
    """FITBIT_AIR_PRIVATE_SCAN when set and non-empty, else ~/.fitbit-air/private-scan.txt (not expanded)."""
    return os.environ.get("FITBIT_AIR_PRIVATE_SCAN") or _FALLBACK_PRIVATE_LIST



@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    detail: str


def load_private(path: Path | None) -> list[str]:
    """The private list's patterns: stripped, non-empty, not comments. A missing file gives []."""
    if not path or not Path(path).exists():
        return []
    lines = (line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def withheld(rel: str) -> str:
    return f"<withheld path sha256:{hashlib.sha256(rel.encode('utf-8')).hexdigest()[:12]}>"


def display_path(path: str | os.PathLike | None) -> str:
    """A path with the home directory shown as ~, so reports never print the home path."""
    if path is None:
        return "?"
    try:
        return "~/" + Path(path).resolve().relative_to(Path.home().resolve()).as_posix()
    except (ValueError, OSError):
        return str(path)


def strip_allowed_emails(text: str) -> str:
    """text without the ALLOWED_EMAILS addresses, removed only where a whole address stands (not inside another)."""
    for email in ALLOWED_EMAILS:
        text = re.sub(r"(?<![\w.+-])" + re.escape(email) + r"(?![\w-]|\.\w)", "", text)
    return text


def _flat_variants(text: str) -> tuple[str, ...]:
    """Case-folded text with whitespace runs collapsed and markdown emphasis removed; a line-end hyphen is either
    dropped (a wrapped word) or kept (a hyphenated value split across lines)."""
    return tuple(_WS.sub(" ", _MARKUP.sub("", _HYPHEN_WRAP.sub(joint, text))).casefold() for joint in ("", "-"))


def _is_binary(data: bytes) -> bool:
    if b"\0" in data:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def path_findings(rel: str, folded: list[str]) -> list[tuple[str, str]]:
    """(rule, detail) for each generic path rule or case-folded private pattern the repo-relative path matches."""
    hits = [(rule, f"file name matches {rule}") for rule, rx, _ in PATH_RULES if rx.search(rel)]
    low = rel.casefold()
    hits += [("private", f"file name matches private pattern #{i}") for i, pat in enumerate(folded, start=1)
             if pat in low]
    return hits


def _scan_file(f: Path, rel: str, folded: list[str]) -> list[Finding]:
    path_hits = path_findings(rel, folded)
    shown = withheld(rel) if path_hits else rel
    out = [Finding(shown, 0, rule, detail) for rule, detail in path_hits]
    data = f.read_bytes()
    if data.startswith(SQLITE_MAGIC) or f.suffix.lower() in SQLITE_SUFFIXES:
        return out + [Finding(shown, 0, "sqlite", "database file")]
    if data and _is_binary(data) and rel not in BINARY_OK:
        out.append(Finding(shown, 0, "binary", "not UTF-8 text"))
    text = data.decode("utf-8", errors="replace")  # binaries too: strings inside them still count
    seen: set[int] = set()
    for n, line in enumerate(text.splitlines(), start=1):
        line = strip_allowed_emails(line)
        for rule, rx, desc in RULES:
            if rx.search(line):
                out.append(Finding(shown, n, rule, desc))
        low = line.casefold()
        for i, pat in enumerate(folded, start=1):
            if pat in low:
                seen.add(i)
                out.append(Finding(shown, n, "private", f"private pattern #{i}"))
    flats = _flat_variants(text)
    for i, pat in enumerate(folded, start=1):
        if i not in seen and any(p in flat for p, flat in zip(_flat_variants(pat), flats)):
            out.append(Finding(shown, 0, "private", f"private pattern #{i} (across lines)"))
    return out


def scan(tree: Path, private: list[str]) -> list[Finding]:
    tree = Path(tree)
    folded = [p.casefold() for p in private]
    out: list[Finding] = []
    for f in sorted(tree.rglob("*")):
        rel_parts = f.relative_to(tree).parts
        if not f.is_file() or TOOL_DIRS & set(rel_parts):
            continue
        out += _scan_file(f, "/".join(rel_parts), folded)
    return out


def format_finding(f: Finding) -> str:
    return f"{f.path}:{f.line}: {f.rule} ({f.detail})"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tree")
    ap.add_argument("--private", default=None, help=f"private pattern list (default: {_FALLBACK_PRIVATE_LIST})")
    a = ap.parse_args(argv)
    plist = Path(a.private or default_private_list()).expanduser()
    tree = Path(a.tree)
    try:
        if not tree.is_dir():
            raise NotADirectoryError(20, "not a folder", str(tree))
        if not plist.exists():
            print("warning: no private pattern list; generic rules only", file=sys.stderr)
        findings = scan(tree, load_private(plist))
    except OSError as e:
        print(f"error: {e.strerror or e}: {display_path(e.filename)}", file=sys.stderr)
        return 2
    for f in findings:
        print(format_finding(f))
    print(f"{len(findings)} finding(s)" if findings else "clean")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
