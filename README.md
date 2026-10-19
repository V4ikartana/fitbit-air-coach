# fitbit-air-coach

Sync your Fitbit Air data from the Google Health API to a local SQLite database on your Mac, analyse it with cited
science, and talk to it through Claude as a personal coach.

> Status: experimental (v0.1.0). Unofficial — not affiliated with or endorsed by Fitbit, Google or Anthropic.
> Not medical advice. See [DISCLAIMER.md](DISCLAIMER.md).

**Local-first, not offline.** Your health data is stored on your Mac, and the project runs no server or database
of its own. When you use the Claude coach or publish the dashboard, the data they read is sent to Anthropic for
processing. See [Security and privacy](#security-and-privacy).

The repository is fitbit-air-coach; the package and command are `fitbit-air`.

## What it does

fitbit-air has four parts. They share one private home folder, `~/.fitbit-air`.

- **Sync:** reads your data from the Google Health API v4 (`health.googleapis.com/v4`) with your own OAuth client,
  into a local SQLite database. A launchd job syncs every 2 hours, at login, and after wake if a run was missed.
- **Analysis:** personal baselines, sleep and recovery scores, strain signals, safety rails, activity, weight,
  running and a race plan, and body age. Every number cites a study or a stated design choice.
- **Coach:** a local MCP server for Claude Desktop and Claude Code. Claude reads your scores and trends, runs
  read-only SQL, and keeps a journal and coach notes on your Mac.
- **Dashboard:** a page you publish as a private Claude Artifact. It shows live numbers in Claude Desktop on your
  Mac, and a daily snapshot anywhere else.

## Features

- **27 Google Health data types:** heart rate, HRV, resting HR, sleep stages, SpO2, respiratory rate,
  temperature during sleep, steps, active zone minutes, exercise sessions, VO2 max, weight, body fat and more.
- **A sync you can leave running:** a resumable backfill back to 2015, a 3-day overlap on every sync, a daily 14-day
  re-scan for late data, throttling and retries. A file lock stops two syncs running at once.
- **A raw layer:** every API payload is kept verbatim in `raw_points`. `fitbit-air rebuild` regenerates the clean
  tables from it without calling Google.
- **Analysis on your own baselines:**
  - sleep score on duration, regularity (SRI), efficiency and timing;
  - recovery score from HRV, resting HR and sleep;
  - strain signals (an illness early warning) and an experimental stress index;
  - absolute safety rails, shown before everything else;
  - activity (steps, moderate-to-vigorous minutes, sedentary time, strength), weight trend and BMI;
  - running: heart-rate zones, a single-run spike rule, a race plan and a time estimate;
  - body age, with its range and caveats.
- **Honest confidence:** scores say "building baseline" until there is enough data, and recovery findings need a
  minimum baseline confidence.
- **Cited science:** each score cites sources in `src/fitbit_air/analysis/evidence.toml`. See
  [docs/science.md](docs/science.md).
- **The coach:** read tools (overview, trends, sleep, activity, running, body age, evidence, data status), one
  read-only SQL query at a time, a local journal and coach notes, profile edits, a "refresh now" tool, and four
  prompts: `morning_checkin`, `weekly_review`, `plan_next_run` and `explain_score`.
- **Customization:** starter templates, your profile, threshold overrides within bounds, the coach's persona and
  prompts, and the dashboard layout. `fitbit-air config check` validates everything.
- **Plugins:** small Python files that add your own daily metrics, findings and read-only coach tools. Off by
  default.

## Compatibility and prerequisites

- **macOS:** tested. The login token lives in the macOS Keychain and the background sync uses launchd.
- **Linux:** not tested yet. CI is set up to run the tests on Ubuntu, but Linux is not supported.
- **Python 3.12** and [uv](https://docs.astral.sh/uv/). uv installs Python for you if needed.
- **A Fitbit Air**, set up in the Google Health app.
- **A Google Cloud OAuth client** with the Google Health API enabled. You create it yourself in a free Google Cloud
  project; [docs/setup.md](docs/setup.md) walks through it.
- **Claude Desktop or Claude Code** for the coach and the dashboard. Sync, analysis and `report` work without them.

## Quick start

```bash
git clone https://github.com/v4ikartana/fitbit-air-coach
cd fitbit-air-coach
uv sync
uv run fitbit-air init --template minimal      # or runner-10k, sleep-recovery
# Create your Google OAuth client (docs/setup.md), then save its JSON file as:
#   ~/.fitbit-air/secrets/client_secret.json
uv run fitbit-air auth                         # log in with Google in your browser
uv run fitbit-air sync                         # fetches new data and backfills any unfinished history
uv run fitbit-air schedule install             # background sync via launchd (see below)
uv run fitbit-air report                       # today's safety checks, scores and findings
```

The background job syncs every 2 hours, at login, and after wake if a run was missed. While your OAuth app is in
Testing mode, Google expires the login every 7 days; run `fitbit-air auth` again when you are asked to. The full
walkthrough, including the coach registration and the dashboard, is [docs/setup.md](docs/setup.md).

> **Privacy note:** sync, analysis and `report` work entirely on your Mac, without Claude. Turning on the MCP coach
> or publishing the dashboard sends some of your health data to Anthropic. See
> [Security and privacy](#security-and-privacy).

## Configuration

Everything personal lives in `~/.fitbit-air` (or the folder in `FITBIT_AIR_HOME`), never in the repo.

- **Templates:** `init --template` starts you with `minimal`, `runner-10k` or `sleep-recovery`.
- **Profile** (`config/profile.toml`): sex, sleep need, goals, race date and distance, height, units.
- **Thresholds** (`config/thresholds.toml`): override any analysis parameter, with a reason. Values stay within
  bounds, and safety rails can only get stricter.
- **Coach** (`config/coach/`): your persona and your own prompts. The core safety rules always apply.
- **Dashboard** (`config/dashboard.toml`): tabs, cards, trend metrics, default range and colours.
- **Plugins** (`plugins/*.py`): your own metrics, findings and coach tools.

A broken file never breaks a sync: the analysis falls back to the built-in defaults for that file and reports the
problem. Details: [docs/configuration.md](docs/configuration.md) and [docs/plugins.md](docs/plugins.md).

## Usage

```bash
uv run fitbit-air status               # login state, what's synced, pending rebuilds, row counts
uv run fitbit-air sync [--backfill]    # fetch new data; --backfill re-walks the full history
uv run fitbit-air report [--date D]    # a day's safety checks, scores and findings
uv run fitbit-air analyze [--since D]  # recompute baselines, scores and findings (default: last 60 days)
uv run fitbit-air rebuild [--since D]  # regenerate clean tables from the raw layer
uv run fitbit-air profile show|set K V # your analysis profile
uv run fitbit-air config show|check    # effective settings and where they come from; validate them
uv run fitbit-air dashboard build      # the dashboard page with your layout
uv run fitbit-air snapshot             # the dashboard's daily snapshot
uv run fitbit-air schedule install|uninstall
```

**The coach:** register `fitbit-air mcp` once with Claude Desktop (`scripts/register_claude_desktop.py`) or Claude
Code (`claude mcp add-json`), as described in [docs/setup.md](docs/setup.md#9-register-the-coach-with-claude).
Claude then starts the server itself. Pick a prompt such as `morning_checkin`, or just ask for a weekly review or
the plan for your next run.

## Architecture

The sync writes every Google payload to a raw table, then normalises it into clean tables (heart rate, sleep,
activity, daily metrics). After each sync, the analysis recomputes the last 60 days into analysis tables (daily
metrics, scores, findings, runs), using parameters from `evidence.toml` and your config. The CLI report, the MCP
coach and the dashboard snapshot all read those tables through the same functions. See
[docs/architecture.md](docs/architecture.md).

## Security and privacy

fitbit-air is local-first, not fully offline.

- **Stored on your Mac:** the database, config and logs live in `~/.fitbit-air`. The folder is 0700 and the files
  fitbit-air writes are 0600. The SQLite database never leaves your Mac.
- **Google:** fitbit-air logs in with your own OAuth client and calls the Google Health API v4 to read your data.
  The OAuth scopes are read-only, and nothing is ever written to Google.
- **Claude (the MCP coach):** when you ask Claude something, the results of the tools it calls (scores, trends,
  sleep, runs, journal entries, or rows from a read-only SQL query) are sent to Anthropic as part of that
  conversation.
- **Dashboard:** in Claude Desktop the page gets live data from the coach through Claude Desktop. If you publish
  it, the daily snapshot (under 240 KB) is stored in your Claude Artifact: your data status and overview, 30- and
  90-day trends, the last 14 days of sleep, 30 days of activity and 90 days of runs, body age and your active coach
  notes.
  "Private" means only you can open it, not that it stays on your Mac.
- **No project backend or telemetry:** the project runs no server or database of its own. Apart from the above, the
  code sends nothing anywhere. The dashboard page is self-contained: it uses your system fonts and loads no outside
  scripts, fonts or images. There is no analytics or usage reporting.
- **Your login:** the Google refresh token is stored in the macOS Keychain. Access tokens are kept in memory only.
- **Plugins** run with your user's permissions. Only install code you wrote or have read.

Using the coach or publishing the dashboard means a third party processes sensitive health data. Read Anthropic's
privacy and data retention terms before you turn these on. If you prefer not to share data with Claude, use sync,
analysis and `fitbit-air report` on their own; they need no AI service.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Known limitations

- **Fitbit Air and the Google Health API v4 only.** Other devices and APIs are not supported.
- **macOS only:** the background sync uses launchd and the token uses the Keychain.
- **Testing mode:** while your OAuth app is in Testing, Google expires the login every 7 days.
- **Body age** uses reference data for men, so it is only calculated when the profile says `sex = "male"`.
- **Not clinically validated:** the individual studies are published, but this combination of methods has not been
  validated. Not medical advice.
- **Plugins are trusted code, not a sandbox.** A single call that hangs is not interrupted.
- **Values can differ slightly from the Google Health app.** The API is the source used.
- **Runs** count toward the race plan only when they are logged as type Run.
- **Units:** the report and dashboard show metric units for now.

## Roadmap

- Linux support (CI runs Ubuntu; not yet verified).
- Further work follows real use; ideas are welcome as issues.

## Contributing

Issues and pull requests are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) and the
[code of conduct](CODE_OF_CONDUCT.md) first. Report security issues privately, as described in
[SECURITY.md](SECURITY.md).

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Built by Vaikartana ([@v4ikartana](https://github.com/v4ikartana))
