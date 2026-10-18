import importlib.metadata
import json
import logging
import sys
import threading
from datetime import date, timedelta
from logging.handlers import RotatingFileHandler

import anyio
import pytest
from mcp import Client

from coach_seed import seed_analysis
from fitbit_air.coach import actions, prompts, server
from fitbit_air.coach.server import build_server
from fitbit_air.config import get_paths
from fitbit_air.store import Store

TOOLS = {"get_overview", "get_trends", "get_sleep", "get_activity", "get_running", "get_body_age", "get_evidence",
         "data_status", "describe_schema", "query_sql", "log_journal", "get_journal", "save_coach_note",
         "get_coach_notes", "update_coach_note", "set_profile", "refresh_data"}
PROMPTS = {"morning_checkin", "weekly_review", "plan_next_run", "explain_score"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FITBIT_AIR_HOME", str(tmp_path))
    s = Store(get_paths().db)
    seed_analysis(s, end="2026-10-17")
    s.close()
    return tmp_path


def _run(coro_fn):
    return anyio.run(coro_fn)


def test_lists_tools_prompts_and_instructions(home):
    async def go():
        async with Client(build_server()) as c:
            tools = {t.name for t in (await c.list_tools()).tools}
            prompts = {p.name for p in (await c.list_prompts()).prompts}
            return tools, prompts, c.instructions
    tools, prompts, instructions = _run(go)
    assert tools == TOOLS and prompts == PROMPTS
    assert "Core rules" in instructions and "data_status" in instructions and "10%" in instructions
    assert "local only" in instructions and "never invent" in instructions.lower()


def test_tool_calls_return_json(home):
    async def go():
        async with Client(build_server()) as c:
            overview = await c.call_tool("get_overview", {"date": "2026-10-17"})
            logged = await c.call_tool("log_journal", {"date": "2026-10-17", "feeling": 4})
            return json.loads(overview.content[0].text), json.loads(logged.content[0].text)
    overview, logged = _run(go)
    assert overview["safety"][0]["title"] == "Resting HR high" and "freshness" in overview
    assert logged["entry"]["feeling"] == 4


def test_set_profile_accepts_numbers_and_lists(home, monkeypatch):
    monkeypatch.setattr(actions.runner, "run_analysis", lambda store, paths: "analysed")

    async def go():
        async with Client(build_server()) as c:
            num = await c.call_tool("set_profile", {"key": "sleep_need_h", "value": 8})
            goals = await c.call_tool("set_profile", {"key": "goals", "value": ["sleep", "running_10k"]})
            tool = next(t for t in (await c.list_tools()).tools if t.name == "set_profile")
            return json.loads(num.content[0].text), json.loads(goals.content[0].text), tool.description
    num, goals, description = _run(go)
    assert num["profile"]["sleep_need_h"] == 8 and goals["profile"]["goals"] == ["sleep", "running_10k"]
    assert "goals" in description and "number" in description


def test_prompt_text(home):
    async def go():
        async with Client(build_server()) as c:
            return (await c.get_prompt("weekly_review", {"week_ending": "2026-10-19"})).messages[0].content.text
    text = _run(go)
    assert "2026-10-19" in text and "save_coach_note" in text and "2026-10-26" in text


def test_morning_checkin_names_today_and_yesterday(home):
    async def go():
        async with Client(build_server()) as c:
            return (await c.get_prompt("morning_checkin", {})).messages[0].content.text
    text = _run(go)
    today = date.today()
    assert f'due_by="{today}"' in text and f'start="{today - timedelta(days=1)}"' in text


def test_morning_checkin_text_uses_the_given_day():
    text = prompts.morning_checkin(date(2026, 10, 1))
    assert 'due_by="2026-10-01"' in text and 'start="2026-09-30", end="2026-09-30"' in text


def test_weekly_review_with_a_bad_date_asks_for_one(home):
    async def go():
        async with Client(build_server()) as c:
            return (await c.get_prompt("weekly_review", {"week_ending": "next friday"})).messages[0].content.text
    text = _run(go)
    assert "'next friday'" in text and "YYYY-MM-DD" in text and "ask the user" in text


def test_server_version_and_tool_descriptions(home):
    async def go():
        async with Client(build_server()) as c:
            tools = {t.name: t.description for t in (await c.list_tools()).tools}
            return c.server_info, tools
    info, descriptions = _run(go)
    assert info.name == "fitbit-coach" and info.version == importlib.metadata.version("fitbit-air")
    assert "review_on on or before" in descriptions["get_coach_notes"]
    assert "2000" in descriptions["query_sql"]
    assert all(m in descriptions["get_trends"] for m in ("hrv_7d", "bmi", "score:body_age", "race"))
    assert "SRI" in descriptions["get_sleep"] and "none" in descriptions["set_profile"]


def test_missing_database_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FITBIT_AIR_HOME", str(tmp_path / "empty"))

    async def go():
        async with Client(build_server()) as c:
            return json.loads((await c.call_tool("data_status", {})).content[0].text)
    out = _run(go)
    assert "database not found" in out["error"] and "fitbit-air sync" in out["hint"]
    assert not (tmp_path / "empty" / "data" / "health.db").exists()


def test_slow_tool_does_not_block_other_calls(home, monkeypatch):
    """A long refresh_data runs off the event loop, so data_status answers while it is still going."""
    release = threading.Event()

    def slow_refresh(ctx):
        return {"released": release.wait(timeout=5)}  # times out (False) only if the server serialised the calls
    monkeypatch.setattr(actions, "refresh_data", slow_refresh)
    results = {}

    async def go():
        async with Client(build_server()) as c:
            async def refresh():
                results["refresh"] = json.loads((await c.call_tool("refresh_data", {})).content[0].text)
            async with anyio.create_task_group() as tg:
                tg.start_soon(refresh)
                await anyio.sleep(0.2)
                with anyio.fail_after(3):
                    status = await c.call_tool("data_status", {})
                results["status"] = json.loads(status.content[0].text)
                release.set()
    _run(go)
    assert "row_counts" in results["status"] and results["refresh"] == {"released": True}


@pytest.fixture
def clean_root_logging():
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    yield root
    for h in [h for h in root.handlers if h not in before]:
        root.removeHandler(h)
        h.close()
    root.setLevel(level)


def test_build_server_has_no_logging_side_effects(home, clean_root_logging):
    before = list(clean_root_logging.handlers)
    build_server()
    assert clean_root_logging.handlers == before
    assert not get_paths().logs_dir.exists()


def test_main_logs_to_coach_log_never_stdout(home, monkeypatch, clean_root_logging):
    ran = []
    monkeypatch.setattr(server.MCPServer, "run", lambda self, transport, **kw: ran.append(transport))
    server.main()
    assert ran == ["stdio"]
    added = [h for h in clean_root_logging.handlers if isinstance(h, RotatingFileHandler)
             and h.baseFilename == str(get_paths().logs_dir / "coach.log")]
    assert len(added) == 1 and added[0].maxBytes == 1_000_000 and added[0].backupCount == 3
    assert added[0].level == logging.INFO or clean_root_logging.level == logging.INFO
    assert not any(getattr(h, "stream", None) is sys.stdout for h in clean_root_logging.handlers)
    logging.getLogger("fitbit_air.coach.test").info("hello coach log")
    added[0].flush()
    assert "hello coach log" in (get_paths().logs_dir / "coach.log").read_text()
    server.main()  # a second call doesn't stack handlers
    assert sum(1 for h in clean_root_logging.handlers if getattr(h, "_fitbit_air_coach", False)) == 1


def test_range_tools_expose_days(home):
    s = Store(get_paths().db)  # get_trends counts back from the real today, so the data must end today
    seed_analysis(s, end=date.today().isoformat())
    s.close()

    async def go():
        async with Client(build_server()) as c:
            tools_ = {t.name: t for t in (await c.list_tools()).tools}
            out = json.loads((await c.call_tool("get_trends", {"metrics": ["rhr"], "days": 3})).content[0].text)
            return tools_, out
    tools_, out = _run(go)
    for name in ("get_trends", "get_sleep", "get_activity", "get_running", "get_journal"):
        assert "days" in tools_[name].input_schema["properties"], name
    assert len(out["series"]["rhr"]) == 3


READ_TOOLS = {"get_overview", "get_trends", "get_sleep", "get_activity", "get_running", "get_body_age", "get_evidence",
              "data_status", "describe_schema", "query_sql", "get_journal", "get_coach_notes"}
WRITE_TOOLS = {"log_journal", "save_coach_note", "update_coach_note", "set_profile", "refresh_data"}


def test_tools_carry_read_only_annotations(home):
    async def go():
        async with Client(build_server()) as c:
            return {t.name: t.annotations for t in (await c.list_tools()).tools}
    annotations = _run(go)
    assert READ_TOOLS | WRITE_TOOLS == TOOLS and set(annotations) == TOOLS
    for name in READ_TOOLS:
        assert annotations[name] is not None and annotations[name].read_only_hint is True, name
    for name in WRITE_TOOLS:
        assert annotations[name] is not None and annotations[name].read_only_hint is False, name


def test_days_must_be_a_real_integer(home):
    async def go():
        async with Client(build_server()) as c:
            ok = await c.call_tool("get_trends", {"metrics": ["rhr"], "days": 3})
            bad = [await c.call_tool(name, {"days": value})
                   for name in ("get_sleep", "get_journal") for value in (True, "3", 3.0)]
            return ok.is_error, [r.is_error for r in bad]
    ok_error, bad_errors = _run(go)
    assert ok_error is False and bad_errors == [True] * 6


def test_user_persona_appears_in_instructions(home):
    (home / "config" / "coach").mkdir(parents=True)
    (home / "config" / "coach" / "persona.md").write_text("Speak like a calm sailor.")

    async def go():
        async with Client(build_server()) as c:
            return c.instructions
    text = _run(go)
    assert "calm sailor" in text and text.index("calm sailor") < text.index("Core rules")


def _prompt_dir(home):
    d = home / "config" / "coach" / "prompts"
    d.mkdir(parents=True)
    return d


def test_custom_prompt_file_is_listed_with_args_and_renders(home):
    (_prompt_dir(home) / "explain_more.md").write_text("args: score, date\nExplain {args.score} on {args.date}.")

    async def go():
        async with Client(build_server()) as c:
            listed = {p.name: p for p in (await c.list_prompts()).prompts}
            got = await c.get_prompt("explain_more", {"score": "sleep"})
            return listed, got.messages[0].content.text
    listed, text = _run(go)
    assert set(listed) == PROMPTS | {"explain_more"}
    assert {a.name for a in listed["explain_more"].arguments} == {"score", "date"}
    assert text == "Explain sleep on ."


def test_custom_prompt_file_replaces_a_built_in(home):
    (_prompt_dir(home) / "morning_checkin.md").write_text("My own brief for {today}.")

    async def go():
        async with Client(build_server()) as c:
            names = [p.name for p in (await c.list_prompts()).prompts]
            return names, (await c.get_prompt("morning_checkin", {})).messages[0].content.text
    names, text = _run(go)
    assert names.count("morning_checkin") == 1 and text == f"My own brief for {date.today()}."


def test_bad_custom_prompt_is_skipped_and_logged(home, caplog):
    (_prompt_dir(home) / "oops.md").write_text("Hello {tomorrow}")

    async def go():
        async with Client(build_server()) as c:
            return {p.name for p in (await c.list_prompts()).prompts}
    with caplog.at_level(logging.WARNING):
        names = _run(go)
    assert names == PROMPTS and "oops.md" in caplog.text


def test_custom_prompt_bad_week_ending_asks_instead_of_crashing(home):
    (_prompt_dir(home) / "wk.md").write_text("args: week_ending\nReview {week_ending}.")

    async def go():
        async with Client(build_server()) as c:
            return (await c.get_prompt("wk", {"week_ending": "next friday"})).messages[0].content.text
    text = _run(go)
    assert "could not be filled in" in text and "YYYY-MM-DD" in text


def test_override_without_args_line_still_takes_week_ending(home):
    (_prompt_dir(home) / "weekly_review.md").write_text("Mine: {week_ending} to {next_week}.")

    async def go():
        async with Client(build_server()) as c:
            return (await c.get_prompt("weekly_review", {"week_ending": "2026-10-10"})).messages[0].content.text
    assert _run(go) == "Mine: 2026-10-10 to 2026-10-17."


def _status_and_tools(home):
    async def go():
        async with Client(build_server()) as c:
            tools = {t.name for t in (await c.list_tools()).tools}
            status = json.loads((await c.call_tool("data_status", {})).content[0].text)
            return tools, status
    return _run(go)


def test_override_missing_builtin_args_is_a_startup_problem(home, caplog):
    (_prompt_dir(home) / "explain_score.md").write_text("Explain it.")
    with caplog.at_level(logging.WARNING):
        tools, status = _status_and_tools(home)
    assert any("(at server start)" in p and "explain_score" in p and "score" in p for p in status["config_problems"])
    assert "explain_score" in caplog.text


def test_broken_profile_shows_in_data_status_and_tools_still_listed(home):
    (home / "config").mkdir(exist_ok=True)
    (home / "config" / "profile.toml").write_text("sex = ")
    tools, status = _status_and_tools(home)
    assert tools == TOOLS
    assert any(p.startswith("(at server start)") and "profile" in p for p in status["config_problems"])


def test_non_utf8_persona_shows_in_data_status(home):
    (home / "config" / "coach").mkdir(parents=True)
    (home / "config" / "coach" / "persona.md").write_bytes(b"\xff\xfe")
    tools, status = _status_and_tools(home)
    assert tools == TOOLS
    assert any(p.startswith("(at server start)") and "persona.md" in p for p in status["config_problems"])


def test_bad_prompt_file_shows_in_data_status(home):
    (_prompt_dir(home) / "oops.md").write_text("Hello {tomorrow}")
    _, status = _status_and_tools(home)
    assert any("oops.md" in p and p.startswith("(at server start)") for p in status["config_problems"])


def test_builtin_prompt_args_table_matches_the_server(home):
    from fitbit_air.coach.prompt_files import BUILTIN_PROMPT_ARGS

    async def go():
        async with Client(build_server()) as c:
            return {p.name: [a.name for a in (p.arguments or [])] for p in (await c.list_prompts()).prompts}
    assert _run(go) == BUILTIN_PROMPT_ARGS


def test_coach_startup_and_config_check_report_the_same_problems(home):
    from fitbit_air.userconfig import check_all

    prompts_dir = _prompt_dir(home)
    (prompts_dir / "explain_score.md").write_text("Explain it.")  # replaces a built-in, missing its args
    (prompts_dir / "oops.md").write_text("Hello {tomorrow}")
    (home / "config" / "coach" / "persona.md").write_bytes(b"\xff\xfe")
    (home / "config" / "profile.toml").write_text('race_date = "2030-05-01"\n')  # a race without a distance
    _, status = _status_and_tools(home)
    startup = " | ".join(status["config_problems"])
    expected = [str(p) for p in check_all(get_paths())]
    assert len(expected) == 4
    for problem in expected:
        assert problem in startup, problem


def test_build_server_does_not_create_the_profile(home):
    build_server()
    assert not (home / "config" / "profile.toml").exists()
