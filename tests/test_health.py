"""Health checks and alert deduplication."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from yt_crypto_signals.config import Config
from yt_crypto_signals.enums import FailureKind, OutboxKind, OutboxState, VideoState
from yt_crypto_signals.models import ApiUsage, Channel, Outbox, StageRun, Video, utcnow
from yt_crypto_signals.pipeline.health import check_health, health

_VIDEO_COUNTER = itertools.count(1)


def _next_video_id() -> str:
    """Unique 11-character ids; a timestamp-derived id collides in a tight loop."""
    return f"vid{next(_VIDEO_COUNTER):08d}"


def make_video(session: Session, **overrides: object) -> Video:
    import sqlmodel

    channel = session.exec(sqlmodel.select(Channel)).first()
    if channel is None:
        channel = Channel(handle="@T")
        session.add(channel)
        session.commit()
    assert channel.id is not None
    base: dict[str, object] = {
        "video_id": _next_video_id(),
        "channel_pk": channel.id,
        "published_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    base.update(overrides)
    video = Video(**base)  # type: ignore[arg-type]
    session.add(video)
    session.commit()
    return video


def test_clean_system_reports_no_problems(session: Session, config: Config) -> None:
    assert check_health(session, config).ok


def test_long_standing_transcript_failures_are_reported(session: Session, config: Config) -> None:
    config.app.health.transcript_failure_alert_hours = 6
    make_video(
        session,
        state=VideoState.TRANSCRIPT_FAILED,
        updated_at=utcnow() - timedelta(hours=8),
    )
    report = check_health(session, config)
    assert not report.ok
    assert any("transcript failures" in p for p in report.problems)


def test_recent_transcript_failure_is_not_yet_a_problem(session: Session, config: Config) -> None:
    config.app.health.transcript_failure_alert_hours = 6
    make_video(
        session, state=VideoState.TRANSCRIPT_FAILED, updated_at=utcnow() - timedelta(minutes=10)
    )
    assert check_health(session, config).ok


def test_permanent_failures_are_reported(session: Session, config: Config) -> None:
    make_video(session, state=VideoState.PERMANENT_FAILED)
    report = check_health(session, config)
    assert any("retry ladder" in p for p in report.problems)


def test_repeated_blocks_suggest_a_dead_proxy(session: Session, config: Config) -> None:
    for _ in range(3):
        make_video(session, last_error_kind=FailureKind.BLOCKED.value, updated_at=utcnow())
    report = check_health(session, config)
    assert any("proxy may be" in p for p in report.problems)


def test_one_block_is_not_a_dead_proxy(session: Session, config: Config) -> None:
    make_video(session, last_error_kind=FailureKind.BLOCKED.value, updated_at=utcnow())
    assert check_health(session, config).ok


def test_repeatedly_failing_stage_is_reported(session: Session, config: Config) -> None:
    for _ in range(3):
        session.add(StageRun(stage="extract", started_at=utcnow() - timedelta(minutes=5), ok=False))
    session.commit()
    report = check_health(session, config)
    assert any("'extract' failed" in p for p in report.problems)


def test_occasional_stage_failure_is_tolerated(session: Session, config: Config) -> None:
    session.add(StageRun(stage="extract", started_at=utcnow(), ok=False))
    session.commit()
    assert check_health(session, config).ok


def test_quota_warning_at_threshold(session: Session, config: Config) -> None:
    config.app.health.youtube_quota_daily_units = 10000
    config.app.health.youtube_quota_warn_pct = 80
    session.add(ApiUsage(day=f"{utcnow():%Y-%m-%d}", service="youtube", units=8500))
    session.commit()
    report = check_health(session, config)
    assert any("quota at 85%" in p for p in report.problems)


def test_normal_quota_is_fine(session: Session, config: Config) -> None:
    session.add(ApiUsage(day=f"{utcnow():%Y-%m-%d}", service="youtube", units=400))
    session.commit()
    assert check_health(session, config).ok


def test_stale_outbox_is_reported(session: Session, config: Config) -> None:
    session.add(
        Outbox(
            dedupe_key="old",
            kind=OutboxKind.VIDEO_ALERT,
            chat_id="-1",
            body="b",
            created_at=utcnow() - timedelta(hours=3),
        )
    )
    session.commit()
    report = check_health(session, config)
    assert any("pending for over an hour" in p for p in report.problems)


def test_failed_outbox_is_reported(session: Session, config: Config) -> None:
    session.add(
        Outbox(
            dedupe_key="dead",
            kind=OutboxKind.VIDEO_ALERT,
            chat_id="-1",
            body="b",
            state=OutboxState.FAILED,
        )
    )
    session.commit()
    report = check_health(session, config)
    assert any("permanently failed" in p for p in report.problems)


def test_health_queues_one_alert_per_problem_set(session: Session, config: Config) -> None:
    """A persistent problem must not produce an alert every 30 minutes."""
    make_video(session, state=VideoState.PERMANENT_FAILED)

    health(session, config)
    health(session, config)

    rows = session.exec(select(Outbox).where(Outbox.kind == OutboxKind.HEALTH)).all()
    assert len(rows) == 1


def test_a_new_problem_gets_through_the_same_day(session: Session, config: Config) -> None:
    make_video(session, state=VideoState.PERMANENT_FAILED)
    health(session, config)

    session.add(ApiUsage(day=f"{utcnow():%Y-%m-%d}", service="youtube", units=9900))
    session.commit()
    health(session, config)

    rows = session.exec(select(Outbox).where(Outbox.kind == OutboxKind.HEALTH)).all()
    assert len(rows) == 2, "a newly appearing problem must still alert"


def test_no_alert_when_healthy(session: Session, config: Config) -> None:
    health(session, config)
    assert session.exec(select(Outbox)).all() == []
