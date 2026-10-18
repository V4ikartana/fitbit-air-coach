"""A preview of the real dashboard outside Claude: the page plus a stand-in host that serves a snapshot."""
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dashboard_preview", ROOT / "scripts" / "dashboard_preview.py")
PREVIEW = importlib.util.module_from_spec(spec)
sys.modules["dashboard_preview"] = PREVIEW
spec.loader.exec_module(PREVIEW)

SNAP = {"as_of": "2026-10-18", "generated_at": "2026-10-18T06:00:00Z", "overview": {"date": "2026-10-18"},
        "note": "</script><b>x</b>"}


def _stub(html: str) -> str:
    m = re.search(r'<script id="fa-preview-host">(.*?)</script>', html, re.S)
    assert m, "the stand-in host script is missing"
    return m.group(1)


def test_stub_comes_before_the_page_script_and_serves_the_snapshot():
    html = PREVIEW.render(SNAP, theme=None)
    stub = html.index('id="fa-preview-host"')
    assert stub < html.index("const SERVER")
    assert html.index('id="fa-preview-snapshot"') < stub
    assert "</script><b>" not in html  # the snapshot is embedded safely
    assert "snapshot/latest" in _stub(html)
    assert json.loads(PREVIEW.embedded_snapshot(html)) == SNAP


def test_config_is_embedded_and_the_page_is_a_full_document():
    html = PREVIEW.render(SNAP, theme=None)
    assert 'id="fa-config"' in html
    assert html.lower().startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in html and 'name="viewport"' in html
    assert re.search(r"<html\b[^>]*>", html).group(0) == '<html lang="en">'  # no forced theme: follows the system


def test_theme_can_be_forced():
    # the template's own CSS also contains data-theme="light", so look at the <html> element itself
    assert re.search(r"<html\b[^>]*>", PREVIEW.render({}, theme="light")).group(0) == \
        '<html lang="en" data-theme="light">'
    assert 'data-theme="dark"' in re.search(r"<html\b[^>]*>", PREVIEW.render({}, theme="dark")).group(0)


def test_main_writes_the_preview(tmp_path):
    snap = tmp_path / "snapshot.json"
    snap.write_text(json.dumps(SNAP), encoding="utf-8")
    out = tmp_path / "preview" / "index.html"
    assert PREVIEW.main([str(snap), str(out), "--theme", "light"]) == 0
    html = out.read_text(encoding="utf-8")
    assert json.loads(PREVIEW.embedded_snapshot(html)) == SNAP and 'data-theme="light">' in html


NODE = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
END = date(2026, 10, 18)


def _trends(days: int) -> dict:
    """Shaped as get_trends returns it: start, end, and per-metric rows with a date."""
    dates = [(END - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
    return {"start": dates[0], "end": dates[-1], "unknown": [],
            "series": {m: [{"date": d, "value": float(i)} for i, d in enumerate(dates)] for m in ("rhr", "hrv_7d")}}


LIVE_SNAP = {"as_of": "2026-10-18", "generated_at": "2026-10-18T06:00:00Z",
             "overview": {"date": "2026-10-18", "evidence_ids": ["plews2013_hrv"]}, "status": {"freshness": {}},
             "trends30": _trends(30), "trends90": _trends(90), "sleep14": {"s": 14}, "activity30": {"a": 30},
             "running90": {"r": 90}, "bodyage": {"b": 1}, "targets": {"notes": []}}


def _node(html: str, body: str, setup: str = "") -> dict:
    """Run the page's stand-in host under node with a minimal document, then `body` (async; returns a value).
    `setup` runs before the stub; console.info/warn calls are collected in `logs`."""
    blocks = dict(re.findall(r'<script type="application/json" id="(fa-preview-[a-z]+)">(.*?)</script>', html, re.S))
    script = """
const blocks = JSON.parse(process.argv[1]);
const logs = [];
console.info = (m) => logs.push(["info", m]); console.warn = (m) => logs.push(["warn", m]);
globalThis.window = {};
globalThis.document = {getElementById: (id) => id in blocks ? {textContent: blocks[id]} : null};
%s
%s
(async () => { %s })().then((v) => console.log(JSON.stringify(v)), (e) => { console.error(e); process.exit(1); });
""" % (setup, _stub(html), body)
    out = subprocess.run(["node", "-e", script, json.dumps(blocks)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


CALL = """
const mcp = await window.claude.use("mcp");
const call = async (tool, args) => {
  try { const r = await mcp.callTool("host:fitbit-coach", tool, args, {cache: {staleTime: 60000}});
        return {payload: r.payload, structured: r.structuredContent, text: JSON.parse(r.content[0].text)}; }
  catch (e) { return {code: e.code, isError: e.result && e.result.isError, message: e.message}; }
};
"""


@NODE
def test_snapshot_only_serves_the_snapshot_and_offers_no_mcp():
    html = PREVIEW.render(SNAP, theme=None, live=False)
    result = _node(html, """
const db = await window.claude.use("db");
const doc = await db.doc("snapshot/latest").get();
const other = await db.doc("something/else").get();
return {exists: doc.exists, data: doc.data(), other: other.exists, mcp: await window.claude.use("mcp"),
        perms: await window.claude.use("permissions"), logs};
""")
    assert result == {"exists": True, "data": SNAP, "other": False, "mcp": None, "perms": None,
                      "logs": [["info", "fa-preview: stand-in host serving a snapshot"]]}


@NODE
def test_use_resolves_null_for_namespaces_it_does_not_serve():
    result = _node(PREVIEW.render(SNAP), "return [await window.claude.use('room'), await window.claude.use('')];")
    assert result == [None, None]


@NODE
def test_a_real_host_is_left_alone():
    setup = "const real = {use: async () => 'real'}; window.claude = real;"
    body = "return {same: window.claude === real, db: await window.claude.use('db'), logs};"
    result = _node(PREVIEW.render(SNAP), body, setup)
    assert result == {"same": True, "db": "real",
                      "logs": [["warn", "fa-preview: a real host is present; the stand-in stays off"]]}


@NODE
def test_live_mode_is_the_default_and_answers_get_overview_like_a_host():
    html = PREVIEW.render(LIVE_SNAP)
    result = _node(html, CALL + """
const db = await window.claude.use("db");
const perms = await window.claude.use("permissions");
return {overview: await call("get_overview", {}), snap: (await db.doc("snapshot/latest").get()).exists,
        perms: await perms.request(["mcp:host:fitbit-coach"]), one: await perms.state("mcp"),
        all: await perms.state()};
""")
    ov = LIVE_SNAP["overview"]
    assert result["overview"] == {"payload": ov, "structured": ov, "text": ov}
    assert result["snap"] is True and result["perms"] == {"mcp:host:fitbit-coach": "granted"}
    assert result["one"] == "granted" and result["all"]["mcp"] == result["all"]["db"] == "granted"


@NODE
def test_live_mode_maps_every_page_tool_to_its_snapshot_section():
    result = _node(PREVIEW.render(LIVE_SNAP), CALL + """
const out = {};
for (const [k, tool, args] of [["status", "data_status", {}], ["t7", "get_trends", {metrics: ["rhr"], days: 7}],
    ["t30", "get_trends", {metrics: ["rhr"], days: 30}], ["t90", "get_trends", {metrics: ["rhr"], days: 90}],
    ["t365", "get_trends", {metrics: ["rhr"], days: 365}], ["t45", "get_trends", {metrics: ["rhr"], days: 45}],
    ["sleep", "get_sleep", {days: 14}],
    ["activity", "get_activity", {days: 30}], ["running", "get_running", {days: 90}],
    ["bodyage", "get_body_age", {}], ["targets", "get_coach_notes", {status: "active"}],
    ["today", "get_overview", {date: "2026-10-18"}], ["other_day", "get_overview", {date: "2026-10-17"}],
    ["evidence", "get_evidence", {ids: ["plews2013_hrv", "nope"]}],
    ["inherited", "get_evidence", {ids: ["__proto__", "toString"]}]]) out[k] = (await call(tool, args)).payload;
return out;
""")
    s = LIVE_SNAP
    assert result["status"] == s["status"] and result["t30"] == s["trends30"] and result["t90"] == s["trends90"]
    t7 = result["t7"]  # trimmed to the requested range, from the 30-day section
    assert t7["start"] == "2026-10-12" and t7["end"] == "2026-10-18"
    assert [r["date"] for r in t7["series"]["rhr"]] == [f"2026-10-{d}" for d in range(12, 19)]
    assert t7["series"]["hrv_7d"] == s["trends30"]["series"]["hrv_7d"][-7:]
    assert result["t365"] == s["trends90"] and result["t365"]["start"] == "2026-07-21"  # all the snapshot holds
    assert result["t45"]["start"] == "2026-09-04" and len(result["t45"]["series"]["rhr"]) == 45
    assert (result["sleep"], result["activity"], result["running"], result["bodyage"], result["targets"]) == \
        (s["sleep14"], s["activity30"], s["running90"], s["bodyage"], s["targets"])
    assert result["today"] == s["overview"]
    assert "2026-10-17" in result["other_day"]["error"] and "2026-10-18" in result["other_day"]["hint"]
    ev = result["evidence"]
    assert ev["unknown"] == ["nope"] and set(ev["sources"]) == {"plews2013_hrv"}
    assert ev["sources"]["plews2013_hrv"]["citation"]
    assert result["inherited"] == {"sources": {}, "unknown": ["__proto__", "toString"]}


@NODE
def test_live_mode_rejects_unknown_tools_with_an_error_result():
    result = _node(PREVIEW.render(LIVE_SNAP), CALL + 'return await call("query_sql", {sql: "select 1"});')
    assert result["code"] == "tool_error" and result["isError"] is True and "query_sql" in result["message"]


def test_snapshot_only_flag(tmp_path):
    snap = tmp_path / "snapshot.json"
    snap.write_text(json.dumps(SNAP), encoding="utf-8")
    out = tmp_path / "index.html"
    assert PREVIEW.main([str(snap), str(out), "--snapshot-only"]) == 0
    assert "const LIVE = false;" in out.read_text(encoding="utf-8")
    assert "const LIVE = true;" in PREVIEW.render(SNAP)


@NODE
def test_a_missing_section_answers_with_an_error_payload():
    result = _node(PREVIEW.render({"as_of": "2026-10-18"}), CALL + """
return {status: (await call("data_status", {})).payload, trends: (await call("get_trends", {days: 7})).payload,
        overview: (await call("get_overview", {})).payload};
""")
    assert result["status"] == {"error": "the snapshot has no status section", "hint": "rebuild the snapshot"}
    assert result["trends"]["error"] == "the snapshot has no trends30 section"
    assert result["overview"]["error"] == "the snapshot has no overview section"


@NODE
def test_the_stub_serves_every_tool_the_template_calls():
    template = (ROOT / "src" / "fitbit_air" / "dashboard" / "template.html").read_text(encoding="utf-8")
    m = re.search(r"const SOURCES = \(\) => \(\{(.*?)\}\);", template, re.S)
    assert m, "SOURCES not found in the template"
    names = set(re.findall(r'\[\s*"([a-z_]+)"\s*,', m.group(1))) | {"get_evidence"}
    assert {"get_overview", "get_trends", "data_status"} <= names
    called = set(re.findall(r'callTool\(SERVER,\s*"([a-z_]+)"', template))
    assert called <= names  # tools named directly in the page are covered too
    result = _node(PREVIEW.render(LIVE_SNAP), CALL + """
const out = {};
for (const t of %s) {
  const r = await call(t, {ids: [], days: 30});
  out[t] = r.code || (r.payload && r.payload.error) || "ok";
}
return out;
""" % json.dumps(sorted(names)))
    assert result == {t: "ok" for t in names}


def test_render_needs_the_page_logic_script():
    with pytest.raises(ValueError, match=r"template has no page logic script \(const SERVER\)"):
        PREVIEW.render({}, template="<style></style><script>boot()</script>")
