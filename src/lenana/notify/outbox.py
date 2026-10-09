"""Transactional outbox: the no-lost-alerts, no-duplicate-alerts guarantee.

How it works:

1. **Enqueue** happens inside the same transaction as the state change that
   caused it. There is no window in which a video is marked alerted but no
   message exists, or vice versa.
2. **`dedupe_key` is UNIQUE**, so re-running a stage cannot produce a second
   copy of the same alert. Duplicate suppression is enforced by SQLite, not by
   application logic that could be bypassed by a new code path.
3. **Sending claims a row first** with a conditional UPDATE. Two senders
   racing cannot both send the same row, which is what makes running the
   sender twice send exactly once.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from ..config import Config
from ..enums import OutboxKind, OutboxState
from ..logging import get_logger
from ..models import Outbox, utcnow
from ..utils import backoff_delay
from .format import split_message
from .telegram import TelegramClient

log = get_logger("outbox")


@dataclass
class SendSummary:
    claimed: int = 0
    sent: int = 0
    failed: int = 0
    skipped_duplicate: int = 0
    gave_up: int = 0


def enqueue(
    session: Session,
    dedupe_key: str,
    kind: OutboxKind,
    body: str,
    cfg: Config,
    chat_id: str | None = None,
) -> list[Outbox]:
    """Queue a message, splitting it if it exceeds Telegram's limit.

    Must be called inside the caller's transaction — it deliberately does not
    commit, so the enqueue and the state change land together.

    An already-queued `dedupe_key` is a no-op, which is what makes the calling
    stage safe to re-run.
    """
    chat = chat_id or cfg.settings.telegram_chat_id
    parts = split_message(body, cfg.app.alerts.max_message_chars)
    rows: list[Outbox] = []

    for index, part in enumerate(parts):
        key = dedupe_key if len(parts) == 1 else f"{dedupe_key}:{index}"
        existing = session.exec(select(Outbox).where(Outbox.dedupe_key == key)).first()
        if existing is not None:
            log.debug("outbox row already queued", extra={"dedupe_key": key})
            continue
        row = Outbox(
            dedupe_key=key,
            kind=kind,
            chat_id=chat,
            body=part,
            part_index=index,
            part_total=len(parts),
        )
        session.add(row)
        rows.append(row)

    return rows


def enqueue_committed(
    session: Session,
    dedupe_key: str,
    kind: OutboxKind,
    body: str,
    cfg: Config,
    chat_id: str | None = None,
) -> list[Outbox]:
    """Enqueue and commit immediately, tolerating a concurrent duplicate.

    For standalone messages (health warnings, test messages) that are not tied
    to a state change.
    """
    try:
        rows = enqueue(session, dedupe_key, kind, body, cfg, chat_id)
        session.commit()
        return rows
    except IntegrityError:
        # Another process queued the same key between our check and our insert.
        session.rollback()
        log.info("outbox duplicate suppressed by constraint", extra={"dedupe_key": dedupe_key})
        return []


def send_pending(
    session: Session,
    cfg: Config,
    client: TelegramClient | None = None,
    limit: int = 50,
    sleep: bool = True,
) -> SendSummary:
    """Send due rows, oldest first, at most one per second.

    Each row is committed individually: a crash partway through leaves the
    already-sent rows marked sent, so a rerun does not resend them.
    """
    client = client or TelegramClient(cfg)
    ladder = cfg.app.alerts.outbox_retry_ladder_minutes
    max_attempts = cfg.app.alerts.outbox_max_attempts
    delay = 1.0 / max(cfg.app.alerts.send_rate_per_second, 1)
    summary = SendSummary()

    now = utcnow()
    due = session.exec(
        select(Outbox)
        .where(Outbox.state == OutboxState.PENDING)
        .where(Outbox.next_attempt_at <= now)
        .order_by(col(Outbox.created_at), col(Outbox.part_index))
        .limit(limit)
    ).all()

    for index, row in enumerate(due):
        # Claim the row before sending. The conditional UPDATE means a second
        # sender running concurrently finds nothing to claim and sends nothing.
        claimed = _claim(session, row)
        if not claimed:
            summary.skipped_duplicate += 1
            continue
        summary.claimed += 1

        if index > 0 and sleep:
            time.sleep(delay)

        result = (
            client.send_document(Path(row.document_path), row.body, chat_id=row.chat_id)
            if row.document_path
            else client.send_message(row.body, chat_id=row.chat_id)
        )

        if result.ok:
            row.state = OutboxState.SENT
            row.sent_at = utcnow()
            row.last_error = None
            session.add(row)
            session.commit()
            summary.sent += 1
            log.info(
                "alert sent",
                extra={"dedupe_key": row.dedupe_key, "kind": str(row.kind)},
            )
            continue

        summary.failed += 1
        row.last_error = result.error

        if result.retry_after is not None:
            # Honour Telegram's own backoff rather than our ladder.
            row.next_attempt_at = utcnow() + timedelta(seconds=result.retry_after + 1)
        elif not result.is_retryable:
            # A 400 means the body is malformed; retrying forever would block
            # every later row behind it.
            row.state = OutboxState.FAILED
            summary.gave_up += 1
            log.error(
                "alert permanently failed",
                extra={
                    "dedupe_key": row.dedupe_key,
                    "status": result.status_code,
                    "error": result.error,
                },
            )
        else:
            next_delay = backoff_delay(ladder, row.attempts)
            if next_delay is None or row.attempts >= max_attempts:
                row.state = OutboxState.FAILED
                summary.gave_up += 1
                log.error(
                    "alert gave up after retries",
                    extra={"dedupe_key": row.dedupe_key, "attempts": row.attempts},
                )
            else:
                row.next_attempt_at = utcnow() + next_delay

        session.add(row)
        session.commit()

    return summary


def _claim(session: Session, row: Outbox) -> bool:
    """Atomically mark a row as being attempted.

    Returns False when another process already claimed it.
    """
    from sqlalchemy import update

    result = session.exec(
        update(Outbox)
        .where(col(Outbox.id) == row.id)
        .where(col(Outbox.state) == OutboxState.PENDING)
        .values(attempts=col(Outbox.attempts) + 1)
    )
    session.commit()
    if result.rowcount == 0:
        return False
    session.refresh(row)
    return True


def retry_failed(session: Session, cfg: Config) -> int:
    """Move FAILED rows back to PENDING.

    A 403 from Telegram (wrong chat id, bot removed from the group) is
    correctly non-retryable — hammering it would be pointless — but it is a
    *configuration* error, so the alert should be recoverable once the config
    is fixed rather than lost for good.
    """
    rows = list(session.exec(select(Outbox).where(Outbox.state == OutboxState.FAILED)).all())
    for row in rows:
        row.state = OutboxState.PENDING
        row.attempts = 0
        row.last_error = None
        row.next_attempt_at = utcnow()
        session.add(row)
    if rows:
        session.commit()
        log.info("failed outbox rows requeued", extra={"count": len(rows)})
    return len(rows)


def pending_count(session: Session) -> int:
    from sqlalchemy import func

    return int(
        session.scalar(
            select(func.count()).select_from(Outbox).where(Outbox.state == OutboxState.PENDING)
        )
        or 0
    )


__all__ = [
    "SendSummary",
    "enqueue",
    "enqueue_committed",
    "pending_count",
    "retry_failed",
    "send_pending",
]
