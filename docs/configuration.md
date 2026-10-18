# Configuration

Everything personal lives in one private **home folder** outside the code. After `fitbit-air init`, the code (this
repo) is never written to.
You can change your profile, the science thresholds (within safe limits), the coach's persona and prompts, and the
dashboard layout. You can also add small [plugins](plugins.md).

## The home folder

The default home is `~/.fitbit-air`. Set `FITBIT_AIR_HOME` to use another folder.

```
~/.fitbit-air/
  data/                 health.db, logs/, dashboard/ (snapshot.json, dashboard.html)
  secrets/              client_secret.json (your Google OAuth client)
  config/
    profile.toml        facts about you that Google doesn't provide
    thresholds.toml     your threshold overrides (optional)
    dashboard.toml      dashboard layout (optional)
    coach/persona.md    the coach's tone and style (optional)
    coach/prompts/*.md  your own prompts, or replacements for the built-in ones (optional)
  plugins/*.py          your plugins (off unless enabled in profile.toml)
```

`init` makes the folder readable by you only (`chmod 700`).

## Getting started: `init` and the templates

```bash
uv run fitbit-air init --template minimal          # or runner-10k, sleep-recovery
uv run fitbit-air init --template minimal --home ~/somewhere-else   # then set FITBIT_AIR_HOME to it
```

`init` creates the folder layout and copies the template's `config/` files. It never overwrites a file that already
exists, so it is safe to run again.

| Template | What you get |
|---|---|
| `minimal` | A neutral profile, and a dashboard without the running tab. |
| `runner-10k` | An example 10 km race (change the date), running-first tabs, a direct, data-led persona, a weekly-review prompt, and commented-out threshold examples. |
| `sleep-recovery` | An 8 h sleep need, sleep and recovery goals, sleep-first tabs and trends, and a calm persona. |

## Layers, `config show` and `config check`

There are two layers. Each later one overrides the one before, key by key:
1. the built-in defaults (in the package);
2. your files in `config/`. A template is copied there once at `init`, and from then on those files are yours.

```bash
uv run fitbit-air config show    # the effective config, with where each value comes from (default or a file)
uv run fitbit-air config check   # validate every file and plugin; exits 1 and lists problems with file and line
```

**A broken file never breaks a sync.** If a file has a problem:
- the analysis, report and coach use the built-in defaults for that file;
- the problem is recorded on the analysis run (`status` shows it), listed in the coach's `data_status`, and printed
  by `config check`.

For `thresholds.toml` this is all or nothing: if any entry is invalid, **none** of its overrides are applied until
you fix it. As a last line of defence, if the analysis itself fails with your overrides, it is run again with the
built-in defaults and the failure is recorded, so the safety rails always run. A `profile.toml` that can't be loaded
means the built-in profile defaults for the analysis. `fitbit-air analyze` and `report` print any config problems
after their output.

## Profile (`config/profile.toml`)

```toml
sex = "unspecified"          # male | female | unspecified
sleep_need_h = 7.5           # 3 to 12
goals = ["sleep", "cardio"]  # free text, shown to the coach
units = "metric"             # metric | imperial (coach wording only, for now)
# race_date = "2030-05-01"   # YYYY-MM-DD; set it together with race_distance_km
# race_distance_km = 10.0    # above 0, at most 100
# height_m = 1.75            # 1.0 to 2.5; needed for BMI
# hrmax_override = 190       # whole number, 120 to 230; otherwise max HR is estimated
# timezone = "Europe/London" # reserved (not used yet); an exact IANA name

[plugins]
enabled = false              # see docs/plugins.md
```

| Key | Default | Notes |
|---|---|---|
| `sex` | `"unspecified"` | Body age needs `male`, because its reference data is for men. Otherwise it says why it is missing. |
| `race_date`, `race_distance_km` | not set | Set both to get a race plan and a forecast. With no race, those show "no race configured". Setting only one is a `config check` error. |
| `sleep_need_h` | `7.5` | The basis for sleep debt. |
| `goals` | `[]` | A list of strings: at most 10 goals, each at most 60 characters. |
| `height_m` | not set | Needed for BMI, and so for body age's BMI part. |
| `hrmax_override` | not set | Replaces the estimated maximum heart rate. |
| `units` | `"metric"` | Used by the coach for wording. The report and dashboard show metric for now. Storage and the science always stay metric. |
| `timezone` | not set | Reserved (not used yet). If set, it must be an exact IANA name, such as `Europe/London`. |
| `[plugins] enabled` | `false` | The only key in `[plugins]`. Writing `plugins_enabled` at the top level is an error. |

An unknown key, a wrong type or a value out of range is an error. Text values can't contain control characters.

`fitbit-air profile set KEY VALUE` changes one key from the command line. It rewrites the file, so comments are lost.
For goals, give a comma-separated list. To unset an optional key, give `none`.

## Thresholds (`config/thresholds.toml`)

Every number the analysis uses is a named param in the package's `src/fitbit_air/analysis/evidence.toml`, next to the
study it comes from. You can override params, and a table's anchor points, with your own values:

```toml
[params.steps_benchmark]
value = 8000
reason = "My own daily target"

[tables.ding2025_steps]           # replaces the table's anchors; same kind and checks as evidence.toml
kind = "loglinear"                # step | linear | loglinear
points = [[2000, 1.0], [7000, 0.53], [12000, 0.45]]
reason = "Why I replaced it"
```

**The rules:**
- Only `[params.<name>]` and `[tables.<name>]` sections are allowed, and only for names that already exist. An unknown
  name is an error, with a "did you mean" suggestion.
- Every entry needs a non-empty `reason`. A param entry takes only `value` and `reason`. A table entry takes only
  `kind`, `points` and `reason`.
- `value` must be a finite number.
- **Whole numbers:** a param whose name ends in `_days`, `_nights`, `_weeks`, `_every` or `_weighins`, and whose
  default is a whole number, takes only a whole number.
- **Tables** need at least two `[x, y]` points, with x strictly increasing. A `loglinear` table also needs every y
  above 0.
- **Bounds:** a value must stay within its param's bounds (see below).
- **Score ranges keep their order:** the ends of each sleep-score range must stay different and in order, for
  example `timing_full_min` below `timing_zero_min`, `eff_zero` below `eff_full`, `sri_zero` below `sri_full`, and
  `sleep_dur_zero_h` < `sleep_dur_ok_min_h` < `sleep_dur_ok_max_h` < `sleep_dur_long_h`.
- **Safety rails** can only be made stricter (see below).

Active overrides are never hidden. They are listed in `data_status` and the report footer, and the dashboard status
line says "personal thresholds active". The coach is told to mention them, and `get_evidence("override:<param>")`
returns the value, the default and your reason.

### Bounds

By default a param may move within **`[default / 4, default × 4]`**. For params ending in `_pct` the upper end is
capped at 100. Where that rule doesn't fit, `evidence.toml` has an explicit `[bounds]` entry:

| Param | Default | Bounds | Why |
|---|---|---|---|
| `hrv_below_z` | -1.0 | -4.0 to 0.0 | The default is 0 or below, so the /4 to ×4 rule can't apply. |
| `sig_hrv_z` | -1.5 | -4.0 to 0.0 | The same. |
| `strength_ref` | 0 | 0 to 600 | The same. Reference strength-training minutes per week. |
| `forecast_growth` | 1.10 | 1.01 to 1.10 | Weekly long-run growth. It must stay above 1, and must never loosen past the evidence. |
| `forecast_cutback_every` | 4 | 2 to 4 | A cutback week at least every 4th week, and never every week. |
| `safety_rhr_bpm` | 100 | 80 to 100 | The resting-HR alert can only get tighter. |
| `safety_spo2_pct` | 92 | 92 to 96 | The overnight SpO2 alert can only get tighter. |
| `spike_pct` | 10 | 5 to 10 | Single-run spike flag: % over the longest run in 30 days. |
| `weight_rate_max_pct` | 1.0 | 0.5 to 1.0 | Weekly weight-loss limit, as % of body weight. |
| `forecast_first_run_km` | 3.0 | 1.0 to 5.0 | The first run of a plan that starts from no runs: a short, safe start. |

### Safety rails

Every param that the safety rails or the strain signals read has a fixed direction. This list lives in the package
(`SAFETY_DIRECTIONS` in `analysis/evidence.py`), not in your config, so no config can loosen a rail.

| Direction | Params | What it means |
|---|---|---|
| **locked** (no override) | every `plaus_*` param; `safety_window_days`, `weekly_lookback_min_days`, `spike_lookback_days`, `weight_rate_window_days`, `min_n_signals`; `floor_rhr`, `floor_resp`, `floor_temp`, `floor_spo2`, `floor_hrv` | These decide what counts as a real reading, or they define a rail: its look-back window, how much history a baseline needs, and the variance floors behind the z-scores. |
| **lower** only | `safety_rhr_bpm`, `safety_rhr_nights`, `safety_spo2_nights`, `safety_strain_days`, `sig_rhr_z`, `sig_rhr_bpm`, `sig_resp_br`, `sig_resp_z`, `sig_temp_dev_c`, `sig_temp_z`, `sig_spo2_drop`, `cusum_k`, `cusum_h`, `strain_persist_nights`, `weight_min_weighins`, `hrv_band_halfwidth_sd`, `forecast_growth`, `forecast_cutback_every`, `spike_pct`, `weight_rate_max_pct` | The alarm fires when a reading reaches the level, or the plan stays conservative. A lower value makes the alarm fire sooner, or the plan safer. A narrower HRV band reports "below" sooner. |
| **higher** only | `safety_spo2_pct`, `sig_hrv_z` (toward 0), `strain_max_skipped_nights`, `forecast_taper_weeks` | The alarm fires when a reading falls below the level, so a higher level fires sooner. More tolerated missing nights keep the strain tracker running instead of resetting it. A longer taper starts the build earlier, so the plan gets more conservative. |

A rail override must also stay within its bounds. For example, `safety_rhr_bpm` can go from 100 down to 80, but never
above 100.

## The coach: persona and prompts

The coach's instructions are built in this order:
1. **About you**, generated from your profile: your sex (unless `unspecified`), goals, race (if any) and units.
2. **Persona**: your `config/coach/persona.md`, or the built-in neutral persona if the file is missing or empty. If
   the file can't be read, the default is used and the problem is reported.
3. **Core rules**: safety first, never invent data, honest confidence, the hard limits, data caveats and memory.
   They always come last, and no config can remove or change them.

A persona sets tone and style only. For example:

```markdown
Style: calm and supportive. Lead with one number, then one small suggestion. Briefs are 120 words or fewer.
```

### Prompts (`config/coach/prompts/<name>.md`)

A file named after a built-in prompt replaces it. Any other name adds a new prompt. The built-in prompts are
`morning_checkin`, `weekly_review` (argument `week_ending`), `plan_next_run` and `explain_score` (arguments `score`
and `date`).

```markdown
args: focus, week_ending
Review the week ending {week_ending} with a focus on {args.focus}. Save any targets with review_on="{next_week}".
```

- **File names:** lowercase letters, digits and `_`, starting with a letter (at most 40 characters), plus `.md`.
- **The `args:` line** is optional, and must be the first line. It declares the prompt's arguments, comma-separated.
  Names follow the file-name rule and can't be Python keywords or repeated. Claude shows them as the prompt's
  arguments, and they are all optional.
- **Placeholders:**

  | Placeholder | Value |
  |---|---|
  | `{today}` | today's date, YYYY-MM-DD |
  | `{yesterday}` | the day before today |
  | `{week_ending}` | the `week_ending` argument if given, else today |
  | `{next_week}` | `week_ending` + 7 days |
  | `{args.<name>}` | the argument `<name>`, which must be on the `args:` line (empty if not given) |

  Using `{week_ending}` or `{next_week}` declares the `week_ending` argument for you. If its value isn't a valid
  date, the coach asks you for one instead of guessing.
- **Not allowed:** any other placeholder, and format options such as `{today:%d}` or `{today!r}`. Both are
  `config check` errors. Write `{{` and `}}` for literal braces.
- If you replace a built-in prompt that takes arguments, declare the same arguments on the `args:` line. Otherwise
  the problem is reported at startup.
- A prompt file with an error is skipped (the built-in prompt stays) and listed in `data_status`.

Prompts can't weaken the coach: the core rules live in the server instructions, not in the prompts.

## Dashboard (`config/dashboard.toml`)

```toml
tabs = ["today", "trends", "sleep", "activity", "running", "bodyage"]  # order; leave a tab out to hide it
today_cards = ["plan", "sleep_debt", "activity", "body"]
trend_metrics = ["rhr", "hrv_7d", "sleep_debt_h", "steps_7d", "mvpa_week", "sedentary_h", "weight_trend_kg",
                 "score:recovery", "score:sleep"]
default_range_days = 30          # 7 | 30 | 90 | 365

[theme]                          # optional colour overrides, #rrggbb
dark = {recovery = "#3ed68c"}
light = {}
```

| Key | Allowed values | Default |
|---|---|---|
| `tabs` | `today`, `trends`, `sleep`, `activity`, `running`, `bodyage` (at least one) | all six, in that order |
| `today_cards` | `plan`, `sleep_debt`, `activity`, `body` | all four |
| `trend_metrics` | `rhr`, `hrv_7d`, `sleep_debt_h`, `steps_7d`, `mvpa_week`, `sedentary_h`, `weight_trend_kg`, `bmi`, `score:recovery`, `score:sleep`, `score:body_age`, and `plugin:<name>` for a metric of an enabled plugin | the list in the example above; at most 12 |
| `default_range_days` | `7`, `30`, `90`, `365` | `30` |
| `theme.dark`, `theme.light` | colour tokens `bg`, `panel`, `panel-2`, `line`, `fg`, `muted`, `faint`, `recovery`, `sleep`, `age`, `run`, `ok`, `warn`, `bad`, `track`, `tip` | no overrides |

An unknown key, tab, card, metric or token is a `config check` error. A repeated name is dropped, and the order is
kept.

```bash
uv run fitbit-air dashboard build [--out FILE]   # default: ~/.fitbit-air/data/dashboard/dashboard.html
```

The built page is the template with your layout embedded in it. It holds no personal data, because the config holds
layout only. `build` refuses to write while `dashboard.toml` has errors. Publishing it as your private Claude Artifact
is described in `dashboard/README.md`.

## Moving an old install: `home migrate`

If your data still lives in the repo (`<repo>/data`, `<repo>/secrets`), move it to your private home:

```bash
uv run fitbit-air home migrate            # to ~/.fitbit-air
uv run fitbit-air home migrate --to PATH  # somewhere else (then set FITBIT_AIR_HOME to it)
```

**Run it from the checkout you'll keep using.** The background sync and the Claude registrations will run that code.
- It refuses to run from a linked git worktree, or for a source home other than this checkout, unless you pass
  `--force`.
- `--to` is made absolute, and must be outside the code folder.
- Before anything happens, it shows the source, the target and the code folder, and asks you to confirm. `--yes`
  skips the question.

**Step 0, before you confirm: quit Claude Desktop (Cmd+Q) and close any Claude Code sessions that use
fitbit-coach.** A coach still running on the old home could write a journal entry or note after the copy.

It then works in this order, and prints each step:
1. It takes the sync lock. If a sync is running, it stops and asks you to try again in a few minutes.
2. It refuses if the target already has a database that it didn't copy itself.
3. It copies the files: the rest of `data/` (everything except the database, its side files and the lock),
   `secrets/`, and `data/profile.toml` to `config/profile.toml`. If the target already has a different file at the
   same place, that file is renamed to `<name>.before-migrate-<date>` first, and your old file wins. An example is a
   profile from `init`.
4. It copies the database with SQLite's backup API. This gives a consistent copy, even while the database is in use.
5. It checks the copy: SQLite's `integrity_check` must pass, and every table must have the same number of rows. If
   not, it stops before switching anything.
6. It switches the background sync:
   - It stops any old `*fitbit-air.sync` launchd job, and checks with `launchctl print` that the job is really gone.
     If the job is still loaded, it stops there.
   - It moves the old job's plist into `<home>/launchd-backup/`, never overwriting an earlier one.
   - It installs `io.github.fitbit-air.sync`, which runs this checkout's code with `FITBIT_AIR_HOME` set to the new
     home.
7. It updates the Claude Code registration. It removes `fitbit-coach` at user and local scope, then adds it with
   `add-json`:
   ```bash
   claude mcp add-json -s user fitbit-coach '{"type": "stdio", "command": "/absolute/path/to/uv",
     "args": ["--directory", "<repo>", "run", "--frozen", "fitbit-air", "mcp"],
     "env": {"FITBIT_AIR_HOME": "<your home folder>/.fitbit-air"}}'
   ```
   The JSON is a single argument, so nothing can be misread as the server name. If `claude` isn't on your PATH, it
   prints this command for you to run. The Claude Desktop app bundles the `claude` command. If you registered
   fitbit-coach inside a project, check `claude mcp list` there.
8. It prints the Claude Desktop step: with Desktop quit, run
   `FITBIT_AIR_HOME=<home> python3 <repo>/scripts/register_claude_desktop.py`.
9. It checks the old database once more. If it gained rows after the copy, it warns you and names the tables. Those
   rows stay in the old folder.
10. Only then does it rename the old `data/` and `secrets/` to `data.migrated-<date>` and `secrets.migrated-<date>`.
    Git ignores both folders.

**Nothing is ever deleted.** Delete the `*.migrated-*` folders yourself once you are happy.

**If a step fails,** fix the cause and run `fitbit-air home migrate` again. It carries on from the step that failed.
- **Progress** is kept in `<home>/.migrate-state.json`. The command reads it first, so a re-run finds the
  unfinished migration and its source, even though the new home already holds a copy.
- **Until the background sync has been switched,** the old home stays the live one: the CLI, the coach and the old
  sync job keep using it, and the unverified copy is not used.
- **A re-run before that point** copies and verifies again, so the new home is never older than the old one. If a
  file or the database in the new home changed since the last copy, it is kept as `*.before-migrate-<date>` before
  being replaced, and a step says so.
- **After the switch,** the new home is live and is not copied over again.
- **Running it again after it has finished** does nothing.
