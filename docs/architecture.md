# Architecture

fitbit-air is one Python package (`src/fitbit_air`) with one command, `fitbit-air`. The code and your data are kept
apart: the code is this checkout, and your data, secrets, config and plugins live in your home folder.

## Data flow

```
Google Health API v4 (health.googleapis.com/v4)
        |  client.py: OAuth token, throttling, retries, pagination
        v
raw_points            every API payload, verbatim
        |  normalize.py (and `fitbit-air rebuild`)
        v
clean tables          heart_rate, samples, daily_metrics, sleep_sessions, sleep_stages,
                      activity_intervals, exercise_sessions, alerts
        |  analysis/ (after every sync, and `fitbit-air analyze`)
        v
analysis tables       metric_daily, scores_daily, insights, runs
        |
        +--> `fitbit-air report`      plain-text daily report
        +--> coach (MCP server)       tools and prompts for Claude
        +--> `fitbit-air snapshot`    JSON for the dashboard
```

- **Sync:** each data type has a watermark in `sync_state`. A sync backfills unfinished history (back to 2015),
  re-fetches 3 days of overlap, and once a day re-scans the last 14 days for late or revised points.
- **Raw first:** the payload is stored before it is parsed. If a parser fails (for example after an API format
  change), the dates are marked `pending rebuild`, and `fitbit-air rebuild` repairs them from `raw_points` without
  calling Google.
- **Analysis:** after every sync, the analysis recomputes the last 60 days. Every numeric parameter comes from
  `analysis/evidence.toml`, plus any threshold overrides in your config.
- **Coach-only tables:** `journal` and `coach_notes` hold what you and the coach write. They are never sent to
  Google.
- **Views:** `daily_wide` and `activity_daily` give one row per day for easy queries.

## Modules

| Module | Job |
|---|---|
| `cli.py` | The `fitbit-air` command (Typer). |
| `config.py` | Paths, the home folder, API constants and OAuth scopes. |
| `auth.py` | Google OAuth login in the browser, the refresh token in the macOS Keychain, access-token refresh. |
| `client.py` | HTTP client for the Google Health API v4: auth header, throttling, retries, pagination. |
| `registry.py` | The 27 data types that are synced, and how to query each one. |
| `sync.py` | Sync orchestration: backfill each type's history, then keep it current. |
| `normalize.py` | Turns raw API data points into rows for the clean tables. |
| `store.py` | SQLite storage. Nearly all SQL lives here; the exceptions are the coach's read-only `coach/sql.py` and the copy checks in `home.py`'s migrate. |
| `lock.py` | A file lock, so a scheduled sync and a manual sync never run at the same time. |
| `schedule.py` | Installs and removes the launchd job. |
| `notify.py` | macOS notifications, for example when the Google login needs attention. |
| `home.py` | `init`: creates the home folder from a template. |
| `userconfig.py` | Loads and validates the files in `config/`, with file and line, and falls back safely. |
| `plugins.py` | Loads your plugins from `plugins/` when they are enabled. |
| `dashboard_build.py` | Builds the dashboard page from the template and your layout. |
| `analysis/` | Baselines, data quality, sleep, recovery, strain signals, safety rails, activity, weight, running, body age, the evidence registry and the daily report. |
| `coach/` | The MCP server, its read tools, write actions, read-only SQL, persona, prompts and the dashboard snapshot. |
| `templates/` | The starter configs: `minimal`, `runner-10k`, `sleep-recovery`. |
| `dashboard/template.html` | The dashboard page. |

## The home folder

```
~/.fitbit-air/                 (or $FITBIT_AIR_HOME; only your user can read it)
  data/
    health.db                  the SQLite database
    logs/                      sync.log, coach.log, launchd.err.log
    dashboard/                 dashboard.html, snapshot.json
  secrets/
    client_secret.json         your Google OAuth client
  config/
    profile.toml               facts about you that Google doesn't provide
    thresholds.toml            threshold overrides (optional)
    dashboard.toml             dashboard layout (optional)
    coach/persona.md           the coach's tone (optional)
    coach/prompts/*.md         your prompts (optional)
  plugins/*.py                 your plugins (off unless enabled)
  launchd-backup/              plists of older sync jobs, kept when they are replaced
```

The Google refresh token is not in this folder: it is in the macOS Keychain.

## Why the snapshot exists

The dashboard is a Claude Artifact. In Claude Desktop on your Mac, it calls the local `fitbit-coach` server for live
numbers. On your phone or in a browser, that server can't be reached. `fitbit-air snapshot` writes the dashboard's
sections as one JSON file (under 240 KB), built by the same functions the coach uses. You store it in the
artifact's own private database, and the page shows it, labelled with its time, whenever live data is unavailable.

## Error handling

- **A sync never stops on one type.** Each data type succeeds or fails on its own. `sync` exits 0 when all went
  well, 1 when a type failed, and 2 when the Google login needs attention (with a macOS notification).
- **Transient errors are retried.** HTTP 429 and 5xx responses are retried with backoff, honouring `Retry-After`. A
  locked Keychain fails only that run; the next run tries again.
- **The analysis never fails a sync.** Its errors are recorded on the analysis run and shown by `status`.
- **Config problems fall back to defaults:**
  - a broken config file means the built-in defaults for that file;
  - an invalid `thresholds.toml` applies none of its overrides;
  - if the analysis fails with your overrides, it runs again with the built-in defaults, so the safety rails
    always run.
- **Problems are visible.** They are recorded on the analysis run, listed by `config check`, printed after
  `analyze` and `report`, and returned in the coach's `data_status` as `config_problems`.
- **Plugins are isolated from failure.** A plugin that fails to load is skipped. A bad value or an exception on one
  day drops that value and records a problem. Each plugin file has a 5-second time budget per analysis run.
- **Coach tools return errors, not crashes.** A failing tool returns `{"error", "hint"}` to Claude, and the server
  keeps running.
