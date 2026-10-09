"""Command-line interface.

Every command that mutates state runs under the exclusive file lock, so a
manual invocation can never race a systemd timer. Commands are thin wrappers:
the logic lives in `pipeline/`, which keeps them testable without Typer.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .config import describe_config, format_config_error, get_config
from .locking import AlreadyRunning, exclusive_lock
from .logging import get_logger, setup_logging

app = typer.Typer(
    name="ycs",
    help="yt-crypto-signals — track, verify and score crypto calls made on YouTube.",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="Database maintenance.")
app.add_typer(db_app, name="db")

log = get_logger("cli")

NOT_BUILT = "Not implemented yet — scheduled for a later build checkpoint."


@app.callback()
def main(
    log_level: Annotated[str, typer.Option(help="DEBUG, INFO, WARNING, ERROR.")] = "INFO",
    json_logs: Annotated[
        bool | None,
        typer.Option("--json-logs/--human-logs", help="Default: JSON when not a TTY."),
    ] = None,
) -> None:
    setup_logging(level=log_level, json_output=json_logs)


def _version_cb(value: bool) -> None:
    if value:
        typer.echo(f"yt-crypto-signals {__version__}")
        raise typer.Exit


@app.command()
def version(
    _v: Annotated[bool, typer.Option("--version", callback=_version_cb, is_eager=True)] = False,
) -> None:
    """Print the version."""
    typer.echo(f"yt-crypto-signals {__version__}")


# --------------------------------------------------------------------------
# Diagnostics — implemented now, they are how you validate a fresh VPS
# --------------------------------------------------------------------------


@app.command("config-check")
def config_check() -> None:
    """Validate .env and config.yaml, then print a redacted summary.

    Run this first on a new host: it fails loudly on a missing key or a bad
    timezone instead of letting the first timer tick discover the problem.
    """
    try:
        cfg = get_config()
    except Exception as exc:
        typer.secho(format_config_error(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(describe_config(cfg), indent=2))
    typer.secho("Configuration OK.", fg=typer.colors.GREEN)


@app.command()
def stats() -> None:
    """Row counts and pipeline state distribution."""
    from sqlalchemy import func
    from sqlmodel import select

    from .db import session_scope, table_counts
    from .models import Video

    counts = table_counts()
    with session_scope() as session:
        states = session.exec(select(Video.state, func.count()).group_by(Video.state)).all()

    typer.echo(json.dumps({"tables": counts, "video_states": dict(states)}, indent=2, default=str))


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    """Apply Alembic migrations."""
    from alembic import command
    from alembic.config import Config as AlembicConfig

    from .config import REPO_ROOT

    alembic_cfg = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    command.upgrade(alembic_cfg, revision)
    typer.secho(f"Database upgraded to {revision}.", fg=typer.colors.GREEN)


@db_app.command("current")
def db_current() -> None:
    """Show the current migration revision."""
    from alembic import command
    from alembic.config import Config as AlembicConfig

    from .config import REPO_ROOT

    command.current(AlembicConfig(str(REPO_ROOT / "alembic.ini")), verbose=True)


@db_app.command("backup")
def db_backup(
    dest: Annotated[Path | None, typer.Option(help="Destination file.")] = None,
) -> None:
    """Consistent online backup via SQLite's backup API."""
    from .db import backup_to
    from .models import utcnow

    cfg = get_config()
    target = dest or (cfg.settings.data_dir / "backups" / f"ycs-{utcnow():%Y%m%d-%H%M%S}.db")
    path = backup_to(target, cfg)
    typer.secho(f"Backed up to {path}", fg=typer.colors.GREEN)


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


@app.command()
def run(
    stages: Annotated[
        str | None, typer.Option(help="Comma-separated subset, e.g. 'discover,transcribe'.")
    ] = None,
) -> None:
    """Run the full pipeline once (what the 30-minute timer calls)."""
    cfg = get_config()
    try:
        with exclusive_lock(cfg.settings.lock_path):
            raise NotImplementedError(f"run: {NOT_BUILT}")
    except AlreadyRunning as exc:
        log.info("skipping run", extra={"reason": str(exc)})
        raise typer.Exit(0) from exc


@app.command()
def backfill(
    days: Annotated[int, typer.Option(help="How far back to look.")] = 30,
) -> None:
    """Backfill recent history. Videos are processed but not alerted
    individually — one summary message is sent at the end."""
    cfg = get_config()
    try:
        with exclusive_lock(cfg.settings.lock_path):
            raise NotImplementedError(f"backfill: {NOT_BUILT}")
    except AlreadyRunning as exc:
        log.info("skipping backfill", extra={"reason": str(exc)})
        raise typer.Exit(0) from exc


@app.command()
def process(
    video: Annotated[str, typer.Option(help="YouTube URL or 11-character video id.")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run/--commit", help="Print JSON; send and store nothing.")
    ] = True,
) -> None:
    """Process one video end to end. With --dry-run, prints the extracted and
    verified calls (plus rejections and their reasons) and sends nothing."""
    raise NotImplementedError(f"process: {NOT_BUILT}")


@app.command()
def reprocess(
    prompt_version: Annotated[str, typer.Option(help="Prompt version to run, e.g. v2.")],
    since: Annotated[
        datetime | None, typer.Option(help="Only videos published on/after this.")
    ] = None,
    limit: Annotated[int | None, typer.Option(help="Cap the number of videos.")] = None,
) -> None:
    """Re-extract stored transcripts with a different prompt version.

    Adds a new extraction run per video; previous runs are retained so the two
    prompts can be compared.
    """
    raise NotImplementedError(f"reprocess: {NOT_BUILT}")


@app.command()
def score() -> None:
    """Score calls whose timeframe has elapsed (daily timer)."""
    raise NotImplementedError(f"score: {NOT_BUILT}")


@app.command()
def scorecard(
    send: Annotated[
        bool, typer.Option("--send/--print", help="Queue to Telegram or print.")
    ] = False,
) -> None:
    """Weekly channel and speaker league table (Sunday timer)."""
    raise NotImplementedError(f"scorecard: {NOT_BUILT}")


@app.command()
def health() -> None:
    """Check for stuck stages, transcript failures, dead proxy and low quota."""
    raise NotImplementedError(f"health: {NOT_BUILT}")


# --------------------------------------------------------------------------
# Connectivity tests
# --------------------------------------------------------------------------


@app.command("test-telegram")
def test_telegram() -> None:
    """Send a test message through the outbox and report the result."""
    raise NotImplementedError(f"test-telegram: {NOT_BUILT}")


@app.command("test-proxy")
def test_proxy(
    video: Annotated[
        str, typer.Option(help="Video to probe.")
    ] = "https://www.youtube.com/watch?v=n3g_L9QI-9k",
) -> None:
    """Fetch one transcript through each provider and report which worked.

    The first thing to run on a new VPS: it proves the proxy and the transcript
    providers work from that IP before any pipeline run depends on them.
    """
    raise NotImplementedError(f"test-proxy: {NOT_BUILT}")


@app.command("outbox-send")
def outbox_send() -> None:
    """Flush pending outbox rows (also runs at the end of every pipeline run)."""
    raise NotImplementedError(f"outbox-send: {NOT_BUILT}")


def entrypoint() -> None:
    try:
        app()
    except NotImplementedError as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
        sys.exit(2)


if __name__ == "__main__":
    entrypoint()
