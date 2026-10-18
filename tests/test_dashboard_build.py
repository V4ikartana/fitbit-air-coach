import json
import re
import shutil
import subprocess

import pytest

from fitbit_air.config import Paths
from fitbit_air.dashboard_build import (DASHBOARD_DEFAULTS, _embed_json, build_dashboard, load_dashboard_config,
                                        render_page)


def _config(tmp_path, text):
    paths = Paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "dashboard.toml").write_text(text)
    return paths


def test_defaults_when_no_file(tmp_path):
    cfg, problems = load_dashboard_config(Paths(tmp_path), plugin_metrics=set())
    assert cfg == DASHBOARD_DEFAULTS and problems == []


def test_validation(tmp_path):
    paths = _config(tmp_path, 'tabs = ["today", "nope"]\ndefault_range_days = 45\n'
                              'trend_metrics = ["plugin:hydration"]\n')
    _, problems = load_dashboard_config(paths, plugin_metrics=set())
    text = " ".join(p.message for p in problems)
    assert "nope" in text and "45" in text and "plugin:hydration" in text
    _, problems = load_dashboard_config(_config(tmp_path / "b", 'trend_metrics = ["plugin:hydration"]\n'),
                                        plugin_metrics={"hydration"})
    assert problems == []


def test_build_embeds_config_and_theme_and_stays_self_contained(tmp_path):
    paths = _config(tmp_path, 'tabs = ["today", "sleep"]\n[theme]\ndark = {recovery = "#00ff88"}\n')
    out = build_dashboard(paths, plugin_metrics=set())
    html = out.read_text()
    cfg = json.loads(re.search(r'<script type="application/json" id="fa-config">(.*?)</script>', html, re.S).group(1))
    assert cfg["tabs"] == ["today", "sleep"]
    assert "--recovery: #00ff88" in html
    hosts = set(re.findall(r'(?:src|href)="(https?://[^/"]+)', html))
    assert hosts <= {"https://fonts.googleapis.com"}
    assert out == paths.data_dir / "dashboard" / "dashboard.html"


def test_build_refuses_invalid_config(tmp_path):
    with pytest.raises(ValueError, match="nope"):
        build_dashboard(_config(tmp_path, 'tabs = ["nope"]\n'), plugin_metrics=set())


def test_theme_colour_must_be_hex(tmp_path):
    _, problems = load_dashboard_config(_config(tmp_path, '[theme]\ndark = {recovery = "red; } body {"}\n'),
                                        plugin_metrics=set())
    assert problems and "colour" in problems[0].message


def test_build_with_defaults_and_custom_out(tmp_path):
    paths = Paths(tmp_path)
    out = build_dashboard(paths, plugin_metrics=set(), out=tmp_path / "x" / "page.html")
    html = out.read_text()
    assert out == tmp_path / "x" / "page.html" and 'id="fa-config"' in html and 'id="fa-theme"' not in html
    # the embedded config sits after the template's own style block so a theme override wins
    assert html.index('id="fa-config"') > html.index("</style>")


def test_build_cannot_be_broken_out_of_by_config_text(tmp_path):
    # a list value is validated against the allow-list, so injected markup never reaches the page
    _, problems = load_dashboard_config(_config(tmp_path, 'tabs = ["</script><script>alert(1)"]\n'), set())
    assert problems and "alert" in problems[0].message


def test_wrong_types_are_problems_not_crashes(tmp_path):
    for text in ('tabs = 3\n', 'theme = 5\n', '[theme]\ndark = 4\n', 'default_range_days = "30"\n',
                 'trend_metrics = [["rhr"]]\n'):
        cfg, problems = load_dashboard_config(_config(tmp_path / str(abs(hash(text))), text), set())
        assert problems, text
        assert cfg == DASHBOARD_DEFAULTS


def _template():
    from importlib.resources import files
    return files("fitbit_air.dashboard").joinpath("template.html").read_text(encoding="utf-8")


def test_template_ships_as_package_data():
    html = _template()
    assert 'id="fa-config"' not in html.split("<script>")[0]  # the builder adds it; the bare template has none
    assert "fa:cfg-start" in html and "fa:cfg-end" in html


def test_embed_json_cannot_close_the_script_element():
    text = _embed_json({"x": "</script><!-- <script>"})
    assert "<" not in text and json.loads(text) == {"x": "</script><!-- <script>"}


def test_build_embeds_plugin_units_and_descriptions(tmp_path):
    paths = _config(tmp_path, 'trend_metrics = ["plugin:water"]\n')
    meta = {"water": {"unit": "l", "description": "Litres of water"}}
    html = build_dashboard(paths, meta).read_text()
    cfg = json.loads(re.search(r'id="fa-config">(.*?)</script>', html, re.S).group(1))
    assert cfg["plugin_meta"] == meta and cfg["trend_metrics"] == ["plugin:water"]
    # a plain set of names still works and embeds no metadata
    other = build_dashboard(Paths(tmp_path / "b"), {"water"}, out=tmp_path / "b.html").read_text()
    assert '"plugin_meta"' not in re.search(r'id="fa-config">(.*?)</script>', other, re.S).group(1)


HOSTILE = [
    {"tabs": 3, "today_cards": "plan", "trend_metrics": {"a": 1}, "default_range_days": "30", "plugin_meta": 5},
    {"tabs": ["nope", 5, None, "sleep", "sleep"], "today_cards": [1, "plan"], "trend_metrics": [],
     "default_range_days": 45, "plugin_meta": {"x": 7, "y": {"unit": 3, "description": ["z"]}}},
    {"tabs": [], "trend_metrics": [["rhr"]], "default_range_days": None, "plugin_meta": []},
    ["not", "an", "object"], None, "text", 7,
]


def test_template_defends_against_a_bad_embedded_config():
    # The builder validates, but the page must also survive a hand-edited or corrupted fa-config.
    for raw in HOSTILE:
        html = render_page(raw if isinstance(raw, dict) else {"tabs": raw})
        assert 'id="fa-config"' in html
    js = _template()
    for needle in ("const strs", "isObj(", "const faConfig", "TODAY_CARDS", "RANGE0", "[7, 30, 90, 365].includes",
                   "if (!TABS.includes(tab)) tab = TABS[0]", "const linked = TABS.includes(tab)", "const own ="):
        assert needle in js, needle
    assert "CFG.today_cards" not in js and "CFG.trend_metrics" not in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_config_normaliser_runs_with_bad_inputs():
    """Run the page's own normaliser (the fa:cfg block) under node against hostile configs."""
    html = _template()
    block = html[html.index("// fa:cfg-start"):html.index("// fa:cfg-end")]
    script = block + "\nconsole.log(JSON.stringify(JSON.parse(process.argv[1]).map(faConfig)));"
    out = subprocess.run(["node", "-e", script, json.dumps(HOSTILE)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    results = json.loads(out.stdout)
    default_tabs = ["today", "trends", "sleep", "activity", "running", "bodyage"]
    default_trend = ["rhr", "hrv_7d", "sleep_debt_h", "steps_7d", "mvpa_week", "sedentary_h", "weight_trend_kg",
                     "score:recovery", "score:sleep"]
    for r in results:
        assert r["range"] in (7, 30, 90, 365) and r["tabs"] and set(r["tabs"]) <= set(default_tabs)
        assert r["trend"] and all(isinstance(m, str) for m in r["trend"])
        assert r["cards"] is None or all(isinstance(c, str) for c in r["cards"])
        assert all(isinstance(m["unit"], str) and isinstance(m["desc"], str) for m in r["pluginMeta"].values())
    assert results[0]["tabs"] == default_tabs and results[0]["trend"] == default_trend and results[0]["range"] == 30
    assert results[1]["tabs"] == ["sleep"] and results[1]["cards"] == ["plan"] and results[1]["range"] == 30
    assert results[1]["pluginMeta"]["y"] == {"unit": "", "desc": ""}
    assert results[2]["tabs"] == default_tabs and results[2]["trend"] == default_trend
    good = json.loads(subprocess.run(
        ["node", "-e", block + "\nconsole.log(JSON.stringify(faConfig(JSON.parse(process.argv[1]))));",
         json.dumps({"tabs": ["sleep", "today"], "trend_metrics": ["plugin:w"], "default_range_days": 365,
                     "today_cards": ["plan"], "plugin_meta": {"w": {"unit": "l", "description": "d"}}})],
        capture_output=True, text=True, timeout=30).stdout)
    assert good == {"tabs": ["sleep", "today"], "cards": ["plan"], "trend": ["plugin:w"], "range": 365,
                    "pluginMeta": {"w": {"unit": "l", "desc": "d"}}}
