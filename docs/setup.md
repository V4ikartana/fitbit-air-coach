# Setup

This guide connects your Fitbit Air to a local database on your Mac, keeps it in sync, and registers the coach with
Claude. It takes about 15 minutes, once.

Your Fitbit Air's data lives in your Google account (the Google Health app). fitbit-air reads it through the
Google Health API v4 (`health.googleapis.com/v4`), using an OAuth client in a Google Cloud project that you create
and only you use.

## Prerequisites

- A Mac. The login token lives in the macOS Keychain and the background sync uses launchd. Linux is not tested yet.
- [uv](https://docs.astral.sh/uv/), for example `brew install uv`. uv installs Python 3.12 for you if needed.
- A Fitbit Air, set up in the Google Health app.
- Claude Desktop or Claude Code, for the coach and the dashboard (optional).

## 1. Install and create your home folder

```bash
git clone https://github.com/v4ikartana/fitbit-air-coach
cd fitbit-air-coach
uv sync
uv run fitbit-air init --template minimal     # or runner-10k, sleep-recovery
```

`init` creates your private home folder, `~/.fitbit-air`, with `data/`, `secrets/`, `config/` and `plugins/`. Only
your user can read it (`chmod 700`). Your data and settings live there, never in the repo.

- **Templates:** `minimal` is neutral, `runner-10k` is set up for a 10 km race, and `sleep-recovery` puts sleep
  first. See [configuration.md](configuration.md#getting-started-init-and-the-templates).
- **Another folder:** pass `--home PATH`, and set `FITBIT_AIR_HOME` to the same path in your shell profile.
- **Safe to re-run:** `init` never overwrites a file that already exists.

## 2. Create a Google Cloud project

1. Open https://console.cloud.google.com/projectcreate and sign in with **the same Google account your Fitbit Air
   uses** in the Google Health app.
2. Name the project, for example `fitbit-air-personal`, and click **Create**. Make sure it is selected in the top
   bar.

## 3. Enable the Google Health API

1. Open https://console.cloud.google.com/apis/library/health.googleapis.com
2. Click **Enable**.

## 4. Create the OAuth consent screen and client

Open https://console.cloud.google.com/auth/overview and click **Get started**.

1. **App information:** an app name, for example `Fitbit Air Personal`, and your Google address as the support
   email.
2. **Audience:** **External**.
3. **Contact information:** your Google address. Agree to the policy and click **Create**.
4. In the left menu, open **Audience**. Under **Test users**, click **+ Add users**, add your Google address and
   click **Save**.
5. In the left menu, open **Data Access** and click **Add or remove scopes**. Filter for "Google Health API", tick
   these six read-only scopes, then click **Update** and **Save**:
   - `.../auth/googlehealth.activity_and_fitness.readonly`
   - `.../auth/googlehealth.health_metrics_and_measurements.readonly`
   - `.../auth/googlehealth.sleep.readonly`
   - `.../auth/googlehealth.profile.readonly`
   - `.../auth/googlehealth.irn.readonly`
   - `.../auth/googlehealth.settings.readonly`

   If the list doesn't show them, paste the full URLs into **Manually add scopes** and click **Add to table**:
   ```
   https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly
   https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly
   https://www.googleapis.com/auth/googlehealth.sleep.readonly
   https://www.googleapis.com/auth/googlehealth.profile.readonly
   https://www.googleapis.com/auth/googlehealth.irn.readonly
   https://www.googleapis.com/auth/googlehealth.settings.readonly
   ```
6. In the left menu, open **Clients**, click **+ Create client** and choose the application type **Web
   application**. Name it, for example `fitbit-air-cli`. Under **Authorized redirect URIs**, add exactly
   `http://localhost:8765/` (with the slash), then click **Create**.
7. In the **OAuth client created** dialog, click **Download JSON** straight away: Google shows the client secret
   only at this moment. If you missed it, open the client in **Clients**, click **Add secret**, and download the
   JSON then. Move the file into place:
   ```bash
   mv ~/Downloads/client_secret_*.json ~/.fitbit-air/secrets/client_secret.json
   chmod 700 ~/.fitbit-air/secrets && chmod 600 ~/.fitbit-air/secrets/client_secret.json
   ```

Never commit this file or paste it anywhere. It is your app's credential.

## 5. Log in and pull your history

```bash
uv run fitbit-air auth
```

Your browser opens. Google warns that it **hasn't verified this app**. That is expected: it is your own app. Click
**Advanced**, then **Go to Fitbit Air Personal (unsafe)**, tick every permission, and click **Continue**. If you
untick a permission, `auth` lists what is missing, and those data types won't sync.

```bash
uv run fitbit-air sync
uv run fitbit-air status
```

`sync` fetches new data and backfills any unfinished history, so the first run walks your history back to 2015,
skipping empty years quickly. It can take from several minutes to an hour, depending on how much history you have.
If it is interrupted, run `sync` again: it resumes where it stopped. Later, `sync --backfill` re-walks the full
history if you ever need it.

## 6. Testing mode and the 7-day login expiry

Your app starts in **Testing** mode, with you as its only test user. In Testing mode, Google expires the login
every 7 days.
- **What you see:** the background sync stops fetching, a macOS notification says "Google login needs attention",
  and `sync` exits with code 2.
- **The fix:** run `uv run fitbit-air auth` again. Nothing is lost; the next sync catches up.

**To stop the weekly expiry,** you can publish the app: open https://console.cloud.google.com/auth/audience, click
**Publish app** and confirm (**In production**), then run `uv run fitbit-air auth` once more. Google's verification
rules depend on the scopes and the number of users, and they change over time. Read Google's current OAuth policy
before you publish. The "hasn't verified this app" warning at login stays after publishing, unless Google verifies
the app. If Google asks for verification you don't want to go through, stay in Testing and re-run `auth` once a
week.

## 7. Turn on background sync (macOS)

```bash
uv run fitbit-air schedule install
```

This installs a launchd job, `io.github.fitbit-air.sync`, that runs `fitbit-air sync` every 2 hours, at login, and
after wake if a run was missed. It runs the code in this checkout against the data in your home folder. Every sync
also re-runs the analysis.

- **Check it is loaded:**
  ```bash
  launchctl print gui/$(id -u)/io.github.fitbit-air.sync | grep -E "state|run interval"
  ```
- **Logs:** `~/.fitbit-air/data/logs/sync.log`, and crashes in `~/.fitbit-air/data/logs/launchd.err.log`.
- **Remove it:** `uv run fitbit-air schedule uninstall`. It also stops any older `*fitbit-air.sync` job and keeps
  its plist in `~/.fitbit-air/launchd-backup/`.

Keep the checkout where it is: the job runs the code from that folder.

## 8. Set your profile

Your profile is `~/.fitbit-air/config/profile.toml`: facts about you that Google doesn't provide. Edit it, or use
`profile set`. The values below are examples:

```bash
uv run fitbit-air profile show
uv run fitbit-air profile set sex male                 # male | female | unspecified
uv run fitbit-air profile set race_date 2030-05-01     # set race_distance_km too
uv run fitbit-air profile set race_distance_km 10
uv run fitbit-air profile set height_m 1.75            # needed for BMI, and body age's BMI part
uv run fitbit-air config check                         # validate all your settings
```

Body age is only calculated with `sex = "male"`, because its reference data is for men. Then run
`uv run fitbit-air report` any day. Every key is explained in [configuration.md](configuration.md).

## 9. Register the coach with Claude

`fitbit-air mcp` is a local MCP server. Claude Desktop and Claude Code start it themselves; you never run it by
hand. Register it once, from the repo folder.

**Claude Desktop:**
1. Open Claude Desktop once first, so it has created its settings folder.
2. Quit Claude Desktop with Cmd+Q. Desktop rewrites its config file while it runs, so edits made with it open are
   lost.
3. Run:
   ```bash
   FITBIT_AIR_HOME=~/.fitbit-air uv run python scripts/register_claude_desktop.py
   ```
   It backs up `claude_desktop_config.json`, adds the `fitbit-coach` entry (with the absolute path to `uv`, since
   Claude Desktop does not see your shell `PATH`, and your home folder) and reopens Claude.

**Claude Code:** the JSON is a single argument, so nothing is mistaken for the server name.
```bash
claude mcp add-json -s user fitbit-coach "{\"type\": \"stdio\", \"command\": \"$(which uv)\",
  \"args\": [\"--directory\", \"$PWD\", \"run\", \"--frozen\", \"fitbit-air\", \"mcp\"],
  \"env\": {\"FITBIT_AIR_HOME\": \"$HOME/.fitbit-air\"}}"
```
If you don't have the `claude` command yet, install Claude Code first: https://docs.anthropic.com/en/docs/claude-code

Then open a conversation and pick a prompt: `morning_checkin`, `weekly_review`, `plan_next_run` or
`explain_score`. The coach's log is
`~/.fitbit-air/data/logs/coach.log`. To change its tone, or add prompts, see
[configuration.md](configuration.md#the-coach-persona-and-prompts).

## 10. Build the dashboard (optional)

```bash
uv run fitbit-air dashboard build      # writes ~/.fitbit-air/data/dashboard/dashboard.html
uv run fitbit-air snapshot             # writes the daily snapshot, data/dashboard/snapshot.json
```

You then ask Claude to publish `dashboard.html` as a private Claude Artifact. In Claude Desktop on your Mac it shows
live numbers from `fitbit-coach`; elsewhere it shows the latest snapshot. The steps, and the capabilities the page
needs, are in [dashboard/README.md](../dashboard/README.md). The layout is set in `config/dashboard.toml`
([configuration.md](configuration.md#dashboard-configdashboardtoml)).

## Troubleshooting

- **macOS notification "Google login needs attention":** run `uv run fitbit-air auth`. The `sync` output also prints
  the reason; an expired or revoked login looks different from a locked Keychain.
- **macOS asks to allow Keychain access** (for example after a Python or uv upgrade): click **Always Allow**, or
  re-run `uv run fitbit-air auth`.
- **`unavailable ... HTTP 403` for a type in `status`:** that scope wasn't added in step 4, or wasn't ticked at
  login. Add it, then re-run `auth` and tick every box.
- **`pending rebuild: N dates` in `status`, or a type marked failed after a parser fix:** run
  `uv run fitbit-air rebuild`. It repairs the clean tables from the raw data already stored, without calling Google.
- **The background sync crashed or did nothing:** look in `~/.fitbit-air/data/logs/launchd.err.log` for crashes, and
  in `~/.fitbit-air/data/logs/sync.log` for normal progress.
- **No notification from the background job:** notifications are delivered through Script Editor. Allow **Script
  Editor** in System Settings → Notifications.
- **`sync` keeps saying "already running":** an earlier run may be stuck behind a hidden Keychain dialog. Find it (it
  may be behind other windows) and answer **Always Allow**, or quit the stuck process.
- **`redirect_uri_mismatch`:** the redirect URI must be exactly `http://localhost:8765/`, with the slash.
- **Port 8765 is busy:** close whatever uses it (`lsof -i :8765`) and try again.
- **"Google returned no refresh token":** revoke the app at https://myaccount.google.com/permissions and run
  `uv run fitbit-air auth` again.
