# Plugins

A plugin is a small Python file in `~/.fitbit-air/plugins/` that adds your own daily metrics, findings or read-only
coach tools. Plugins are **off by default**.

> **Security: plugins are your own trusted code, not a sandbox.** A plugin runs inside the analysis and the coach
> server with your user's permissions. It can read and write any file you can. The read-only context described below
> guards against mistakes, not against a hostile plugin. Only put files in `plugins/` that you wrote or have read
> and trust.

## Enabling plugins

In `config/profile.toml`:

```toml
[plugins]
enabled = true
```

Then put `*.py` files in `<home>/plugins/` and run `uv run fitbit-air config check`. Every file is loaded in name
order.
- **Analysis:** each analysis run loads the plugins afresh. That happens after every sync, and with
  `fitbit-air analyze`.
- **Coach:** the coach server loads the plugins when it starts, so restart Claude to pick up a change.
- **Status:** `fitbit-air status` shows how many plugins loaded and any problems.

## `register(api)`

Each file defines `register(api)`. It can register three kinds of things. Names are lowercase letters, digits and
`_`, start with a letter, and are at most 40 characters. A name may be used only once across all plugin files.

| Call | Your function | What happens |
|---|---|---|
| `api.metric(name, fn, unit="", description="")` | `fn(day, ctx) -> float \| None` | Runs once per analysed day. The value is stored in `metric_daily` as `plugin:<name>`, so `get_trends`, the snapshot and the dashboard (`trend_metrics = ["plugin:<name>"]`) can use it. |
| `api.finding(name, fn, evidence_note)` | `fn(day, ctx) -> {"severity", "title", "detail"} \| None` | Runs once per analysed day. A result becomes an insight of kind `plugin`, with evidence id `plugin:<name>`. `get_evidence` returns your `evidence_note` for it. |
| `api.coach_tool(name, fn, description)` | `fn(ctx, **kwargs) -> dict` | Exposed to Claude as a read-only MCP tool named `plugin_<name>`. The parameters after `ctx` become the tool's arguments. |

### A complete example

```python
# ~/.fitbit-air/plugins/example.py


def rhr_doubled(day, ctx):
    """A metric: one number per day, or None when there is nothing to report."""
    rhr = ctx.daily("resting_hr")          # Google's resting heart rate for `day`, or None
    return None if rhr is None else rhr * 2


def rhr_seen(day, ctx):
    """A finding: a dict, or None for no finding today. Severity is "info" or "watch", never "alert"."""
    if ctx.daily("resting_hr") is None:
        return None
    return {"severity": "info", "title": "Resting HR recorded", "detail": f"A resting HR was recorded on {day}."}


def echo(ctx, word: str = "hi"):
    """A coach tool: called by Claude with keyword arguments; returns a plain JSON dict."""
    return {"word": word, "today": ctx.today.isoformat()}


def register(api):
    api.metric("rhr_x2", rhr_doubled, unit="bpm", description="Resting HR times two (an example)")
    api.finding("high_rhr", rhr_seen, evidence_note="Example rule: notes any day with a resting HR reading.")
    api.coach_tool("echo", echo, description="Echo a word back (an example).")
```

`day` is the local date as a `YYYY-MM-DD` string.

## What `ctx` gives you

### In metrics and findings: `DayContext`

A read-only view of one day:

- **`ctx.metric(name) -> float | None`** returns a value from the analysis (`metric_daily`) for that day. For example:
  `rhr`, `hrv`, `hrv_7d`, `resp`, `temp`, `spo2`, `steps_7d`, `mvpa_week`, `azm_week`, `sedentary_h`,
  `strength_min_week`, `strength_days_week`, `strain_level`, `weight_trend_kg`. **The derived sleep metrics** are
  reached here too, for example `sleep_min` (minutes asleep) and `sleep_debt_h`. Metrics from other plugins are not
  included.
- **`ctx.daily(name) -> float | None`** returns Google's own daily value (`daily_metrics`) for that day. For example:
  `steps`, `resting_hr`, `hrv_rmssd_ms`, `resp_rate`, `spo2_avg_pct`, `sleep_temp_dev_c`, `active_kcal`,
  `distance_m`, `sedentary_min`, `vo2_max`.

Both return `None` when the value is missing that day. Your function must cope with that. Which names exist depends
on your data. To list them, ask the coach, or run `SELECT DISTINCT metric FROM metric_daily` (or `daily_metrics`)
with the `query_sql` tool.

### In coach tools: `PluginToolCtx`

- **`ctx.today`** is the local date, and **`ctx.now`** the current time in UTC.
- **`ctx.query(sql) -> dict`** runs one read-only `SELECT` through the same `run_query` as the `query_sql` tool. It
  has the same limits: one statement, a read-only connection, 500 rows and a 5 s time limit. It returns
  `{"columns", "rows", "truncated"}`, or `{"error", "hint"}` when the query is refused or fails.

There is no store, no file path and no write access in this context. Your tool must return a `dict` of plain JSON
(no NaN or infinity). Anything else, an exception, or a call to `sys.exit()` becomes an `{"error", "hint"}` result
for Claude. The coach server keeps running.

## Limits

- **Metrics:** a value must be a finite number, or `None` for no value. `True`/`False`, NaN, infinity and strings
  are rejected.
- **Findings:** `severity` must be `info` or `watch`. Only the built-in safety rails raise alerts. The title is cut to
  200 characters, and the detail and the evidence note to **2,000 characters**.
- **Labels:** the unit is cut to 20 characters, a metric description to 200, and a tool description to 500.
- **Time budget:** each plugin file may spend **5 seconds in total** across all the days of one analysis run. Once a
  file goes over budget, its remaining days are skipped and the run records a problem. The check runs between calls,
  so **a single call that hangs is not interrupted**. Keep your functions quick, and never wait on the network
  without a timeout.

## When something goes wrong

A plugin never breaks a sync, the analysis or the coach:
- **A file that fails to load** (a syntax error, an exception or `sys.exit()` in `register`, or no `register`
  function) is skipped whole, and nothing it registered is kept.
- **A bad value or an exception on one day** drops that value and is recorded as a problem.
- **Where problems show:** `config check`, `status`, the analysis run's error (shown by `status`) and the coach's
  `data_status`.
- **Output:** anything a plugin prints goes to stderr, so it can't corrupt the coach's MCP stream.

Plugins never run in this project's tests or CI, except test-local ones.
