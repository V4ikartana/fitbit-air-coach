"""What any dashboard redesign must keep: the config block, the host hookup, the tabs and the theme tokens."""
import json
import re
import shutil
import subprocess
from importlib.resources import files

import anyio
import pytest
from mcp import Client

from fitbit_air.coach.server import build_server
from fitbit_air.dashboard_build import TABS, THEME_TOKENS

HTML = files("fitbit_air.dashboard").joinpath("template.html").read_text(encoding="utf-8")
SNAPSHOT_PY = files("fitbit_air.coach").joinpath("snapshot.py").read_text(encoding="utf-8")

# Every coach tool the page calls today; a redesign may drop one but must not invent another.
PAGE_TOOLS = {"data_status", "get_overview", "get_trends", "get_sleep", "get_activity", "get_running", "get_body_age",
              "get_coach_notes", "get_evidence"}


def _page_tools() -> set[str]:
    """Tool names in the SOURCES table (`["tool", {args}]`) plus direct `callTool(SERVER, "tool", ...)` calls."""
    sources = re.search(r"const SOURCES = .*?\n  \}\);", HTML, re.S).group(0)
    return set(re.findall(r'\["([a-z_]+)", ', sources)) | set(re.findall(r'callTool\(SERVER, "([a-z_]+)"', HTML))


def _registered_tools(home) -> set[str]:
    async def go():
        async with Client(build_server()) as c:
            return {t.name for t in (await c.list_tools()).tools}
    return anyio.run(go)


def test_config_block_markers_and_server_name():
    """The builder swaps the block between the markers and the page addresses the coach by this server name."""
    assert "// fa:cfg-start" in HTML and "// fa:cfg-end" in HTML
    assert 'const SERVER = "host:fitbit-coach"' in HTML


def test_config_is_read_from_the_embedded_fa_config_element():
    """`dashboard build` injects the user's layout as <script id="fa-config">; the page must read that element."""
    assert 'getElementById("fa-config")' in HTML


def test_host_hookup_uses_mcp_db_and_the_snapshot_doc():
    """Live data comes through the mcp capability, the stored snapshot through db; losing either breaks a mode."""
    for needle in ('use("mcp")', 'use("db")', '"snapshot/latest"', "callTool(SERVER"):
        assert needle in HTML, needle


def test_every_tab_has_a_button():
    """dashboard.toml can name any of these tabs, so each needs a data-tab button the page can show."""
    for tab in TABS:
        assert re.search(rf'data-tab="{tab}"', HTML), tab


def test_every_tab_has_a_page_section():
    """Selecting a tab shows the section carrying its name in data-page and id; without it the tab is blank."""
    for tab in TABS:
        assert re.search(rf'data-page="{tab}"', HTML), tab
        assert f'id="page-{tab}"' in HTML, tab


def test_every_theme_token_is_a_css_variable():
    """dashboard.toml [theme] overrides tokens by name; each must exist as a --token for the override to land."""
    for token in THEME_TOKENS:
        assert re.search(rf"--{re.escape(token)}\s*:", HTML), token


def test_snapshot_sections_are_read():
    """Snapshot mode (no live host) shows only what the page reads from these sections of the stored document."""
    for key in ("status", "overview", "trends30", "trends90", "sleep14", "activity30", "running90", "bodyage",
                "targets"):
        assert key in HTML, key


def test_snapshot_sections_exist_in_the_snapshot_builder():
    """The sections the page reads must be ones build_snapshot actually writes, or snapshot mode shows nothing."""
    written = set(re.findall(r'^\s+"(\w+)": ', SNAPSHOT_PY, re.M))
    for key in ("status", "overview", "trends30", "trends90", "sleep14", "activity30", "running90", "bodyage",
                "targets"):
        assert key in written, key


def test_self_contained():
    """The page runs in a sandboxed artifact frame: no external scripts and no remote images."""
    assert not re.search(r"<script[^>]+src=", HTML)
    assert not re.search(r'<img[^>]+src="https?:', HTML)
    assert not re.search(r'<link[^>]+href="https?:', HTML) and "@import" not in HTML  # system fonts, no web fonts


def test_page_calls_exactly_the_known_coach_tools():
    """A redesign may drop a call, but a new tool name here must be added to PAGE_TOOLS on purpose."""
    assert _page_tools() == PAGE_TOOLS


def test_page_only_calls_tools_the_server_registers(tmp_path, monkeypatch):
    """A redesign must not call a coach tool that does not exist; the live view would silently fall back."""
    monkeypatch.setenv("FITBIT_AIR_HOME", str(tmp_path))
    assert _page_tools() <= _registered_tools(tmp_path)


def test_safety_findings_tell_the_reader_to_see_a_doctor():
    """Safety insights must say to contact a doctor if symptoms persist (the page has no separate disclaimer)."""
    assert "contact a doctor" in HTML
    assert "chest pain, breathlessness, fainting, fever" in HTML
    assert "o.safety" in HTML and "notice safety" in HTML


def test_tabs_use_tablist_and_tab_roles_with_selected_state():
    """Screen readers announce the tab bar from these roles and aria-selected; keep them on the bar and buttons."""
    assert re.search(r'role="tablist"', HTML)
    for tab in TABS:
        assert re.search(rf'role="tab"[^>]*data-tab="{tab}"[^>]*aria-selected="(true|false)"', HTML), tab


def test_tab_pages_have_the_tabpanel_role():
    """Each page section should be a tabpanel so assistive tech links tabs to their content (absent today)."""
    for tab in TABS:
        assert re.search(rf'<section[^>]*role="tabpanel"[^>]*data-page="{tab}"', HTML) or \
            re.search(rf'<section[^>]*data-page="{tab}"[^>]*role="tabpanel"', HTML), tab


def test_reduced_motion_is_respected():
    """Animation stays behind a prefers-reduced-motion query, and a reduce block switches all of it off."""
    css = re.search(r"<style>(.*?)</style>", HTML, re.S).group(1)
    assert "prefers-reduced-motion" in css
    assert not re.search(r"(transition|animation)\s*:", css.split("prefers-reduced-motion")[0])
    reduce = re.search(r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", css, re.S)
    assert reduce, "no prefers-reduced-motion: reduce block"
    assert re.search(r"animation:\s*none", reduce.group(1)) and re.search(r"transition:\s*none", reduce.group(1))
    assert "prefers-reduced-motion: reduce" in HTML.split("<script>")[1]  # count-ups check it too


def test_page_shows_a_not_medical_advice_notice():
    """The design brief requires a visible "Not medical advice" notice in the markup, not in a script or comment."""
    visible = re.sub(r"<!--.*?-->|<script\b.*?</script>|<style\b.*?</style>", "", HTML, flags=re.S | re.I)
    assert re.search(r"not\s+medical\s+advice", visible, re.I)


def test_state_thresholds_on_the_page_match_the_registry():
    """A state pill's cut-off is the analysis registry's default, read through the user's overrides."""
    import tomllib
    params = tomllib.loads(files("fitbit_air.analysis").joinpath("evidence.toml").read_text(encoding="utf-8"))["params"]
    page = dict(re.findall(r"(SLEEP_DEBT_WATCH_H|STEPS_BENCHMARK|MVPA_TARGET_MIN) = (\d+)", HTML))
    assert page == {"SLEEP_DEBT_WATCH_H": str(params["sleep_debt_watch_h"]), "STEPS_BENCHMARK": str(params["steps_benchmark"]),
                    "MVPA_TARGET_MIN": str(params["mvpa_target_min"])}
    for name, const in (("sleep_debt_watch_h", "SLEEP_DEBT_WATCH_H"), ("steps_benchmark", "STEPS_BENCHMARK"),
                        ("mvpa_target_min", "MVPA_TARGET_MIN")):
        assert f'thr("{name}", {const})' in HTML, name  # honours a dashboard of personal thresholds
    assert '"Good"' not in HTML.split("const BAND")[1].split("\n", 1)[1]  # "Good" only from a recovery band


def _state_block() -> str:
    return HTML[HTML.index("// fa:state-start"):HTML.index("// fa:state-end")]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_state_words_follow_the_payload():
    """Run the page's own state helpers under node: words come from bands, statuses and reasons, never guesses."""
    cases = {
        "baseline": {"value": None, "status": "insufficient_data", "reason": "building baseline: 3/14 valid days"},
        "missing": {"value": None, "status": "insufficient_data", "reason": "today's HRV or resting HR is missing"},
        "nosleep": {"value": None, "status": "insufficient_data",
                    "reason": "no valid main sleep (staged, 3 h or more) waking on this date"},
        "yellow": {"value": 43, "status": "ok", "confidence": 1, "band": "yellow"},
        "noband": {"value": 72, "status": "ok", "confidence": 1, "band": None},
        "weak": {"value": 60, "status": "ok", "confidence": 0.3, "band": "green"},
        "prov": {"value": 32, "status": "provisional", "confidence": 0.8},
    }
    plans = {st: {"status": st, "first_run_by": "2026-11-01" if st == "no_runs" else None, "safe_by_race_km": 6.0}
             for st in ("on_track", "tight", "behind", "no_runs", "no_runs_behind", "something_new")}
    script = _state_block() + """
const [cases, plans] = JSON.parse(process.argv[1]);
const out = {state: {}, center: {}, tone: {}, word: {}, advice: {}};
for (const [k, s] of Object.entries(cases)) { out.state[k] = scoreState(s); out.center[k] = scoreCenter(s); }
for (const [k, pc] of Object.entries(plans)) {
  out.tone[k] = planTone(k); out.word[k] = planWord(k); out.advice[k] = planAdvice(pc, (d) => "D" + d);
}
out.thr = [threshold({sleep_debt_watch_h: {value: 6, default: 5, reason: "x"}}, "sleep_debt_watch_h", 5),
           threshold({sleep_debt_watch_h: {value: "6"}}, "sleep_debt_watch_h", 5), threshold(null, "steps_benchmark", 7000)];
console.log(JSON.stringify(out));"""
    run = subprocess.run(["node", "-e", script, json.dumps([cases, plans])], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    r = json.loads(run.stdout)
    assert r["state"]["baseline"] == {"tone": "", "word": "Building baseline"}
    assert r["state"]["missing"] == r["state"]["nosleep"] == {"tone": "", "word": "Not enough data"}
    assert r["state"]["yellow"] == {"tone": "warn", "word": "Caution"} and r["state"]["noband"] is None
    assert r["state"]["weak"] == {"tone": "", "word": "Low confidence"}
    assert r["state"]["prov"] == {"tone": "", "word": "Provisional"}
    assert r["center"]["baseline"] == "building" and r["center"]["missing"] == r["center"]["nosleep"] == "\u2014"
    assert r["center"]["yellow"] is None  # a value: the page shows the number
    assert r["tone"] == {"on_track": "ok", "tight": "warn", "behind": "bad", "no_runs": "", "no_runs_behind": "bad",
                         "something_new": ""}
    assert r["word"]["no_runs_behind"] == "Behind: no runs yet" and r["word"]["something_new"] == "Something new"
    adv = r["advice"]
    assert "run-walk" in adv["behind"] and "run-walk" in adv["no_runs_behind"]
    assert all("run-walk" not in adv[k] for k in ("on_track", "tight", "no_runs", "something_new"))
    assert adv["no_runs"] == "No runs yet. Start with an easy run by D2026-11-01."
    assert adv["something_new"] == ""
    assert r["thr"] == [6, 5, 7000]


def test_light_run_colour_is_not_a_brand_orange_and_stays_legible():
    """The light `run` token is a red-orange of our own, at least 3:1 on the panel and apart from `bad`."""
    light = re.search(r':root\[data-theme="light"\] \{(.*?)\}', HTML, re.S).group(1)
    run = re.search(r"--run:\s*(#[0-9A-Fa-f]{6})", light).group(1).upper()
    assert run == "#EB5B3C" and HTML.count("--run: #EB5B3C") == 2  # both light blocks

    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    assert (1.05) / (lum(run) + 0.05) >= 3.0  # against the light panel (#FFFFFF), as a chart mark


def test_copy_is_accurate_about_where_data_lives_and_names_no_other_brand():
    visible = re.sub(r"<!--.*?-->|<script\b.*?</script>|<style\b.*?</style>", "", HTML, flags=re.S | re.I)
    assert "Your data lives on your computer; this page shows it through Claude, or from a private snapshot stored " \
           "in this Artifact." in " ".join(visible.split())
    assert "Your data stays on your computer" not in HTML
    assert "Google zone minutes" not in HTML and "Active zone minutes" in HTML
    assert HTML.startswith('<meta charset="utf-8">')
