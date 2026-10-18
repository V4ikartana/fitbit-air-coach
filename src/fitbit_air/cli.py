"""Command-line entry point: `fitbit-air`."""
from __future__ import annotations

import logging
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import typer

try:  # imported defensively: both come in transitively and only matter for friendlier messages
    from oauthlib.oauth2.rfc6749.errors import OAuth2Error
except ImportError:  # pragma: no cover
    class OAuth2Error(Exception):  # type: ignore[no-redef]
        pass
try:
    from keyring.errors import KeyringError
except ImportError:  # pragma: no cover
    class KeyringError(Exception):  # type: ignore[no-redef]
        pass

from . import auth as auth_mod
from . import normalize
from . import schedule as schedule_mod
from .analysis import report as analysis_report
from .analysis.profile import load_profile, profile_path, set_value
from .analysis.runner import run_analysis as _run_analysis
from .client import HealthClient
from .config import CODE_ROOT, DEFAULT_HOME, Paths, get_paths
from .errors import NormalizeError
from .lock import SyncLock
from .notify import notify
from .registry import BY_NAME, REGISTRY
from .store import Store
from .sync import Syncer
from .timeutil import UTC, iso_utc
from .userconfig import read_profile, registry_for

app = typer.Typer(help="Sync Fitbit Air data from the Google Health API into a local database.", no_args_is_help=True)
schedule_app = typer.Typer(help="Manage the background sync job (every 2 hours).", no_args_is_help=True)
app.add_typer(schedule_app, name="schedule")
profile_app = typer.Typer(help="Show or edit your analysis profile (your profile file; see `fitbit-air config show`).",
                          no_args_is_help=True)
app.add_typer(profile_app, name="profile")
dashboard_app = typer.Typer(help="Build your dashboard page from config/dashboard.toml.", no_args_is_help=True)
app.add_typer(dashboard_app, name="dashboard")
config_app = typer.Typer(help="Show or check your configuration (<home>/config).", no_args_is_help=True)
app.add_typer(config_app, name="config")
home_app = typer.Typer(help="Manage your private home folder.", no_args_is_help=True)
app.add_typer(home_app, name="home")
log = logging.getLogger(__name__)


@app.callback()
def _main() -> None:
    os.umask(0o077)  # health data is private: new DB, WAL, log and lock files are 0600, directories 0700


def _setup_logging(paths: Paths) -> None:
    root = logging.getLogger()
    if any(getattr(h, "_fitbit_air", False) for h in root.handlers):
        return
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(paths.logs_dir / "sync.log", maxBytes=5_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler._fitbit_air = True
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    if sys.stderr.isatty():  # interactive run: show progress on the console too
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(logging.INFO)
        console.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
        console._fitbit_air = True
        root.addHandler(console)
    # httpx/httpcore log every request at INFO; keep the sync log readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@contextmanager
def _friendly_errors():
    """Turn expected operational failures into a one-line message and exit code 1 (no traceback)."""
    try:
        yield
    except (subprocess.CalledProcessError, RuntimeError, FileNotFoundError, OSError, OAuth2Error, KeyringError) as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(1) from exc


def _make_client() -> HealthClient:
    return HealthClient(auth_mod.TokenProvider())


@app.command()
def auth() -> None:
    """Log in with Google in your browser; the token is stored in the macOS Keychain."""
    with _friendly_errors():
        creds = auth_mod.login(get_paths())
    try:  # a locked database must never fail a login that already succeeded
        store = Store(get_paths().db)
        try:
            store.set_meta("auth_completed_at", iso_utc(datetime.now(UTC)))  # shown by the coach's data_status
        finally:
            store.close()
    except sqlite3.Error as exc:
        typer.echo(f"Note: could not record login time ({exc})")
    typer.echo("Logged in. Token saved to the macOS Keychain.")
    missing = auth_mod.missing_scopes(creds)
    if missing:
        typer.echo("\nWARNING: some permissions were not granted, so those data types will not sync:")
        for scope in missing:
            typer.echo(f"  - {scope}")
        typer.echo("Run `fitbit-air auth` again and tick every permission box on the Google consent screen.")


@app.command()
def sync(
    backfill: bool = typer.Option(False, "--backfill", help="Re-walk the full history for every type."),
    types: str | None = typer.Option(None, "--types", help="Comma-separated data types, e.g. heart-rate,sleep"),
) -> None:
    """Fetch new data (and any unfinished history) into the local database.

    Exit codes: 0 ok, 1 some data type failed, 2 Google login needs attention.
    """
    paths = get_paths()
    _setup_logging(paths)
    selected = [t.strip() for t in types.split(",") if t.strip()] if types else None
    if selected:
        unknown = [t for t in selected if t not in BY_NAME]
        if unknown:
            typer.echo(f"Unknown data types: {', '.join(unknown)}. Valid: {', '.join(BY_NAME)}")
            raise typer.Exit(1)
    with SyncLock(paths.lock_file) as acquired:
        if not acquired:
            typer.echo("Sync already running; exiting.")
            return
        store = Store(paths.db)
        client = None
        try:
            client = _make_client()
            result = Syncer(store, client).run(types=selected, full_backfill=backfill)
            if not result.reauth_required:
                try:
                    _run_analysis(store, paths)
                except Exception:  # analysis must never fail a sync; the error is recorded for `status`
                    log.exception("analysis after sync failed")
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                close()
            store.close()
    for r in result.results:
        typer.echo(f"{r.name:38} {r.status:12} {r.points:>9,}" + (f"  {r.error}" if r.error else ""))
    if result.reauth_required:
        notify("Fitbit Air sync", "Google login needs attention — run: fitbit-air auth")
        reason = next((r.error for r in result.results if r.error and r.error.startswith("reauth")), None)
        typer.echo("Google login needs attention. Run: fitbit-air auth")
        if reason:
            typer.echo(f"Reason: {reason}")
        raise typer.Exit(2)
    if any(r.status == "failed" for r in result.results):
        raise typer.Exit(1)


def _load_plugins_for(paths: Paths):
    """The plugins enabled in the profile; a profile that cannot be read means none (reported by `config check`)."""
    from .plugins import load_plugins
    try:
        enabled = load_profile(profile_path(paths)).plugins_enabled
    except (ValueError, OSError):
        enabled = False
    return load_plugins(paths, enabled), enabled


def _echo_plugins(paths: Paths) -> None:
    plugins, enabled = _load_plugins_for(paths)
    if not enabled:
        return
    typer.echo(f"Plugins: {len(plugins.files)} loaded, {len(plugins.problems)} problems")
    for problem in plugins.problems:
        typer.echo(f"  {problem}")


@app.command()
def status() -> None:
    """Show login state, last run, per-type progress and row counts."""
    store = Store(get_paths().db)
    try:
        typer.echo(f"Auth: {auth_mod.auth_state()}")
        run = store.last_run()
        if run is None:
            typer.echo("Last run: never")
        else:
            typer.echo(
                f"Last run: {run['started_at']} → {run['finished_at'] or 'running'} ({run['mode']}), "
                f"{run['points_fetched'] or 0:,} points, {run['types_failed'] or 0} failed"
                + (f" — {run['error_summary']}" if run["error_summary"] else "")
            )
        analysis = store.last_analysis()
        if analysis is None:
            typer.echo("Analysis: never")
        elif analysis["finished_at"] is None:
            typer.echo(f"Analysis: last {analysis['started_at']}, did not finish (interrupted or still running)")
        elif analysis["error"]:
            typer.echo(f"Analysis: last {analysis['started_at']}, ERROR {analysis['error']}"
                       + (f" (wrote: {analysis['summary'].split('; ')[0]})" if analysis["summary"] else ""))
        else:
            typer.echo(f"Analysis: last {analysis['started_at']}, {analysis['summary'] or ''}")
        _echo_plugins(get_paths())
        typer.echo("\nData types:")
        states = {s.data_type: s for s in store.all_states()}
        for dt in REGISTRY:
            s = states.get(dt.name)
            if s is None:
                typer.echo(f"  {dt.name:38} never synced")
                continue
            upto = iso_utc(s.watermark_utc) if s.watermark_utc else "-"
            earliest = iso_utc(s.earliest_data_utc)[:10] if s.earliest_data_utc else "-"
            line = (f"  {dt.name:38} {s.status or '-':12} up to {upto}  from {earliest}  "
                    f"backfill {'done' if s.backfill_done else 'in progress'}")
            if s.deep_scan_utc:
                line += f"  deep re-scan {iso_utc(s.deep_scan_utc)}"
            typer.echo(line + (f"  ERROR {s.last_error}" if s.last_error else ""))
        pending = {dt.name: len(store.pending_dates(dt.name)) for dt in REGISTRY}
        for name, n in pending.items():
            if n:
                typer.echo(f"  {name:38} pending rebuild: {n} dates (run: fitbit-air rebuild)")
        typer.echo("\nRows:")
        for table, n in store.table_counts().items():
            typer.echo(f"  {table:20} {n:>12,}")
    finally:
        store.close()


@dashboard_app.command("build")
def dashboard_build(out: str | None = typer.Option(None, "--out", help="Output file")) -> None:
    """Write data/dashboard/dashboard.html (layout from config; no personal data inside)."""
    from .dashboard_build import build_dashboard

    paths = get_paths()
    try:
        plugins, _ = _load_plugins_for(paths)
        written = build_dashboard(paths, plugins.metric_meta(), Path(out) if out else None)
    except (ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(1) from exc
    typer.echo(f"Dashboard written to {written}. Ask Claude to publish it as your Artifact "
               "(see dashboard/README.md).")


@app.command()
def rebuild(since: str | None = typer.Option(None, "--since", help="Only local dates on/after YYYY-MM-DD")) -> None:
    """Regenerate clean tables from the raw layer (after a normalizer change)."""
    if since is not None:
        try:
            date.fromisoformat(since)
        except ValueError:
            raise typer.BadParameter("use YYYY-MM-DD") from None
    paths = get_paths()
    failed = False
    with SyncLock(paths.lock_file) as acquired:
        if not acquired:
            typer.echo("A sync is running; try again shortly.")
            raise typer.Exit(1)
        store = Store(paths.db)
        try:
            for dt in REGISTRY:
                # Snapshot first: anything that becomes pending during the rebuild must stay pending.
                pending = [d for d in store.pending_dates(dt.name) if since is None or d >= since]
                pending_error = store.pending_error(dt.name)
                try:
                    # Pending dates are included even with no raw rows, so their stale clean rows are removed too.
                    dates = sorted(set(store.raw_dates(dt.name, since)) | set(pending))
                    n = normalize.rebuild(store, dt, dates)
                except NormalizeError as exc:
                    typer.echo(f"{dt.name:38} FAILED: {exc}")
                    failed = True
                    continue
                store.clear_pending(dt.name, pending)
                if pending and not store.pending_dates(dt.name):
                    state = store.get_state(dt.name)
                    if state and state.status == "failed" and state.last_error == pending_error:
                        state.status, state.last_error = "ok", None  # the failure was only the pending rebuild
                        store.save_state(state)
                if n:
                    typer.echo(f"{dt.name:38} {n:>9,} rows")
        finally:
            store.close()
    if failed:
        raise typer.Exit(1)


@schedule_app.command("install")
def schedule_install() -> None:
    """Install and start the launchd agent."""
    paths = get_paths()
    if paths.legacy:
        typer.echo("Error: your data is still in the old repo-root home; run `fitbit-air home migrate` instead "
                   "(it moves your data and installs the background sync).")
        raise typer.Exit(1)
    with _friendly_errors():
        path = schedule_mod.install(paths)
    typer.echo(f"Installed {path}. Syncing every 2 hours, at login, and after wake if a run was missed.")


@schedule_app.command("uninstall")
def schedule_uninstall() -> None:
    """Stop and remove the launchd agent."""
    with _friendly_errors():
        for step in schedule_mod.uninstall(paths=get_paths()):
            typer.echo(f"- {step}")
    typer.echo("Background sync removed.")


def _parse_date(value: str | None, option: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter("use YYYY-MM-DD", param_hint=option) from None


@app.command()
def analyze(since: str | None = typer.Option(
        None, "--since", help="Recompute from this date (YYYY-MM-DD); default 60 days ago")) -> None:
    """Recompute baselines, scores and findings from the synced data."""
    paths = get_paths()
    start = _parse_date(since, "--since")
    _setup_logging(paths)
    with SyncLock(paths.lock_file) as acquired:
        if not acquired:
            typer.echo("A sync is running; try again shortly.")
            raise typer.Exit(1)
        store = Store(paths.db)
        try:
            summary = _run_analysis(store, paths, start)
        except (ValueError, KeyError) as exc:
            typer.echo(f"Error: {exc}")
            raise typer.Exit(1) from exc
        except Exception as exc:
            log.exception("analysis failed")
            typer.echo(f"Error: {exc!r}")
            raise typer.Exit(1) from exc
        finally:
            store.close()
    typer.echo(f"Analysed {summary}.")
    for problem in getattr(summary, "config_errors", []):
        typer.echo(f"Config problem: {problem}")


@app.command()
def report(day: str | None = typer.Option(None, "--date", help="Report date (YYYY-MM-DD); default today")) -> None:
    """Show the day's safety checks, scores with their components, and findings."""
    target = _parse_date(day, "--date") or date.today()
    paths = get_paths()
    reg, problems = registry_for(paths)
    store = Store(paths.db)
    try:
        typer.echo(analysis_report.render(store, target.isoformat(), reg=reg))
    finally:
        store.close()
    for problem in read_profile(paths)[1] + problems:
        typer.echo(f"Config problem: {problem}")


@app.command()
def snapshot(out: str | None = typer.Option(
        None, "--out", help="Output file; default data/dashboard/snapshot.json")) -> None:
    """Write the dashboard's daily snapshot (the numbers shown when live data can't be reached)."""
    from .coach import snapshot as snapshot_mod
    from .coach.tools import Ctx

    paths = get_paths()
    if not paths.db.exists():
        typer.echo(f"Error: database not found at {paths.db}; run `fitbit-air sync` first")
        raise typer.Exit(1)
    target = Path(out) if out else paths.data_dir / "dashboard" / "snapshot.json"
    store = Store(paths.db)
    try:
        reg, problems = registry_for(paths)
        plugins, _ = _load_plugins_for(paths)
        ctx = Ctx(store=store, reg=reg, now=datetime.now(UTC), today=date.today(), paths=paths,
                  config_problems=[str(p) for p in problems], plugins=plugins)
        written = snapshot_mod.write_snapshot(ctx, target)
    except (ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(1) from exc
    finally:
        store.close()
    typer.echo(f"Snapshot written to {written}")


@profile_app.command("show")
def profile_show() -> None:
    """Print the profile."""
    path = profile_path(get_paths())
    try:
        load_profile(path)  # creates it with defaults on first use
    except ValueError as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(1) from exc
    typer.echo(path.read_text().rstrip())


@profile_app.command("set")
def profile_set(key: str, value: str) -> None:
    """Set one profile value, e.g. `fitbit-air profile set sleep_need_h 8`."""
    try:
        set_value(profile_path(get_paths()), key, value)
    except ValueError as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(1) from exc
    typer.echo(f"Set {key} = {value}")


@app.command()
def init(template: str = typer.Option("minimal", "--template", help="minimal | runner-10k | sleep-recovery"),
         home: str | None = typer.Option(None, "--home", help="Home folder; default ~/.fitbit-air")) -> None:
    """Create your private home folder (data, secrets, config, plugins) from a starter template."""
    from .home import init_home

    if home:
        paths = Paths(Path(home).expanduser())
    else:
        env = os.environ.get("FITBIT_AIR_HOME")
        paths = Paths(Path(env).expanduser() if env else DEFAULT_HOME.expanduser())
    if get_paths().legacy or (not home and (CODE_ROOT / "data" / "health.db").exists()):
        typer.echo("Error: your data is still in the old repo-root home; run `fitbit-air home migrate` instead.")
        raise typer.Exit(1)
    if paths.root.resolve() == CODE_ROOT.resolve():
        typer.echo("Error: refusing to use the code folder as a home; choose another folder with --home.")
        raise typer.Exit(1)
    try:
        created = init_home(paths, template)
    except ValueError as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(1) from exc
    typer.echo(f"Home ready at {paths.root} ({len(created)} new files from '{template}').")
    steps = []
    if paths.root.resolve() != DEFAULT_HOME.expanduser().resolve():
        steps.append(f"export FITBIT_AIR_HOME={shlex.quote(str(paths.root))}   (add it to your shell profile)")
    steps.append(f"put your Google OAuth client file at {paths.root / 'secrets' / 'client_secret.json'}")
    steps.append("run `fitbit-air auth`, then `fitbit-air sync`")
    typer.echo("Next:")
    for n, step in enumerate(steps, 1):
        typer.echo(f"  {n}. {step}")


@config_app.command("check")
def config_check() -> None:
    """Validate every config file and plugin; exit 1 if anything is wrong."""
    from .userconfig import check_all

    problems = check_all(get_paths())
    for p in problems:
        typer.echo(str(p))
    if problems:
        raise typer.Exit(1)
    typer.echo("Config OK.")


@config_app.command("show")
def config_show() -> None:
    """Print the effective configuration and where each value comes from."""
    from .userconfig import show_all

    for line in show_all(get_paths()):
        typer.echo(line)


@home_app.command("migrate")
def home_migrate(to: str | None = typer.Option(None, "--to", help="New home; default ~/.fitbit-air"),
                 yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation"),
                 force: bool = typer.Option(False, "--force",
                                            help="Allow a linked git worktree, or a source other than this checkout")
                 ) -> None:
    """Move an old repo-root home (data, secrets, profile) to your private home, then switch the background sync.
    Re-run it after a failure: it resumes from the step that failed."""
    from . import home as home_mod

    dst = Paths((Path(to).expanduser() if to else DEFAULT_HOME.expanduser()).resolve())
    state_file = home_mod._state_file(dst)
    try:
        state = home_mod._state(dst)
    except (OSError, ValueError) as exc:
        typer.echo(f"Error: cannot read {state_file}: {exc}")
        raise typer.Exit(1) from exc
    if isinstance(state, dict) and state and not state.get("done"):
        if not isinstance(state.get("source"), str):
            typer.echo(f"Error: {state_file} does not name the source home; cannot resume")
            raise typer.Exit(1)
        src = Paths(Path(state["source"]), legacy=True)  # whatever get_paths() says now: the copy may exist already
        typer.echo(f"Resuming the unfinished migration from {src.root}.")
    else:
        src = get_paths()
        if not src.legacy and not to:
            typer.echo(f"Nothing to migrate: your home is already {src.root}.")
            return
    code = CODE_ROOT.resolve()
    if dst.root.is_relative_to(code):
        typer.echo(f"Error: the target {dst.root} is inside the code folder {CODE_ROOT}; choose a home outside it.")
        raise typer.Exit(1)
    problems = []
    if (CODE_ROOT / ".git").is_file():
        problems.append(f"{CODE_ROOT} is a linked git worktree; the background sync and Claude would run its code")
    if src.root.resolve() != code:
        problems.append(f"the source {src.root} is not this checkout ({CODE_ROOT})")
    if problems and not force:
        for problem in problems:
            typer.echo(f"Error: {problem}.")
        typer.echo("Run it from the checkout you will keep using, or pass --force.")
        raise typer.Exit(1)
    typer.echo("This moves your home folder:")
    typer.echo(f"  source: {src.root}")
    typer.echo(f"  target: {dst.root}")
    typer.echo(f"  code:   {CODE_ROOT}  (the background sync and Claude will run this checkout)")
    typer.echo("0. Quit Claude Desktop (Cmd+Q) and close Claude Code sessions that use fitbit-coach before continuing.")
    if not yes:
        typer.confirm("Continue?", abort=True)
    try:
        for step in home_mod.migrate_home(src, dst, uv=shutil.which("uv"), claude=shutil.which("claude")):
            typer.echo(f"- {step}")
    except (home_mod.MigrateError, OSError, subprocess.CalledProcessError, sqlite3.Error) as exc:
        typer.echo(f"Error: {exc}  (safe to re-run; progress is kept)")
        raise typer.Exit(1) from exc


@app.command()
def mcp() -> None:
    """Run the coach MCP server over stdio (Claude Desktop and Claude Code start this; it prints nothing else)."""
    from .coach import server  # imported lazily so the CLI doesn't load the MCP SDK for other commands

    server.main()
