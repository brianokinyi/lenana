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
from .utils import human_datetime

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
    from .db import session_scope
    from .pipeline.health import ping_healthcheck
    from .pipeline.orchestrate import run_pipeline

    cfg = get_config()
    only = [s.strip() for s in stages.split(",") if s.strip()] if stages else None
    try:
        with exclusive_lock(cfg.settings.lock_path):
            with session_scope(cfg) as session:
                report = run_pipeline(session, cfg, only=only)
            typer.echo(
                json.dumps(
                    {"stages": report.stages, "outbox": report.outbox, "errors": report.errors},
                    indent=2,
                    default=str,
                )
            )
            if report.ok:
                ping_healthcheck(cfg)
            else:
                raise typer.Exit(1)
    except AlreadyRunning as exc:
        log.info("skipping run", extra={"reason": str(exc)})
        raise typer.Exit(0) from exc


@app.command()
def backfill(
    days: Annotated[int, typer.Option(help="How far back to look.")] = 30,
) -> None:
    """Backfill recent history. Videos are processed but not alerted
    individually — one summary message is sent at the end."""
    from .db import session_scope
    from .pipeline.orchestrate import run_backfill

    cfg = get_config()
    try:
        with exclusive_lock(cfg.settings.lock_path):
            with session_scope(cfg) as session:
                report = run_backfill(session, cfg, days=days)
            typer.echo(
                json.dumps(
                    {"stages": report.stages, "outbox": report.outbox, "errors": report.errors},
                    indent=2,
                    default=str,
                )
            )
            if not report.ok:
                raise typer.Exit(1)
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
    from .pipeline.process import process_video
    from .utils import extract_video_id

    cfg = get_config()
    video_id = extract_video_id(video)
    if not video_id:
        typer.secho(f"Could not parse a video id from {video!r}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    if not dry_run:
        typer.secho(
            "--commit is not implemented yet; the storing path runs through `ycs run`.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(2)

    report = process_video(video_id, cfg)
    typer.echo(json.dumps(report.as_dict(), indent=2, default=str))


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
    from sqlmodel import col, select

    from .db import session_scope
    from .enums import VideoState
    from .llm.client import load_prompt
    from .models import Transcript, Video, utcnow
    from .pipeline.extract import extract as extract_stage
    from .pipeline.verify import verify as verify_stage

    cfg = get_config()
    load_prompt(prompt_version)  # fail fast on a bad version before touching rows

    with exclusive_lock(cfg.settings.lock_path), session_scope(cfg) as session:
        query = (
            select(Video)
            .join(Transcript, col(Transcript.video_pk) == col(Video.id))
            .where(col(Video.state).not_in([VideoState.DISCOVERED, VideoState.SKIPPED]))
        )
        if since is not None:
            floor = since if since.tzinfo else since.replace(tzinfo=utcnow().tzinfo)
            query = query.where(col(Video.published_at) >= floor)
        query = query.order_by(col(Video.published_at).desc())
        if limit:
            query = query.limit(limit)

        videos = list(session.exec(query).all())
        for video in videos:
            # Send each video back to TRANSCRIBED so the normal extract and
            # verify stages do the work — no parallel code path to drift.
            video.state = VideoState.TRANSCRIBED
            video.updated_at = utcnow()
            session.add(video)
        session.commit()

        typer.echo(f"requeued {len(videos)} video(s) for prompt {prompt_version}")
        processed = 0
        while True:
            result = extract_stage(session, cfg, prompt_version=prompt_version)
            if result.processed == 0:
                break
            processed += result.processed
            verify_stage(session, cfg)

    typer.echo(f"re-extracted {processed} video(s) with prompt {prompt_version}")


@app.command()
def score() -> None:
    """Score calls whose timeframe has elapsed (daily timer)."""
    from .db import session_scope
    from .pipeline.score import score as score_stage

    cfg = get_config()
    with exclusive_lock(cfg.settings.lock_path), session_scope(cfg) as session:
        result = score_stage(session, cfg)
    typer.echo(json.dumps(result.as_dict(), indent=2, default=str))


@app.command()
def scorecard(
    send: Annotated[
        bool, typer.Option("--send/--print", help="Queue to Telegram or print.")
    ] = False,
) -> None:
    """Weekly channel and speaker league table (Sunday timer)."""
    from .db import session_scope
    from .enums import OutboxKind
    from .models import utcnow
    from .notify.outbox import enqueue_committed, send_pending
    from .pipeline.scorecard import build_scorecard, render_scorecard

    cfg = get_config()
    with session_scope(cfg) as session:
        card = build_scorecard(session, cfg)
        body = render_scorecard(card)
        if not send:
            typer.echo(body)
            return
        enqueue_committed(
            session,
            dedupe_key=f"scorecard:{utcnow():%Y-W%W}",
            kind=OutboxKind.SCORECARD,
            body=body,
            cfg=cfg,
        )
        summary = send_pending(session, cfg)
    typer.echo(f"sent={summary.sent} failed={summary.failed}")


@app.command()
def health(
    notify: Annotated[
        bool, typer.Option("--notify/--no-notify", help="Queue a Telegram warning on problems.")
    ] = True,
) -> None:
    """Check for stuck stages, transcript failures, dead proxy and low quota."""
    from .db import session_scope
    from .notify.outbox import send_pending
    from .pipeline.health import health as health_stage

    cfg = get_config()
    with session_scope(cfg) as session:
        result = health_stage(session, cfg, notify=notify)
        if notify:
            send_pending(session, cfg)
    typer.echo(json.dumps(result.as_dict(), indent=2, default=str))
    if result.failed:
        raise typer.Exit(1)


# --------------------------------------------------------------------------
# Connectivity tests
# --------------------------------------------------------------------------


@app.command("test-telegram")
def test_telegram(
    direct: Annotated[
        bool,
        typer.Option("--direct/--via-outbox", help="Bypass the outbox and send immediately."),
    ] = False,
    find_chat_id: Annotated[
        bool,
        typer.Option("--find-chat-id", help="List chats that have messaged the bot, then exit."),
    ] = False,
) -> None:
    """Send a test message through the outbox and report the result."""
    from .db import session_scope
    from .enums import OutboxKind
    from .models import utcnow
    from .notify.outbox import enqueue_committed, send_pending
    from .notify.telegram import TelegramClient

    cfg = get_config()
    client = TelegramClient(cfg)

    identity = client.get_me()
    if not identity.ok:
        typer.secho(
            f"Bot token rejected by Telegram: {identity.error}", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(1)
    typer.secho(f"Bot token OK (@{identity.username}).", fg=typer.colors.GREEN)

    if find_chat_id:
        chats = client.discover_chats()
        if not chats:
            typer.secho(
                f"No chats found yet. In Telegram, send any message to @{identity.username} "
                "(or add the bot to a group and post there), then run this again.",
                fg=typer.colors.YELLOW,
            )
            raise typer.Exit(1)
        typer.echo("\nChats that have messaged this bot:")
        for chat in chats:
            marker = (
                "  <- current TELEGRAM_CHAT_ID"
                if str(chat.chat_id) == client.default_chat_id
                else ""
            )
            typer.echo(f"   {chat.chat_id}   {chat.chat_type:<10} {chat.name}{marker}")
        typer.echo("\nPut the id you want in .env as TELEGRAM_CHAT_ID.")
        return

    if str(identity.bot_id) == str(client.default_chat_id):
        typer.secho(
            "TELEGRAM_CHAT_ID is set to the bot's own id, so Telegram will refuse every "
            "message.\n  Run `ycs test-telegram --find-chat-id` to get the right value.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)

    body = (
        "✅ <b>yt-crypto-signals test</b>\n"
        f"   Sent {human_datetime(utcnow().astimezone(cfg.settings.tz))} "
        f"{cfg.settings.display_tz}\n"
        f"   Model: <code>{cfg.settings.anthropic_model}</code>\n"
        f"   Channels: {len(cfg.app.enabled_channels)}"
    )

    if direct:
        result = client.send_message(body)
        if result.ok:
            typer.secho(f"Message sent (id={result.message_id}).", fg=typer.colors.GREEN)
            return
        typer.secho(f"Send failed: {result.error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    # Default path exercises the real machinery: enqueue, then flush.
    key = f"test:{utcnow():%Y%m%d%H%M%S}"
    with session_scope(cfg) as session:
        enqueue_committed(session, key, OutboxKind.TEST, body, cfg)
        summary = send_pending(session, cfg, client=client)

    typer.echo(
        f"claimed={summary.claimed} sent={summary.sent} failed={summary.failed} "
        f"gave_up={summary.gave_up}"
    )
    if summary.sent:
        typer.secho("Outbox round-trip OK.", fg=typer.colors.GREEN)
    else:
        typer.secho("Nothing was sent; see the log lines above.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command("test-proxy")
def test_proxy(
    video: Annotated[
        str, typer.Option(help="Video to probe.")
    ] = "https://www.youtube.com/watch?v=n3g_L9QI-9k",
    all_providers: Annotated[
        bool,
        typer.Option(
            "--all/--first-success", help="Probe every provider, not just until one works."
        ),
    ] = True,
) -> None:
    """Fetch one transcript through each provider and report which worked.

    The first thing to run on a new VPS: it proves the proxy and the transcript
    providers work from that IP before any pipeline run depends on them.
    """
    from .enums import TranscriptProvider
    from .sources.transcripts.registry import build_sources, fetch_transcript
    from .sources.transcripts.ytdlp import YtDlpSource
    from .utils import extract_video_id

    cfg = get_config()
    video_id = extract_video_id(video)
    if not video_id:
        typer.secho(f"Could not parse a video id from {video!r}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    typer.echo(f"Video:          {video_id}")
    typer.echo(f"Proxy provider: {cfg.settings.proxy_provider}")
    if cfg.settings.proxy_provider.value == "none":
        typer.secho(
            "  note: no proxy configured. Fine on a residential IP; most VPS ranges are "
            "blocked by YouTube and will need PROXY_PROVIDER=webshare.",
            fg=typer.colors.YELLOW,
        )
    ytdlp_probe = YtDlpSource(cfg)
    typer.echo(
        f"PO-token provider: "
        f"{'detected' if ytdlp_probe.po_token_provider_configured() else 'not installed'}"
    )
    typer.echo("")

    sources = build_sources(cfg)
    if all_providers:
        # Probe each provider independently so one success does not mask
        # another's failure — on a VPS you want to know about both.
        attempts = []
        succeeded: list[str] = []
        for source in sources:
            outcome = fetch_transcript(video_id, cfg, sources=[source])
            attempts.extend(outcome.attempts)
            if outcome.ok:
                succeeded.append(str(source.name))
    else:
        outcome = fetch_transcript(video_id, cfg, sources=sources)
        attempts = outcome.attempts
        succeeded = [str(a.provider) for a in attempts if a.ok]

    for attempt in attempts:
        if attempt.ok:
            typer.secho(
                f"  OK    {attempt.provider:<24} {attempt.segments} segments "
                f"in {attempt.elapsed_ms} ms",
                fg=typer.colors.GREEN,
            )
        else:
            colour = (
                typer.colors.YELLOW
                if attempt.provider is TranscriptProvider.WHISPER
                else typer.colors.RED
            )
            typer.secho(
                f"  FAIL  {attempt.provider:<24} {attempt.kind}: {(attempt.message or '')[:160]}",
                fg=colour,
            )

    typer.echo("")
    if succeeded:
        typer.secho(f"Working providers: {', '.join(succeeded)}", fg=typer.colors.GREEN)
    else:
        typer.secho(
            "No provider could fetch a transcript from this host.\n"
            "  - blocked        -> configure a Webshare RESIDENTIAL proxy, or install the "
            "bgutil PO-token provider\n"
            "  - no_captions    -> this video genuinely has no captions in the configured "
            "languages; try another video\n"
            "  - unavailable    -> video is private, removed, age-restricted or still live",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)


@app.command("outbox-send")
def outbox_send(
    limit: Annotated[int, typer.Option(help="Maximum rows to send in this pass.")] = 50,
    retry_failed_rows: Annotated[
        bool,
        typer.Option(
            "--retry-failed",
            help="Requeue permanently-failed rows first (use after fixing TELEGRAM_CHAT_ID).",
        ),
    ] = False,
) -> None:
    """Flush pending outbox rows (also runs at the end of every pipeline run)."""
    from .db import session_scope
    from .notify.outbox import pending_count, retry_failed, send_pending

    cfg = get_config()
    with session_scope(cfg) as session:
        requeued = retry_failed(session, cfg) if retry_failed_rows else 0
        before = pending_count(session)
        summary = send_pending(session, cfg, limit=limit)
    typer.echo(
        json.dumps(
            {
                "requeued": requeued,
                "pending_before": before,
                "claimed": summary.claimed,
                "sent": summary.sent,
                "failed": summary.failed,
                "gave_up": summary.gave_up,
            },
            indent=2,
        )
    )


@app.command()
def dashboard(
    port: Annotated[int, typer.Option(min=1024, max=65535)] = 8501,
) -> None:
    """Open the local coin leaderboard and target tracker."""
    import subprocess

    path = Path(__file__).with_name("dashboard.py")
    raise typer.Exit(
        subprocess.call(
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                str(path),
                "--server.address",
                "127.0.0.1",
                "--server.port",
                str(port),
                "--server.headless",
                "true",
                "--browser.gatherUsageStats",
                "false",
            ]
        )
    )


@app.command("mentions-backfill")
def mentions_backfill() -> None:
    """Scan all saved transcripts for coins, without downloading videos again."""
    from .db import session_scope
    from .pipeline.mentions import mentions

    cfg = get_config()
    processed = failed = 0
    with exclusive_lock(cfg.settings.lock_path), session_scope(cfg) as session:
        while True:
            result = mentions(session, cfg)
            processed += result.processed
            failed += result.failed
            if not result.processed or result.failed:
                break
    typer.echo(json.dumps({"scanned": processed, "failed": failed}))
    if failed:
        raise typer.Exit(1)


@app.command()
def report(
    send: Annotated[
        bool, typer.Option(help="Send the summary and spreadsheet to Telegram.")
    ] = False,
    refresh_prices: Annotated[bool, typer.Option(help="Fetch current USD prices.")] = False,
    force: Annotated[
        bool, typer.Option(help="Send another report even if today's was sent.")
    ] = False,
    output: Annotated[Path | None, typer.Option(help="Output spreadsheet path.")] = None,
) -> None:
    """Export the full coin tracker; optionally refresh prices and send to Telegram."""
    from .db import session_scope
    from .notify.outbox import send_pending
    from .tracker import load_tracker, queue_daily_report, refresh_current_prices, workbook_bytes

    cfg = get_config()
    destination = output or cfg.settings.data_dir / "reports" / "coin-tracker.xlsx"
    with exclusive_lock(cfg.settings.lock_path), session_scope(cfg) as session:
        if refresh_prices:
            typer.echo(f"Updated {refresh_current_prices(session, cfg)} coin prices.")
        data = load_tracker(session, cfg)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(workbook_bytes(data, cfg.settings.display_tz))
        typer.echo(f"Spreadsheet: {destination.resolve()}")
        if send:
            queued = queue_daily_report(session, cfg, force=force)
            summary = send_pending(session, cfg)
            typer.echo(f"Queued {queued}; sent {summary.sent}; failed {summary.failed}.")
            if summary.failed:
                raise typer.Exit(1)


def entrypoint() -> None:
    try:
        app()
    except NotImplementedError as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
        sys.exit(2)


if __name__ == "__main__":
    entrypoint()
