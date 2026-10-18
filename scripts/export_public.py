"""Export an allow-listed, history-free copy of this repo into a fresh folder, then scan it.

Usage: uv run python scripts/export_public.py <dest> [--src <repo>]
    [--private ~/.fitbit-air/private-scan.txt | --no-private-list]

- Candidates: in a git checkout, the files committed at HEAD (git ls-tree), copied from HEAD's blobs, so the copy
  equals the committed content even when the working tree differs or git is told to ignore changes
  (assume-unchanged, skip-worktree). An allowed file that is not committed, a selected file with staged or unstaged
  changes, or a selected file missing from the working tree stops the export, and so does a committed symlink or
  submodule. A --src that is a subfolder of a checkout is refused. Outside git, a walk of the folder.
- Only paths matching ALLOW are copied, and DENY (matched case-insensitively) always wins: real fixtures, internal
  plans, caches, databases, secrets, env files, data, keys, tokens, virtualenvs and git data.
- A symlink that would ship (selected, or a folder under an allowed path) stops the export before anything is
  copied; symlinks elsewhere (a local venv, say) are ignored. Error messages show a path that matches a generic path
  rule or a private pattern only in its withheld (sha256) form.
- <dest> must be new or empty and must not sit inside the source. The copy is staged in <dest>.partial (which must
  not exist) and scanned with check_public.py (generic rules plus the private list). Clean: it is renamed to <dest>.
  Findings: it is renamed to <dest>.blocked and ExportError names path:line and rule only. A crash while copying
  leaves <dest>.partial. Nothing is ever deleted or overwritten.
- The private list is required: a missing or empty list stops the export (exit 2) unless --no-private-list is given.
--src defaults to the repo this script lives in. Exit codes: 0 clean, 1 blocked, 2 error or no private list."""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ALLOW = [
    "src/**", "tests/**", "docs/*.md", "dashboard/README.md",
    "scripts/check_public.py", "scripts/export_public.py", "scripts/make_fixtures.py",
    "scripts/build_private_scan_list.py", "scripts/register_claude_desktop.py",
    "README.md", "pyproject.toml", "uv.lock", ".gitignore",
    "LICENSE", "NOTICE", "DISCLAIMER.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md",
    ".github/**",
]
DENY = [
    "tests/fixtures/real/**", "tests/test_real_fixtures.py", "docs/superpowers/**",
    "**/__pycache__/**", "**/*.pyc", "**/*.db*", "**/*.sqlite*",
    "**/.git", "**/.git/**", "**/.venv/**", "**/.pytest_cache/**", "**/.ruff_cache/**", "**/.mypy_cache/**",
    "**/node_modules/**", "**/*.egg-info/**", "**/.DS_Store", "**/.superpowers/**", "**/.claude/**",
    "**/.env", "**/.env.*", "**/secrets/**", "**/data/**", "**/*.pem", "**/*.key", "**/client_secret*.json",
    "**/token*.json", "**/*.env",
]
GIT_LINK_MODES = {"120000": "symlink", "160000": "submodule"}


class ExportError(Exception):
    pass


def _load_check():
    """scripts/check_public.py as a module, reusing an already loaded copy of the same file."""
    path = HERE / "check_public.py"
    mod = sys.modules.get("check_public")
    if mod is not None and Path(getattr(mod, "__file__", "")).resolve() == path:
        return mod
    spec = importlib.util.spec_from_file_location("check_public", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_public"] = mod  # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


check_public = _load_check()
PRUNE_ANYWHERE = check_public.TOOL_DIRS  # never walked at any depth (the scanner skips them too)
PRUNE_TOP = check_public.LOCAL_ENV_DIRS  # local venvs and build output: skipped only at the repo's top level


def _glob_rx(pattern: str, flags: int = 0) -> re.Pattern[str]:
    """A repo-relative glob as a regex: ** spans directories, * and ? stay inside one path segment."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:[^/]+/)*", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out, flags)


_ALLOW_RX = [_glob_rx(p) for p in ALLOW]
_DENY_RX = [_glob_rx(p, re.IGNORECASE) for p in DENY]


def selected(rel: str) -> bool:
    """True when a repo-relative posix path ships: it matches ALLOW and no DENY pattern."""
    return (any(rx.fullmatch(rel) for rx in _ALLOW_RX)
            and not any(rx.fullmatch(rel) for rx in _DENY_RX))


def _allow_reaches_under(link: str) -> bool:
    """True when some ALLOW pattern could match a path below `link` (taken as a folder)."""
    parts = link.split("/")
    for pattern in ALLOW:
        segs = pattern.split("/")
        for i, part in enumerate(parts):
            if i >= len(segs) or (segs[i] != "**" and not _glob_rx(segs[i]).fullmatch(part)):
                break
            if segs[i] == "**":
                return True  # anything below matches
        else:
            if len(segs) > len(parts):  # the pattern continues below the link
                return True
    return False


def _would_ship(link: str) -> bool:
    """A symlink matters when it is selected itself, or when an allowed path could sit under it."""
    return selected(link) or _allow_reaches_under(link)


def _walk(src: Path) -> tuple[list[str], list[str]]:
    """(regular files, symlinks) under src as repo-relative posix paths, outside PRUNE_ANYWHERE folders and the
    top-level PRUNE_TOP ones."""
    files, links = [], []
    for dirpath, dirnames, filenames in os.walk(src):
        base = Path(dirpath).relative_to(src)
        keep = []
        for d in sorted(dirnames):
            if d in PRUNE_ANYWHERE or (d in PRUNE_TOP and base == Path(".")):
                continue
            if os.path.islink(os.path.join(dirpath, d)):
                links.append((base / d).as_posix())
            else:
                keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            rel = (base / name).as_posix()
            (links if os.path.islink(os.path.join(dirpath, name)) else files).append(rel)
    return files, links


def _git(src: Path, *args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(["git", *args], cwd=src, input=stdin, capture_output=True, check=True).stdout


def _git_top(src: Path) -> Path | None:
    """The checkout's top level when src is inside a git checkout, else None (a plain folder)."""
    try:
        return Path(_git(src, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError) as e:
        if (src / ".git").exists():
            raise ExportError("git could not read the source checkout") from e
        return None


def _git_committed(src: Path) -> dict[str, tuple[str, str]]:
    """{path: (mode, blob sha)} for every file committed at HEAD."""
    try:
        out = _git(src, "ls-tree", "-r", "-z", "--full-tree", "HEAD").decode("utf-8")
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError) as e:
        raise ExportError("git could not list the files committed at HEAD (is there a commit yet?)") from e
    committed = {}
    for entry in filter(None, out.split("\0")):
        meta, path = entry.split("\t", 1)
        mode, _kind, sha = meta.split()
        committed[path] = (mode, sha)
    return committed


def _git_index(src: Path) -> dict[str, tuple[str, str]]:
    """{path: (mode, blob sha)} for every file in the index (staged work included; no commit needed)."""
    try:
        out = _git(src, "ls-files", "-s", "-z").decode("utf-8")
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError) as e:
        raise ExportError("git could not list the index") from e
    index = {}
    for entry in filter(None, out.split("\0")):
        meta, path = entry.split("\t", 1)
        mode, sha, _stage = meta.split()
        index[path] = (mode, sha)
    return index


def _git_changed(src: Path) -> set[str]:
    """Paths with staged or unstaged changes (untracked files are left to the untracked check)."""
    try:
        status = _git(src, "status", "--porcelain=v1", "-z", "--untracked-files=no").decode("utf-8").split("\0")
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError) as e:
        raise ExportError("git could not report the checkout's status") from e
    changed: set[str] = set()
    entries = iter(status)
    for entry in entries:
        if len(entry) < 4:
            continue
        changed.add(entry[3:])
        if entry[0] in "RC":  # a rename or copy: the next entry is the original path
            changed.add(next(entries, ""))
    return changed


def _git_blobs(src: Path, shas: list[str]) -> list[bytes]:
    """The contents of the given blobs, in order, from one `git cat-file --batch`."""
    try:
        out = _git(src, "cat-file", "--batch", stdin="".join(f"{sha}\n" for sha in shas).encode())
    except (OSError, subprocess.CalledProcessError) as e:
        raise ExportError("git could not read the committed files") from e
    blobs, i = [], 0
    for sha in shas:
        nl = out.index(b"\n", i)
        header = out[i:nl].split()
        if len(header) != 3 or header[0].decode() != sha or header[1] != b"blob":
            raise ExportError("git returned an unexpected object while reading the committed files")
        size = int(header[2])
        blobs.append(out[nl + 1:nl + 1 + size])
        i = nl + 1 + size + 1
    return blobs


def _shown(rel: str, folded_private: list[str]) -> str:
    """rel, or its withheld form when the path itself matches a generic path rule or a private pattern."""
    return check_public.withheld(rel) if check_public.path_findings(rel, folded_private) else rel


def _select(src: Path, private: list[str], skip_untracked: bool = False, allow_dirty: bool = False,
            from_index: bool = False) -> tuple[list[str], dict[str, tuple[str, str]] | None]:
    """(paths to copy, their committed (mode, sha) in git mode or None for a plain folder). from_index lists the
    index instead of HEAD (for candidates(): works before the first commit and includes staged files)."""
    if not src.is_dir():
        raise ExportError("the source is not a folder")
    folded = [p.casefold() for p in private]

    def listed(paths) -> str:
        return ", ".join(_shown(r, folded) for r in paths)

    files, links = _walk(src)
    links = [link for link in links if _would_ship(link)]
    if links:
        raise ExportError(f"{len(links)} symlink(s) in the source: {listed(links)}")
    top = _git_top(src)
    if top is None:
        return [r for r in files if selected(r)], None
    if top != src:
        raise ExportError("the source is inside a git checkout but is not its top level")
    committed = _git_index(src) if from_index else _git_committed(src)
    chosen = sorted(r for r in committed if selected(r))
    for mode, kind in GIT_LINK_MODES.items():
        bad = [r for r in chosen if committed[r][0] == mode]
        if bad:
            raise ExportError(f"{len(bad)} committed {kind}(s) in the source: {listed(bad)}")
    missing = [r for r in chosen if not (src / r).is_file()]
    if missing:
        raise ExportError(f"{len(missing)} committed file(s) missing from the working tree: {listed(missing)}")
    changed = _git_changed(src)
    dirty = [r for r in chosen if r in changed]
    if dirty and not allow_dirty:
        raise ExportError(f"{len(dirty)} selected file(s) have uncommitted changes (commit them first, so the export "
                          f"equals the committed content): {listed(dirty)}")
    untracked = sorted({r for r in files if selected(r)} - set(committed))
    if untracked and not skip_untracked:
        raise ExportError(f"{len(untracked)} untracked or uncommitted file(s) match the allow-list (commit or move "
                          f"them first): {listed(untracked)}")
    return chosen, committed


def candidates(src: Path) -> list[str]:
    """What an export of src would copy right now, tolerating work in progress: the repo-relative paths, with
    allowed files git does not track skipped and uncommitted edits allowed. The docs tests use it to find the
    shipped markdown. It lists the git index (staged files included, no commit needed); export() itself copies
    from HEAD and stays strict about both."""
    return _select(Path(src).resolve(), [], skip_untracked=True, allow_dirty=True, from_index=True)[0]


def export(src: Path, dest: Path, private: list[str], *, skip_untracked: bool = False,
           allow_dirty: bool = False) -> list[Path]:
    """Copy the allow-listed files of src into dest (via dest.partial), scan, and return the copied paths.
    For the test suite's dry run of this checkout only (the CLI never sets them): skip_untracked=True skips rather
    than refuses allowed files that are not committed, and allow_dirty=True tolerates selected files with
    uncommitted changes (HEAD's content is still what is copied)."""
    src, dest = Path(src).resolve(), Path(dest).resolve()
    partial = dest.with_name(dest.name + ".partial")
    blocked = dest.with_name(dest.name + ".blocked")
    if dest == src or dest.is_relative_to(src):
        raise ExportError("the destination is inside the source tree")
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise ExportError("the destination is not empty")
    for leftover in (partial, blocked):
        if leftover.exists() or leftover.is_symlink():
            raise ExportError(f"{leftover.name} already exists; inspect it and move it away first")
    files, committed = _select(src, private, skip_untracked, allow_dirty)
    blobs = _git_blobs(src, [committed[r][1] for r in files]) if committed is not None else None
    partial.mkdir(parents=True)
    for n, rel in enumerate(files):
        target = partial / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if blobs is None:
            shutil.copyfile(src / rel, target, follow_symlinks=False)
            shutil.copymode(src / rel, target)
        else:
            target.write_bytes(blobs[n])
            target.chmod(0o755 if committed[rel][0] == "100755" else 0o644)
    findings = check_public.scan(partial, private)
    if findings:
        partial.rename(blocked)
        listed = "; ".join(f"{f.path}:{f.line} {f.rule}" for f in findings)
        raise ExportError(f"{len(findings)} finding(s), copy kept in {blocked.name}: {listed}")
    partial.rename(dest)
    return [dest / rel for rel in files]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dest")
    ap.add_argument("--src", default=str(HERE.parent))
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--private", default=None, help="private pattern list (default: ~/.fitbit-air/private-scan.txt"
                                                       " or $FITBIT_AIR_PRIVATE_SCAN)")
    group.add_argument("--no-private-list", action="store_true", help="export with the generic rules only")
    a = ap.parse_args(argv)
    try:
        if a.no_private_list:
            private: list[str] = []
            print("warning: --no-private-list: generic rules only", file=sys.stderr)
        else:
            plist = Path(a.private or check_public.default_private_list()).expanduser()
            private = check_public.load_private(plist)
            if not private:
                print(f"refusing to export: the private pattern list {check_public.display_path(plist)} is missing "
                      "or empty. Build it with scripts/build_private_scan_list.py, or pass --no-private-list to "
                      "export with the generic rules only.", file=sys.stderr)
                return 2
        copied = export(Path(a.src), Path(a.dest), private)
    except ExportError as e:
        print(f"export blocked: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"error: {e.strerror or type(e).__name__}: {check_public.display_path(e.filename)}", file=sys.stderr)
        return 2
    print(f"exported {len(copied)} file(s) to {check_public.display_path(a.dest)}: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
