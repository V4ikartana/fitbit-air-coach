"""Public docs: the README's structure and disclaimers, no hype or mythology in any exported markdown, and every
relative link resolves to a file that the export ships (so nothing points at internal plans or private files).
The exported markdown is chosen by scripts/export_public.py's own allow-list, so the two cannot drift."""
import importlib.util
import re
import sys
import tomllib
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = "export_public_for_docs"  # its own name, so tests/test_export_public.py's module is never replaced


@cache
def _export():
    spec = importlib.util.spec_from_file_location(MODULE, ROOT / "scripts" / "export_public.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[MODULE] = mod  # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


@cache
def _shipped() -> frozenset[str]:
    """Repo-relative posix paths the export would copy (work in progress tolerated)."""
    return frozenset(_export().candidates(ROOT))


def _markdown() -> list[Path]:
    return sorted(ROOT / rel for rel in _shipped() if rel.endswith(".md"))


# Built from pieces so this file reads cleanly; it is not markdown, so the scan below never reads it anyway.
MYTH = ["kar" + "na", "maha" + "bharata"]
FORBIDDEN = [r"production[- ]ready", r"secure by default", r"\bofficial\b(?! (?:google|fitbit) (?:api|docs))",
             r"enterprise", r"works everywhere", r"revolution", r"battle[- ]tested", r"blazing(?:ly)? fast",
             r"world[- ]class", r"\bstars?\b", *MYTH]
INTERNAL = ["docs/superpowers", "SETUP.md", "tests/fixtures/real"]
LINK = re.compile(r"\]\(\s*(?:<([^>]+)>|([^)\s]+))(?:\s+\"[^\"]*\")?\s*\)")  # [text](target "title")
REF_DEF = re.compile(r"^ {0,3}\[[^\]]+\]:\s*(?:<([^>]+)>|(\S+))", re.MULTILINE)  # reference-style: [label]: target
SCHEDULE = "every 2 hours, at login, and after wake if a run was missed"
PROMPTS = ("morning_checkin", "weekly_review", "plan_next_run", "explain_score")


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_the_export_ships_the_public_docs():
    for rel in ("README.md", "docs/setup.md", "docs/architecture.md", "docs/science.md", "docs/configuration.md",
                "docs/plugins.md", "DISCLAIMER.md", "CONTRIBUTING.md", "SECURITY.md"):
        assert rel in _shipped(), rel
    assert not (ROOT / "SETUP.md").exists()


def test_readme_structure_and_disclaimers():
    r = (ROOT / "README.md").read_text()
    assert r.startswith("# fitbit-air-coach\n")
    for heading in ("## What it does", "## Features", "## Compatibility and prerequisites", "## Quick start",
                    "## Configuration", "## Usage", "## Architecture", "## Security and privacy",
                    "## Known limitations", "## Roadmap", "## Contributing", "## License"):
        assert re.search(rf"^{re.escape(heading)}$", r, re.MULTILINE), heading
    low = r.lower()
    assert "unofficial" in low and "not affiliated" in low and "not medical advice" in low
    assert "macos" in low and "google health api v4" in low and "health.googleapis.com/v4" in r
    assert "Built by Vaikartana ([@v4ikartana](https://github.com/v4ikartana))" in r
    assert "not tested yet" in low  # Linux is not claimed


def test_no_hype_or_mythology_in_exported_markdown():
    assert len(_markdown()) >= 10
    for f in _markdown():
        text = f.read_text().lower()
        hits = [p for p in FORBIDDEN if re.search(p, text)]
        assert hits == [], f"{_rel(f)}: {hits}"


def test_exported_markdown_never_mentions_internal_files():
    for f in _markdown():
        text = f.read_text()
        assert [s for s in INTERNAL if s in text] == [], _rel(f)


def _targets(text: str) -> list[str]:
    return [a or b for a, b in LINK.findall(text) + REF_DEF.findall(text)]


def test_link_patterns_find_inline_and_reference_links():
    text = 'a [x](a.md) b [y](<b c.md> "t") [z][r]\n\n[r]: ref.md\n   [s]: <sub/s.md> "title"\n'
    assert sorted(_targets(text)) == ["a.md", "b c.md", "ref.md", "sub/s.md"]


def test_relative_links_resolve_inside_the_export():
    checked = 0
    for f in _markdown():
        for target in _targets(f.read_text()):
            if re.match(r"[a-z][a-z0-9+.-]*:", target, re.IGNORECASE) or target.startswith("#"):
                continue  # a URL, mailto: or an in-page anchor
            path = target.split("#", 1)[0]
            resolved = (f.parent / path).resolve()
            assert resolved.is_relative_to(ROOT), f"{_rel(f)} -> {target} leaves the repo"
            rel = resolved.relative_to(ROOT).as_posix()
            ships = rel in _shipped() or (resolved.is_dir() and any(s.startswith(rel + "/") for s in _shipped()))
            assert resolved.exists() and ships, f"{_rel(f)} -> {target} is not in the export"
            checked += 1
    assert checked > 10


def test_science_doc_lists_every_evidence_source():
    sources = tomllib.loads((ROOT / "src/fitbit_air/analysis/evidence.toml").read_text())["source"]
    science = (ROOT / "docs" / "science.md").read_text()
    ids = {s["id"] for s in sources}
    section = science.split("## Sources by domain", 1)[1]
    listed = set(re.findall(r"^\| [^|]+ \| `([^`]+)` \|", section, re.MULTILINE))
    assert sorted(ids - listed) == [], "missing from docs/science.md"
    assert sorted(listed - ids) == [], "listed in docs/science.md but not in evidence.toml"
    assert section.count("\n| ") - 1 == len(ids)  # one row per source, plus the header


def test_readme_names_the_repo_and_package_and_is_honest_about_network_use():
    r = (ROOT / "README.md").read_text()
    flat = " ".join(r.split())
    assert "The repository is fitbit-air-coach; the package and command are `fitbit-air`." in flat
    assert "under 240 KB" in r and "uses your system fonts and loads no outside scripts, fonts or images" in flat
    assert "fonts.googleapis.com" not in r  # the page no longer fetches web fonts
    assert "makes no other network calls" not in r


def test_docs_use_one_schedule_wording_and_the_registered_prompt_names():
    for rel in ("README.md", "docs/setup.md"):
        text = " ".join((ROOT / rel).read_text().split())
        assert SCHEDULE in text, rel
        assert "login/wake" not in text, rel
    readme = (ROOT / "README.md").read_text()
    assert [p for p in PROMPTS if f"`{p}`" not in readme] == []
    assert "`morning_checkin`" in (ROOT / "docs" / "setup.md").read_text()


def test_dashboard_readme_has_no_owner_specific_setup():
    text = (ROOT / "dashboard" / "README.md").read_text()
    for leftover in ("09:00", "morning-brief", "morning brief", "on this Mac", "the URL in"):
        assert leftover not in text, leftover
    assert "snapshot/latest" in text and "fitbit-air snapshot" in text


def test_docs_are_finished_and_for_new_installs():
    for path in _markdown():
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\bTBD\b|\bTODO\b", text), _rel(path)
        assert not re.search(r"older (?:in-repo )?install|older versions kept", text, re.IGNORECASE), _rel(path)


def test_contributing_explains_the_maintainer_tools():
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    section = text.split("## Maintainer tools", 1)[1]
    for script in ("export_public.py", "build_private_scan_list.py", "make_fixtures.py"):
        assert script in section
    assert "not needed to contribute" in section


def test_readme_separates_local_storage_from_ai_processing():
    """The README must not call the data 'local' without saying what the coach and dashboard send to Anthropic."""
    r = (ROOT / "README.md").read_text()
    flat = " ".join(r.split())
    assert "Your data stays local" not in r
    assert "local-first, not fully offline" in flat.lower()
    assert "sent to Anthropic" in flat
    assert "Privacy note:" in r
    assert "they need no AI service" in flat

