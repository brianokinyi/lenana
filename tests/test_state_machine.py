"""State machine transitions and the retry ladder."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

from sqlmodel import Session

from yt_crypto_signals.config import Config
from yt_crypto_signals.enums import FailureKind, SkipReason, VideoState
from yt_crypto_signals.models import Channel, Video, utcnow
from yt_crypto_signals.pipeline.runner import (
    advance,
    claim_videos,
    defer,
    record_failure,
    requeue_failed,
    skip,
)

_VIDEO_COUNTER = itertools.count(1)


def _next_video_id() -> str:
    """Unique 11-character ids; a timestamp-derived id collides in a tight loop."""
    return f"vid{next(_VIDEO_COUNTER):08d}"


def make_video(session: Session, **overrides: object) -> Video:
    channel = session.exec(__import__("sqlmodel").select(Channel)).first()
    if channel is None:
        channel = Channel(handle="@Test")
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
    session.refresh(video)
    return video


def test_new_video_is_discovered(session: Session) -> None:
    assert make_video(session).state is VideoState.DISCOVERED


def test_advance_clears_error_bookkeeping(session: Session) -> None:
    video = make_video(
        session, last_error="boom", last_error_kind="blocked", next_retry_at=utcnow()
    )
    advance(session, video, VideoState.TRANSCRIBED)
    session.commit()
    assert video.state is VideoState.TRANSCRIBED
    assert video.last_error is None
    assert video.last_error_kind is None
    assert video.next_retry_at is None


def test_stage_only_claims_its_own_input_state(session: Session, config: Config) -> None:
    """The property that makes stages order-independent."""
    make_video(session, video_id="aaaaaaaaaaa", state=VideoState.DISCOVERED)
    make_video(session, video_id="bbbbbbbbbbb", state=VideoState.TRANSCRIBED)
    make_video(session, video_id="ccccccccccc", state=VideoState.DONE)

    claimed = claim_videos(session, VideoState.DISCOVERED, 10)
    assert [v.video_id for v in claimed] == ["aaaaaaaaaaa"]


def test_claim_respects_next_retry_at(session: Session) -> None:
    make_video(
        session,
        video_id="futureeeeee",
        state=VideoState.DISCOVERED,
        next_retry_at=utcnow() + timedelta(hours=2),
    )
    make_video(session, video_id="duenowwwwww", state=VideoState.DISCOVERED)

    due = claim_videos(session, VideoState.DISCOVERED, 10)
    assert [v.video_id for v in due] == ["duenowwwwww"]

    everything = claim_videos(session, VideoState.DISCOVERED, 10, respect_retry=False)
    assert len(everything) == 2


def test_claim_is_oldest_first(session: Session) -> None:
    make_video(session, video_id="newerrrrrrr", published_at=datetime(2026, 5, 1, tzinfo=UTC))
    make_video(session, video_id="olderrrrrrr", published_at=datetime(2026, 1, 1, tzinfo=UTC))
    assert [v.video_id for v in claim_videos(session, VideoState.DISCOVERED, 10)] == [
        "olderrrrrrr",
        "newerrrrrrr",
    ]


def test_claim_honours_the_batch_limit(session: Session) -> None:
    for i in range(5):
        make_video(session, video_id=f"vid{i:08d}")
    assert len(claim_videos(session, VideoState.DISCOVERED, 2)) == 2


# --------------------------------------------------------------------------
# retry ladder
# --------------------------------------------------------------------------


def test_blocked_failure_walks_the_ladder(session: Session, config: Config) -> None:
    config.app.reliability.transcript_retry_ladder_minutes = [15, 60, 240]
    video = make_video(session)

    for expected_minutes in (15, 60, 240):
        before = utcnow()
        retried = record_failure(session, video, FailureKind.BLOCKED, "blocked", config)
        session.commit()
        assert retried is True
        assert video.state is VideoState.TRANSCRIPT_FAILED
        assert video.next_retry_at is not None
        delay = (video.next_retry_at - before).total_seconds() / 60
        assert expected_minutes - 1 <= delay <= expected_minutes + 1

    # One rung past the end: condemned, and the health stage reports it.
    retried = record_failure(session, video, FailureKind.BLOCKED, "blocked", config)
    session.commit()
    assert retried is False
    assert video.state is VideoState.PERMANENT_FAILED
    assert video.attempts == 4


def test_no_captions_skips_without_retrying(session: Session, config: Config) -> None:
    """Retrying a video with no captions only burns proxy bandwidth."""
    video = make_video(session)
    retried = record_failure(session, video, FailureKind.NO_CAPTIONS, "none", config)
    session.commit()
    assert retried is False
    assert video.state is VideoState.SKIPPED
    assert video.skip_reason == SkipReason.NO_CAPTIONS.value


def test_unavailable_skips_without_retrying(session: Session, config: Config) -> None:
    video = make_video(session)
    assert record_failure(session, video, FailureKind.UNAVAILABLE, "private", config) is False
    assert video.state is VideoState.SKIPPED
    assert video.skip_reason == SkipReason.UNAVAILABLE.value


def test_transient_failure_is_retried(session: Session, config: Config) -> None:
    video = make_video(session)
    assert record_failure(session, video, FailureKind.TRANSIENT, "timeout", config) is True
    assert video.state is VideoState.TRANSCRIPT_FAILED


def test_requeue_only_returns_videos_past_their_backoff(session: Session, config: Config) -> None:
    ready = make_video(
        session,
        video_id="readyyyyyyy",
        state=VideoState.TRANSCRIPT_FAILED,
        next_retry_at=utcnow() - timedelta(minutes=1),
    )
    waiting = make_video(
        session,
        video_id="waitingggggg"[:11],
        state=VideoState.TRANSCRIPT_FAILED,
        next_retry_at=utcnow() + timedelta(hours=1),
    )

    requeued = requeue_failed(session, config, limit=10)
    assert [v.video_id for v in requeued] == [ready.video_id]
    session.refresh(ready)
    session.refresh(waiting)
    assert ready.state is VideoState.DISCOVERED
    assert waiting.state is VideoState.TRANSCRIPT_FAILED


def test_defer_keeps_state_but_delays(session: Session) -> None:
    """Livestream settle delay needs no state of its own."""
    video = make_video(session)
    defer(session, video, timedelta(hours=2), "waiting for captions")
    session.commit()
    assert video.state is VideoState.DISCOVERED
    assert video.next_retry_at is not None and video.next_retry_at > utcnow()
    assert claim_videos(session, VideoState.DISCOVERED, 10) == []


def test_skip_is_terminal(session: Session) -> None:
    video = make_video(session)
    skip(session, video, SkipReason.TOO_SHORT)
    session.commit()
    assert video.state is VideoState.SKIPPED
    assert video.next_retry_at is None
