"""Outbox idempotency and send semantics."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from lenana.config import Config
from lenana.enums import OutboxKind, OutboxState
from lenana.models import Outbox, utcnow
from lenana.notify.outbox import (
    enqueue,
    enqueue_committed,
    pending_count,
    retry_failed,
    send_pending,
)
from lenana.notify.telegram import SendResult


class FakeTelegram:
    """Records every send so double-sends are detectable."""

    def __init__(self, results: list[SendResult] | None = None) -> None:
        self.sent: list[str] = []
        self.results = results or []
        self.default_chat_id = "-100"

    def send_message(self, text: str, chat_id: str | None = None) -> SendResult:
        self.sent.append(text)
        if self.results:
            return self.results.pop(0)
        return SendResult(ok=True, status_code=200, message_id=len(self.sent))


def test_enqueue_then_send(session: Session, config: Config) -> None:
    enqueue_committed(session, "k1", OutboxKind.VIDEO_ALERT, "hello", config)
    client = FakeTelegram()
    summary = send_pending(session, config, client=client, sleep=False)  # type: ignore[arg-type]
    assert summary.sent == 1
    assert client.sent == ["hello"]


def test_running_the_sender_twice_sends_once(session: Session, config: Config) -> None:
    """The core idempotency guarantee."""
    enqueue_committed(session, "k1", OutboxKind.VIDEO_ALERT, "only once", config)
    client = FakeTelegram()
    first = send_pending(session, config, client=client, sleep=False)  # type: ignore[arg-type]
    second = send_pending(session, config, client=client, sleep=False)  # type: ignore[arg-type]
    assert first.sent == 1
    assert second.sent == 0
    assert client.sent == ["only once"], "the message must be on the wire exactly once"


def test_enqueueing_the_same_key_twice_is_a_noop(session: Session, config: Config) -> None:
    enqueue_committed(session, "dup", OutboxKind.VIDEO_ALERT, "first", config)
    enqueue_committed(session, "dup", OutboxKind.VIDEO_ALERT, "second", config)
    rows = session.exec(select(Outbox).where(Outbox.dedupe_key == "dup")).all()
    assert len(rows) == 1
    assert rows[0].body == "first"


def test_dedupe_key_uniqueness_is_enforced_by_the_database(
    session: Session, config: Config
) -> None:
    """Duplicate suppression must not depend on application logic alone."""
    session.add(Outbox(dedupe_key="x", kind=OutboxKind.HEALTH, chat_id="-1", body="a"))
    session.commit()
    session.add(Outbox(dedupe_key="x", kind=OutboxKind.HEALTH, chat_id="-1", body="b"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_long_body_is_split_into_parts_with_distinct_keys(session: Session, config: Config) -> None:
    body = "\n".join("y" * 200 for _ in range(60))
    rows = enqueue(session, "big", OutboxKind.VIDEO_ALERT, body, config)
    session.commit()
    assert len(rows) > 1
    keys = {r.dedupe_key for r in rows}
    assert len(keys) == len(rows)
    assert all(r.part_total == len(rows) for r in rows)


def test_split_parts_are_all_sent_in_order(session: Session, config: Config) -> None:
    body = "\n".join(f"line{i} " + "z" * 200 for i in range(60))
    enqueue(session, "big", OutboxKind.VIDEO_ALERT, body, config)
    session.commit()
    client = FakeTelegram()
    summary = send_pending(session, config, client=client, sleep=False)  # type: ignore[arg-type]
    assert summary.sent == len(client.sent) > 1
    assert "\n".join(client.sent) == body


def test_retryable_failure_schedules_a_later_attempt(session: Session, config: Config) -> None:
    enqueue_committed(session, "k", OutboxKind.VIDEO_ALERT, "body", config)
    client = FakeTelegram([SendResult(ok=False, status_code=503, error="upstream")])
    summary = send_pending(session, config, client=client, sleep=False)  # type: ignore[arg-type]
    assert summary.sent == 0 and summary.failed == 1 and summary.gave_up == 0

    row = session.exec(select(Outbox).where(Outbox.dedupe_key == "k")).one()
    assert row.state is OutboxState.PENDING
    assert row.next_attempt_at > utcnow()


def test_non_retryable_failure_gives_up_immediately(session: Session, config: Config) -> None:
    """A 400 means the body is malformed; retrying forever would block every
    later row behind it."""
    enqueue_committed(session, "k", OutboxKind.VIDEO_ALERT, "body", config)
    client = FakeTelegram([SendResult(ok=False, status_code=400, error="bad html")])
    summary = send_pending(session, config, client=client, sleep=False)  # type: ignore[arg-type]
    assert summary.gave_up == 1
    row = session.exec(select(Outbox).where(Outbox.dedupe_key == "k")).one()
    assert row.state is OutboxState.FAILED


def test_rate_limit_honours_retry_after(session: Session, config: Config) -> None:
    enqueue_committed(session, "k", OutboxKind.VIDEO_ALERT, "body", config)
    client = FakeTelegram([SendResult(ok=False, status_code=429, retry_after=30)])
    send_pending(session, config, client=client, sleep=False)  # type: ignore[arg-type]
    row = session.exec(select(Outbox).where(Outbox.dedupe_key == "k")).one()
    assert row.state is OutboxState.PENDING
    # Telegram's own backoff wins over our ladder.
    assert row.next_attempt_at >= utcnow() + timedelta(seconds=29)


def test_future_rows_are_not_sent_yet(session: Session, config: Config) -> None:
    row = Outbox(
        dedupe_key="later",
        kind=OutboxKind.VIDEO_ALERT,
        chat_id="-100",
        body="not yet",
        next_attempt_at=utcnow() + timedelta(hours=1),
    )
    session.add(row)
    session.commit()
    client = FakeTelegram()
    summary = send_pending(session, config, client=client, sleep=False)  # type: ignore[arg-type]
    assert summary.sent == 0
    assert client.sent == []


def test_retry_failed_requeues_for_recovery(session: Session, config: Config) -> None:
    """A wrong chat id is a config error; the alert should be recoverable once
    it is fixed, not lost."""
    enqueue_committed(session, "k", OutboxKind.VIDEO_ALERT, "body", config)
    failing = FakeTelegram([SendResult(ok=False, status_code=403, error="forbidden")])
    send_pending(session, config, client=failing, sleep=False)  # type: ignore[arg-type]
    assert pending_count(session) == 0

    assert retry_failed(session, config) == 1
    assert pending_count(session) == 1

    working = FakeTelegram()
    summary = send_pending(session, config, client=working, sleep=False)  # type: ignore[arg-type]
    assert summary.sent == 1
    assert working.sent == ["body"]


def test_exhausting_the_ladder_gives_up(session: Session, config: Config) -> None:
    enqueue_committed(session, "k", OutboxKind.VIDEO_ALERT, "body", config)
    rungs = len(config.app.alerts.outbox_retry_ladder_minutes)

    for _ in range(rungs + 2):
        row = session.exec(select(Outbox).where(Outbox.dedupe_key == "k")).one()
        if row.state is not OutboxState.PENDING:
            break
        row.next_attempt_at = utcnow() - timedelta(seconds=1)
        session.add(row)
        session.commit()
        client = FakeTelegram([SendResult(ok=False, status_code=500, error="boom")])
        send_pending(session, config, client=client, sleep=False)  # type: ignore[arg-type]

    row = session.exec(select(Outbox).where(Outbox.dedupe_key == "k")).one()
    assert row.state is OutboxState.FAILED
    assert row.attempts >= rungs
