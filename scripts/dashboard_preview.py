"""Write a preview of the real dashboard that opens in any browser.

The page normally runs inside Claude, which provides `window.claude`. This adds a small stand-in host that serves a
snapshot file through `db`. By default (live mode) it also plays a connected fitbit-coach server and grants the
page's permissions: each tool the page calls is answered from the matching snapshot section, get_evidence from the
bundled evidence source list, and any other tool rejects. The page then shows its normal live state. With
--snapshot-only it offers no server, so the page falls back to the stored snapshot ("Snapshot", with the no-live-data
notice). If a real host is present, the stand-in stays off. Usage:

    uv run python scripts/dashboard_preview.py SNAPSHOT.json OUT.html [--theme dark|light] [--snapshot-only]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from fitbit_air.analysis.evidence import load_registry
from fitbit_air.coach.tools import evidence_source
from fitbit_air.dashboard_build import DASHBOARD_DEFAULTS, _embed_json, render_page

# Results are shaped as the host's callTool resolves them (content, structuredContent, payload); a tool the page
# never calls rejects with code "tool_error" and the isError envelope on `.result`, as the host does. `use()` resolves
# null for anything not served, as the host does. get_trends trims the 30-day section (up to 30 days) or the 90-day
# one (31-90 days) to the requested range; beyond 90 days it returns the 90-day section, all the snapshot holds.
STUB = """<script type="application/json" id="fa-preview-snapshot">{snap}</script>
<script type="application/json" id="fa-preview-evidence">{evidence}</script>
<script id="fa-preview-host">
(() => {{
  if (window.claude) {{ console.warn("fa-preview: a real host is present; the stand-in stays off"); return; }}
  const LIVE = {live};
  const read = (id) => JSON.parse(document.getElementById(id).textContent);
  const snap = read("fa-preview-snapshot"), sources = read("fa-preview-evidence");
  const ok = (v) => ({{content: [{{type: "text", text: JSON.stringify(v)}}], structuredContent: v, payload: v}});
  const failure = (server, message) => Object.assign(new Error(message), {{code: "tool_error", server, message,
    result: {{content: [{{type: "text", text: message}}], isError: true}}}});
  const has = (key) => Object.hasOwn(snap, key) && snap[key] != null;
  const sectionOf = (key) => has(key) ? snap[key]
    : {{error: "the snapshot has no " + key + " section", hint: "rebuild the snapshot"}};
  const isoAdd = (iso, days) => new Date(Date.parse(iso + "T00:00:00Z") + days * 86400000).toISOString().slice(0, 10);
  function trends(args) {{
    const days = Math.max(1, Math.floor(Number(args.days)) || 30);
    const key = days > 30 ? "trends90" : "trends30";
    const t = sectionOf(key);
    if (days > 90 || t.error || typeof t.end !== "string" || !t.series || typeof t.series !== "object") return t;
    const start = isoAdd(t.end, -(days - 1));
    const series = Object.fromEntries(Object.entries(t.series).map(([m, rows]) => [m, Array.isArray(rows)
      ? rows.filter((r) => !r || typeof r.date !== "string" || r.date >= start) : rows]));
    return {{...t, start, series}};
  }}
  const SECTION = {{data_status: "status", get_sleep: "sleep14", get_activity: "activity30", get_running: "running90",
                   get_body_age: "bodyage", get_coach_notes: "targets"}};
  function answer(tool, args) {{
    if (tool === "get_overview") {{
      if (args.date && args.date !== snap.as_of)
        return {{error: "no overview for " + args.date + " in the preview",
                hint: "the preview holds only " + snap.as_of}};
      return sectionOf("overview");
    }}
    if (tool === "get_trends") return trends(args);
    if (tool === "get_evidence") {{
      const ids = [].concat(args.ids || []);
      const known = (i) => typeof i === "string" && Object.hasOwn(sources, i);
      return {{sources: Object.fromEntries(ids.filter(known).map((i) => [i, sources[i]])),
              unknown: ids.filter((i) => !known(i))}};
    }}
    return Object.hasOwn(SECTION, tool) ? sectionOf(SECTION[tool]) : undefined;
  }}
  const mcp = {{callTool: async (server, tool, args) => {{
    const v = answer(tool, args || {{}});
    if (v === undefined) throw failure(server, "Unknown tool: " + tool + " (not served by the preview)");
    return ok(v);
  }}}};
  const GRANTED = {{db: "granted", mcp: "granted"}};
  const permissions = {{state: async (name) => name === undefined ? {{...GRANTED}} : "granted",
    request: async (names) => Object.fromEntries([].concat(names || []).map((n) => [n, "granted"])),
    manage: async () => {{}}}};
  const db = {{doc: (path) => ({{get: async () => ({{exists: path === "snapshot/latest", data: () => snap}})}})}};
  window.claude = {{use: async (name) => {{
    if (name === "db") return db;
    if (LIVE && name === "mcp") return mcp;
    if (LIVE && name === "permissions") return permissions;
    return null;
  }}}};
  console.info("fa-preview: stand-in host serving a snapshot");
}})();
</script>
"""
# The template is a page fragment (Claude adds the document skeleton when it publishes), so the preview adds one.
HEAD = '<!doctype html>\n<html lang="en"{theme}>\n<meta charset="utf-8">\n' \
       '<meta name="viewport" content="width=device-width, initial-scale=1">\n'


def _evidence(snapshot: dict) -> dict:
    """The bundled citations for every evidence id in the snapshot, shaped as get_evidence returns them."""
    ids: set[str] = set()

    def walk(o) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "evidence_ids" and isinstance(v, list):
                    ids.update(i for i in v if isinstance(i, str))
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(snapshot)
    reg = load_registry()
    return {i: evidence_source(reg, i) for i in sorted(ids) if reg.has(i)}


def render(snapshot: dict, theme: str | None = None, live: bool = True, template: str | None = None) -> str:
    html = render_page(json.loads(json.dumps(DASHBOARD_DEFAULTS)), template)
    stub = STUB.format(snap=_embed_json(snapshot), evidence=_embed_json(_evidence(snapshot)),
                       live="true" if live else "false")
    logic = html.find("const SERVER")
    i = html.rfind("<script", 0, logic) if logic >= 0 else -1  # just before the page logic script
    if i < 0:
        raise ValueError("template has no page logic script (const SERVER)")
    html = html[:i] + stub + html[i:]
    # The page reads its colours from :root[data-theme=...] and never sets the attribute itself.
    forced = f' data-theme="{theme}"' if theme in ("dark", "light") else ""
    return HEAD.format(theme=forced) + html + "\n</html>\n"


def embedded_snapshot(html: str) -> str:
    m = re.search(r'<script type="application/json" id="fa-preview-snapshot">(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("no embedded snapshot in this page")
    return m.group(1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("snapshot", help="a snapshot JSON file (the `snapshot` part of make_demo_home.py --facts)")
    ap.add_argument("out", help="the HTML file to write")
    ap.add_argument("--theme", choices=("dark", "light"), help="force a theme (default: follow the system)")
    ap.add_argument("--snapshot-only", action="store_true",
                    help="offer no live server, so the page shows its snapshot fallback")
    args = ap.parse_args(argv)
    snap = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(snap, args.theme, live=not args.snapshot_only), encoding="utf-8")
    print(f"preview written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
