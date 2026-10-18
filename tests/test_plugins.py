import json
import textwrap
from datetime import date

import anyio
from mcp import Client
from typer.testing import CliRunner

from analysis_seed import history
from coach_seed import seed_analysis
from fitbit_air import cli
from fitbit_air.analysis.runner import run_analysis
from fitbit_air.config import Paths, get_paths
from fitbit_air.plugins import DayContext, LoadedPlugins, load_plugins, run_daily_plugins
from fitbit_air.store import Store

GOOD = textwrap.dedent('''
    def register(api):
        api.metric("rhr_x2", lambda day, ctx: (ctx.daily("resting_hr") or 0) * 2, unit="bpm", description="test")
        api.finding("high_rhr", lambda day, ctx: {"severity": "info", "title": "RHR seen", "detail": day}
                    if ctx.daily("resting_hr") else None, evidence_note="test rule")
        api.coach_tool("echo", lambda ctx, word="hi": {"word": word}, description="Echo a word.")
''')


def _home(tmp_path, files: dict[str, str], enabled=True):
    paths = Paths(tmp_path)
    paths.plugins_dir.mkdir(parents=True)
    for name, text in files.items():
        (paths.plugins_dir / name).write_text(text)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    flag = str(enabled).lower()
    paths.profile_file.write_text(
        f'sex = "male"\nrace_date = "2027-02-07"\nrace_distance_km = 10.0\n[plugins]\nenabled = {flag}\n')
    return paths


def _names(loaded):
    return {p.file.rsplit("/", 1)[-1] for p in loaded.problems}


def test_disabled_by_default(tmp_path):
    loaded = load_plugins(_home(tmp_path, {"good.py": GOOD}, enabled=False), enabled=False)
    assert loaded.metric_names() == set() and loaded.problems == []


def test_load_and_isolation(tmp_path):
    paths = _home(tmp_path, {"good.py": GOOD, "broken.py": "def register(api):\n    raise RuntimeError('boom')\n",
                             "syntax.py": "def (:\n"})
    loaded = load_plugins(paths, enabled=True)
    assert loaded.metric_names() == {"rhr_x2"} and set(loaded.tools) == {"echo"}
    assert loaded.evidence_note("high_rhr") == "test rule"
    assert _names(loaded) == {"broken.py", "syntax.py"}


def test_import_time_exit_and_missing_register_are_isolated(tmp_path):
    loaded = load_plugins(_home(tmp_path, {"good.py": GOOD, "quit.py": "import sys\nsys.exit(3)\n",
                                           "noreg.py": "x = 1\n"}), enabled=True)
    assert loaded.metric_names() == {"rhr_x2"} and _names(loaded) == {"quit.py", "noreg.py"}


def test_a_failing_plugin_leaves_nothing_half_registered(tmp_path):
    half = textwrap.dedent('''
        def register(api):
            api.metric("half", lambda day, ctx: 1.0)
            api.finding("bad name!", lambda day, ctx: None, evidence_note="x")
    ''')
    loaded = load_plugins(_home(tmp_path, {"half.py": half, "good.py": GOOD}), enabled=True)
    assert "half" not in loaded.metrics and _names(loaded) == {"half.py"}
    assert loaded.metric_names() == {"rhr_x2"}


def test_duplicate_names_across_files_skip_the_second_file(tmp_path):
    loaded = load_plugins(_home(tmp_path, {"a_good.py": GOOD, "b_dupe.py": GOOD}), enabled=True)
    assert _names(loaded) == {"b_dupe.py"} and loaded.metrics["rhr_x2"]["file"].endswith("a_good.py")


def test_coach_tool_signature_is_checked_at_load(tmp_path):
    bad = textwrap.dedent('''
        def register(api):
            api.coach_tool("noctx", lambda: {}, description="x")
        ''')
    posonly = textwrap.dedent('''
        def register(api):
            api.coach_tool("posonly", lambda ctx, a, /: {}, description="x")
        ''')
    loaded = load_plugins(_home(tmp_path, {"noctx.py": bad, "posonly.py": posonly}), enabled=True)
    assert loaded.tools == {} and _names(loaded) == {"noctx.py", "posonly.py"}


def test_a_missing_or_unreadable_plugins_dir_is_not_an_error(tmp_path):
    paths = Paths(tmp_path)
    assert load_plugins(paths, enabled=True).problems == []
    paths.plugins_dir.parent.mkdir(parents=True, exist_ok=True)
    paths.plugins_dir.write_text("a file, not a folder")
    assert load_plugins(paths, enabled=True).problems == []


def test_dataclass_plugin_with_postponed_annotations_loads(tmp_path):
    src = textwrap.dedent('''
        from __future__ import annotations
        from dataclasses import dataclass

        @dataclass
        class Reading:
            day: str
            value: float

        def register(api):
            api.metric("dc", lambda day, ctx: Reading(day, 2.0).value)
    ''')
    loaded = load_plugins(_home(tmp_path, {"dc.py": src}), enabled=True)
    assert loaded.problems == [] and loaded.metric_names() == {"dc"}
    import sys
    assert "fitbit_air_user_plugin_dc" in sys.modules
    sys.modules.pop("fitbit_air_user_plugin_dc")


def test_failed_plugin_modules_are_not_left_in_sys_modules(tmp_path):
    import sys
    bad = "def register(api):\n    api.metric('m', lambda d, c: 1.0)\n    raise RuntimeError('late')\n"
    loaded = load_plugins(_home(tmp_path, {"late.py": bad, "syn.py": "def (:\n"}), enabled=True)
    assert len(loaded.problems) == 2
    assert not [m for m in sys.modules if m.startswith("fitbit_air_user_plugin_late")
                or m.startswith("fitbit_air_user_plugin_syn")]


def test_plugin_printing_at_import_leaves_stdout_clean(tmp_path, capsys):
    src = "print('hello from import')\ndef register(api):\n    print('and from register')\n"
    loaded = load_plugins(_home(tmp_path, {"chatty.py": src}), enabled=True)
    captured = capsys.readouterr()
    assert loaded.problems == [] and captured.out == "" and "hello from import" in captured.err


def test_day_context_is_read_only_copy():
    metrics, daily = {"rhr": 60.0}, {"resting_hr": 61.0}
    ctx = DayContext(metrics, daily)
    metrics["rhr"] = 99.0
    assert ctx.metric("rhr") == 60.0 and ctx.daily("resting_hr") == 61.0 and ctx.metric("nope") is None


def _loaded(**fns):
    loaded = LoadedPlugins()
    for name, fn in fns.items():
        loaded.findings[name] = {"fn": fn, "evidence_note": "n", "file": "f.py"}
    return loaded


def test_finding_severity_alert_is_rejected_and_info_watch_pass():
    ctx = DayContext({}, {})
    loaded = _loaded(
        alert=lambda d, c: {"severity": "alert", "title": "x"},
        safety=lambda d, c: {"severity": "safety", "title": "x"},
        info=lambda d, c: {"severity": "info", "title": "i", "detail": "d" * 5000},
        watch=lambda d, c: {"severity": "watch", "title": "w"},
        notdict=lambda d, c: ["info"],
        quiet=lambda d, c: None)
    _, insights, problems = run_daily_plugins(loaded, "2026-10-01", ctx)
    assert sorted(i["severity"] for i in insights) == ["info", "watch"]
    assert max(len(i["detail"]) for i in insights) == 2000
    assert all(i["kind"] == "plugin" and i["evidence_ids"][0].startswith("plugin:") for i in insights)
    assert len(problems) == 3 and any("alert" in p for p in problems)


def test_metric_values_are_validated():
    loaded = LoadedPlugins()
    for name, fn in {"ok": lambda d, c: 3, "none": lambda d, c: None, "nan": lambda d, c: float("nan"),
                     "inf": lambda d, c: float("inf"), "str": lambda d, c: "5", "bool": lambda d, c: True,
                     "boom": lambda d, c: 1 / 0}.items():
        loaded.metrics[name] = {"fn": fn, "unit": "", "description": "", "file": "f.py"}
    metrics, _, problems = run_daily_plugins(loaded, "2026-10-01", DayContext({}, {}))
    assert [m["metric"] for m in metrics] == ["plugin:ok"] and metrics[0]["value"] == 3.0
    assert len(problems) == 5


def test_analysis_writes_plugin_metric_and_finding(tmp_path):
    paths = _home(tmp_path, {"good.py": GOOD, "bad_metric.py": textwrap.dedent('''
        def register(api):
            api.metric("nan", lambda day, ctx: float("nan"))
            api.metric("raises", lambda day, ctx: 1 / 0)
    ''')})
    store = Store(paths.db)
    try:
        ds = history(store, "2026-09-01", 10, rhr=60)
        run_analysis(store, paths, date.fromisoformat(ds[0]))
        day = store.analysis_for_day(ds[-1])
        metrics = {m["metric"]: m["value"] for m in day["metrics"]}
        assert metrics["plugin:rhr_x2"] == 120.0 and "plugin:nan" not in metrics and "plugin:raises" not in metrics
        plugin_insights = [i for i in day["insights"] if i["kind"] == "plugin"]
        assert plugin_insights and plugin_insights[0]["evidence_ids"] == ["plugin:high_rhr"]
        assert "plugin" in (store.last_analysis()["summary"] or "")
    finally:
        store.close()


def test_plugin_metric_named_like_a_core_metric_does_not_touch_the_core_row(tmp_path):
    src = "def register(api):\n    api.metric('rhr', lambda day, ctx: 1.0)\n"
    paths = _home(tmp_path, {"clash.py": src})
    store = Store(paths.db)
    try:
        ds = history(store, "2026-09-01", 10, rhr=60)
        run_analysis(store, paths, date.fromisoformat(ds[0]))
        rows = {m["metric"]: m["value"] for m in store.analysis_for_day(ds[-1])["metrics"]}
        assert rows["plugin:rhr"] == 1.0 and rows["rhr"] == 60.0
    finally:
        store.close()


def test_slow_plugin_is_cut_off_after_its_time_budget(tmp_path, monkeypatch):
    from fitbit_air.analysis import daily
    from fitbit_air import plugins as plugins_mod
    store = Store(tmp_path / "h.db")
    try:
        now = [0.0]
        monkeypatch.setattr(plugins_mod, "_clock", lambda: now[0])
        calls = {"slow": 0, "fast": 0}

        def slow(day, ctx):
            calls["slow"] += 1
            now[0] += 2.0  # pretend each day costs two seconds
            return 1.0

        def fast(day, ctx):
            calls["fast"] += 1
            return 2.0
        loaded = LoadedPlugins()
        loaded.metrics["slow"] = {"fn": slow, "unit": "", "description": "", "file": "/p/slow.py"}
        loaded.metrics["fast"] = {"fn": fast, "unit": "", "description": "", "file": "/p/fast.py"}
        metrics, _, problems = daily._plugin_rows(store, loaded, [], date(2026, 9, 1), date(2026, 9, 10))
        assert daily.PLUGIN_BUDGET_S == 5.0
        assert calls == {"slow": 3, "fast": 10}  # 2 + 2 + 2 s: over 5 s after the third day
        assert sum(m["metric"] == "plugin:slow" for m in metrics) == 3
        assert sum(m["metric"] == "plugin:fast" for m in metrics) == 10
        assert len(problems) == 1 and "slow.py" in problems[0] and "budget" in problems[0]
    finally:
        store.close()


def test_run_records_load_and_run_problems_but_succeeds(tmp_path):
    paths = _home(tmp_path, {"good.py": GOOD, "broken.py": "raise RuntimeError('boom')\n",
                             "raises.py": "def register(api):\n    api.metric('r', lambda d, c: 1 / 0)\n"})
    store = Store(paths.db)
    try:
        ds = history(store, "2026-09-01", 10)
        summary = run_analysis(store, paths, date.fromisoformat(ds[0]))
        assert summary.plugin_problems and "plugin problems:" in str(summary)
        error = store.last_analysis()["error"]
        assert "plugin: " in error and "broken.py" in error and "metric r" in error
        assert error.count("metric r") == 1  # one line per distinct problem, not one per analysed day
        assert store.analysis_for_day(ds[-1])["metrics"]  # the core analysis was still written
    finally:
        store.close()


def test_a_plugin_cannot_break_the_analysis(tmp_path, monkeypatch):
    paths = _home(tmp_path, {"good.py": GOOD})
    store = Store(paths.db)
    try:
        ds = history(store, "2026-09-01", 10)

        def explode(*a, **k):
            raise RuntimeError("db gone")
        monkeypatch.setattr(store, "daily_values_between", explode)
        summary = run_analysis(store, paths, date.fromisoformat(ds[0]))
        metrics = {m["metric"] for m in store.analysis_for_day(ds[-1])["metrics"]}
        assert "rhr" in metrics and "plugin:rhr_x2" not in metrics
        assert any("db gone" in p for p in summary.plugin_problems)
    finally:
        store.close()


def test_plugins_off_means_no_plugin_rows(tmp_path):
    paths = _home(tmp_path, {"good.py": GOOD}, enabled=False)
    store = Store(paths.db)
    try:
        ds = history(store, "2026-09-01", 10)
        summary = run_analysis(store, paths, date.fromisoformat(ds[0]))
        assert not [m for m in store.analysis_for_day(ds[-1])["metrics"] if m["metric"].startswith("plugin:")]
        assert summary.plugin_problems == [] and "plugin" not in str(summary)
    finally:
        store.close()


def test_daily_values_between(tmp_path):
    store = Store(tmp_path / "h.db")
    try:
        history(store, "2026-09-01", 5, rhr=61)
        rows = store.daily_values_between("2026-09-02", "2026-09-03")
        assert {r[0] for r in rows} == {"2026-09-02", "2026-09-03"}
        assert ("2026-09-02", "resting_hr", 61.0) in rows
    finally:
        store.close()


def _server(tmp_path, monkeypatch, files, enabled=True):
    _home(tmp_path, files, enabled)
    monkeypatch.setenv("FITBIT_AIR_HOME", str(tmp_path))
    s = Store(get_paths().db)
    seed_analysis(s)
    s.close()
    from fitbit_air.coach.server import build_server
    return build_server()


def test_plugin_coach_tool_exposed_read_only(tmp_path, monkeypatch):
    srv = _server(tmp_path, monkeypatch, {"good.py": GOOD})

    async def go():
        async with Client(srv) as c:
            tools_ = {t.name: t for t in (await c.list_tools()).tools}
            out = json.loads((await c.call_tool("plugin_echo", {"word": "yo"})).content[0].text)
            default = json.loads((await c.call_tool("plugin_echo", {})).content[0].text)
            return tools_, out, default
    tools_, out, default = anyio.run(go)
    t = tools_["plugin_echo"]
    assert t.annotations.read_only_hint is True and out["word"] == "yo" and default["word"] == "hi"
    assert t.description.endswith("(your plugin)") and "word" in t.input_schema["properties"]
    assert "freshness" in out  # the same wrapper as the built-in tools


def test_plugin_tool_errors_become_error_dicts(tmp_path, monkeypatch):
    src = textwrap.dedent('''
        def register(api):
            api.coach_tool("boom", lambda ctx: 1 / 0, description="x")
            api.coach_tool("quit", lambda ctx: __import__("sys").exit(2), description="x")
            api.coach_tool("listy", lambda ctx: [1, 2], description="x")
            api.coach_tool("nanny", lambda ctx: {"v": float("nan")}, description="x")
            api.coach_tool("count", lambda ctx: {"n": ctx.query("SELECT COUNT(*) FROM metric_daily")["rows"][0][0]},
                           description="x")
    ''')
    srv = _server(tmp_path, monkeypatch, {"p.py": src})

    async def go():
        async with Client(srv) as c:
            return {n: json.loads((await c.call_tool(f"plugin_{n}", {})).content[0].text)
                    for n in ("boom", "quit", "listy", "nanny", "count")}
    out = anyio.run(go)
    for n in ("boom", "quit", "listy", "nanny"):
        assert "error" in out[n], n
    assert out["count"]["n"] > 0  # a good tool still gets a working ctx


def test_plugin_tools_get_a_read_only_facade_not_the_store(tmp_path, monkeypatch):
    src = textwrap.dedent('''
        def register(api):
            api.coach_tool("probe", lambda ctx: {"attrs": sorted(a for a in dir(ctx) if not a.startswith("_")),
                                                 "today": str(ctx.today), "has_store": hasattr(ctx, "store"),
                                                 "has_paths": hasattr(ctx, "paths")}, description="x")
            api.coach_tool("wipe", lambda ctx: ctx.query("DELETE FROM metric_daily"), description="x")
            api.coach_tool("two", lambda ctx: ctx.query("SELECT 1; SELECT 2"), description="x")
            api.coach_tool("sel", lambda ctx: ctx.query("SELECT metric FROM metric_daily LIMIT 2"), description="x")
    ''')
    srv = _server(tmp_path, monkeypatch, {"p.py": src})
    before = _count_metric_rows()

    async def go():
        async with Client(srv) as c:
            return {n: json.loads((await c.call_tool(f"plugin_{n}", {})).content[0].text)
                    for n in ("probe", "wipe", "two", "sel")}
    out = anyio.run(go)
    assert out["probe"]["attrs"] == ["now", "query", "today"] and not out["probe"]["has_store"]
    assert not out["probe"]["has_paths"]
    assert "error" in out["wipe"] and "error" in out["two"] and len(out["sel"]["rows"]) == 2
    assert _count_metric_rows() == before > 0  # the DELETE changed nothing


def _count_metric_rows():
    s = Store(get_paths().db)
    try:
        return s.count_rows("metric_daily")
    finally:
        s.close()


def test_plugin_tools_and_evidence_follow_the_enable_flag(tmp_path, monkeypatch):
    srv = _server(tmp_path, monkeypatch, {"good.py": GOOD}, enabled=False)

    async def go():
        async with Client(srv) as c:
            return {t.name for t in (await c.list_tools()).tools}
    assert not {n for n in anyio.run(go) if n.startswith("plugin_")}


def test_plugin_evidence_and_load_problems_reach_the_coach(tmp_path, monkeypatch):
    srv = _server(tmp_path, monkeypatch, {"good.py": GOOD, "broken.py": "raise RuntimeError('boom')\n"})

    async def go():
        async with Client(srv) as c:
            ev = json.loads((await c.call_tool("get_evidence", {"ids": ["plugin:high_rhr"]})).content[0].text)
            st = json.loads((await c.call_tool("data_status", {})).content[0].text)
            return ev, st
    ev, st = anyio.run(go)
    assert ev["sources"]["plugin:high_rhr"]["note"] == "test rule"
    assert any("broken.py" in p and "at server start" in p for p in st["config_problems"])


def test_plugin_tool_with_varargs_and_keyword_only_params(tmp_path, monkeypatch):
    # *rest and **kw are not tool arguments; a keyword-only parameter with a default is
    src = textwrap.dedent('''
        def register(api):
            api.coach_tool("odd", lambda ctx, a, *rest, b=2, **kw: {"a": a, "b": b}, description="x")
    ''')
    srv = _server(tmp_path, monkeypatch, {"odd.py": src, "good.py": GOOD})

    async def go():
        async with Client(srv) as c:
            tools_ = {t.name: t for t in (await c.list_tools()).tools}
            out = json.loads((await c.call_tool("plugin_odd", {"a": 1})).content[0].text)
            return tools_, out
    tools_, out = anyio.run(go)
    assert set(tools_["plugin_odd"].input_schema["properties"]) == {"a", "b"} and out["a"] == 1
    assert "plugin_echo" in tools_


runner = CliRunner()


def _cli_home(tmp_path, monkeypatch, files, enabled=True):
    _home(tmp_path, files, enabled)
    monkeypatch.setenv("FITBIT_AIR_HOME", str(tmp_path))
    monkeypatch.setattr(cli.auth_mod, "auth_state", lambda: "not logged in")


def test_status_reports_plugins_and_problems(tmp_path, monkeypatch):
    _cli_home(tmp_path, monkeypatch, {"good.py": GOOD, "broken.py": "raise RuntimeError('boom')\n"})
    Store(get_paths().db).close()
    r = runner.invoke(cli.app, ["status"])
    assert r.exit_code == 0, r.output
    assert "Plugins: 1 loaded, 1 problem" in r.output and "broken.py" in r.output


def test_status_is_silent_about_plugins_when_disabled(tmp_path, monkeypatch):
    _cli_home(tmp_path, monkeypatch, {"good.py": GOOD}, enabled=False)
    Store(get_paths().db).close()
    r = runner.invoke(cli.app, ["status"])
    assert r.exit_code == 0 and "Plugins:" not in r.output


def test_dashboard_build_command(tmp_path, monkeypatch):
    _cli_home(tmp_path, monkeypatch, {"good.py": GOOD})
    (get_paths().config_dir / "dashboard.toml").write_text('trend_metrics = ["plugin:rhr_x2", "rhr"]\n')
    r = runner.invoke(cli.app, ["dashboard", "build"])
    assert r.exit_code == 0, r.output
    out = get_paths().data_dir / "dashboard" / "dashboard.html"
    assert out.exists() and "plugin:rhr_x2" in out.read_text() and str(out) in r.output


def test_dashboard_build_reports_a_bad_config_and_unknown_plugin_metric(tmp_path, monkeypatch):
    _cli_home(tmp_path, monkeypatch, {}, enabled=False)
    (get_paths().config_dir / "dashboard.toml").write_text('trend_metrics = ["plugin:rhr_x2"]\n')
    r = runner.invoke(cli.app, ["dashboard", "build"])
    assert r.exit_code == 1 and "plugin:rhr_x2" in r.output
    other = tmp_path / "elsewhere.html"
    (get_paths().config_dir / "dashboard.toml").write_text("")
    r = runner.invoke(cli.app, ["dashboard", "build", "--out", str(other)])
    assert r.exit_code == 0 and other.exists()
