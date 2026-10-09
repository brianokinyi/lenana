"""Discovery admission rules: Shorts, livestreams and the settle delay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from yt_crypto_signals.config import ChannelConfig, Config
from yt_crypto_signals.enums import SkipReason, VideoState
from yt_crypto_signals.models import ApiUsage, Channel, Video, utcnow
from yt_crypto_signals.pipeline.discover import _admit, ensure_channel, record_quota
from yt_crypto_signals.sources.youtube_api import ChannelInfo, VideoInfo


class FakeApi:
    """Stands in for the YouTube Data API; records what was asked for."""

    def __init__(self) -> None:
        self.resolved: list[str] = []
        self.quota_used = 0

    def resolve_channel(self, handle: str) -> ChannelInfo:
        self.resolved.append(handle)
        self.quota_used += 1
        return ChannelInfo(
            channel_id=f"UC{handle.lstrip('@')}",
            title=handle.lstrip("@"),
            uploads_playlist_id=f"UU{handle.lstrip('@')}",
        )


def video_info(**overrides: object) -> VideoInfo:
    base: dict[str, object] = {
        "video_id": "vid00000001",
        "channel_id": "UCtest",
        "title": "Test",
        "published_at": utcnow(),
        "duration_seconds": 1800,
        "is_livestream": False,
        "actual_end_time": None,
    }
    base.update(overrides)
    return VideoInfo(**base)  # type: ignore[arg-type]


def make_row(session: Session) -> Video:
    channel = Channel(handle="@T")
    session.add(channel)
    session.commit()
    assert channel.id is not None
    video = Video(
        video_id="vid00000001",
        channel_pk=channel.id,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(video)
    session.commit()
    return video


# --------------------------------------------------------------------------
# channel resolution
# --------------------------------------------------------------------------


def test_channel_is_resolved_once_then_cached(session: Session, config: Config) -> None:
    api = FakeApi()
    cfg_channel = ChannelConfig(handle="@Test", default_host="Someone")

    first = ensure_channel(session, cfg_channel, api)  # type: ignore[arg-type]
    second = ensure_channel(session, cfg_channel, api)  # type: ignore[arg-type]

    assert first.id == second.id
    assert api.resolved == ["@Test"], "the uploads playlist id is stable; resolve once"
    assert second.uploads_playlist_id == "UUTest"


def test_host_label_syncs_from_config(session: Session, config: Config) -> None:
    api = FakeApi()
    ensure_channel(session, ChannelConfig(handle="@Test", default_host="Old"), api)  # type: ignore[arg-type]
    updated = ensure_channel(
        session,
        ChannelConfig(handle="@Test", default_host="New"),
        api,  # type: ignore[arg-type]
    )
    assert updated.default_host == "New"
    assert api.resolved == ["@Test"], "a label change must not re-resolve the API ids"


# --------------------------------------------------------------------------
# admission rules
# --------------------------------------------------------------------------


def test_normal_video_is_admitted(session: Session, config: Config) -> None:
    video = make_row(session)
    admitted, reason = _admit(session, video, video_info(), config)
    assert admitted is True and reason is None


def test_short_is_skipped(session: Session, config: Config) -> None:
    config.app.discovery.min_duration_seconds = 180
    video = make_row(session)
    admitted, reason = _admit(session, video, video_info(duration_seconds=58), config)
    assert admitted is False
    assert reason is SkipReason.TOO_SHORT
    assert video.state is VideoState.SKIPPED


def test_video_exactly_at_the_minimum_is_admitted(session: Session, config: Config) -> None:
    config.app.discovery.min_duration_seconds = 180
    video = make_row(session)
    admitted, _ = _admit(session, video, video_info(duration_seconds=180), config)
    assert admitted is True


def test_running_livestream_is_deferred_not_skipped(session: Session, config: Config) -> None:
    """It will end, and then it becomes processable — so this must not be terminal."""
    video = make_row(session)
    admitted, reason = _admit(
        session,
        video,
        video_info(is_livestream=True, actual_end_time=None, duration_seconds=0),
        config,
    )
    assert admitted is False
    assert reason is None, "deferred, not skipped"
    assert video.state is VideoState.DISCOVERED
    assert video.next_retry_at is not None and video.next_retry_at > utcnow()


def test_recently_ended_stream_waits_for_captions(session: Session, config: Config) -> None:
    config.app.discovery.livestream_settle_hours = 2
    video = make_row(session)
    ended = utcnow() - timedelta(minutes=30)
    admitted, reason = _admit(
        session, video, video_info(is_livestream=True, actual_end_time=ended), config
    )
    assert admitted is False and reason is None
    assert video.next_retry_at is not None
    # Roughly 90 minutes left of the 2-hour settle window.
    remaining = (video.next_retry_at - utcnow()).total_seconds() / 60
    assert 80 <= remaining <= 95


def test_stream_past_the_settle_window_is_admitted(session: Session, config: Config) -> None:
    config.app.discovery.livestream_settle_hours = 2
    video = make_row(session)
    ended = utcnow() - timedelta(hours=3)
    admitted, _ = _admit(
        session, video, video_info(is_livestream=True, actual_end_time=ended), config
    )
    assert admitted is True


def test_short_livestream_is_skipped_before_the_settle_check(
    session: Session, config: Config
) -> None:
    """Duration is checked first, so a 2-minute stream is skipped rather than
    waiting two hours to then be skipped."""
    video = make_row(session)
    admitted, reason = _admit(
        session,
        video,
        video_info(
            duration_seconds=120, is_livestream=True, actual_end_time=utcnow() - timedelta(hours=5)
        ),
        config,
    )
    assert reason is SkipReason.TOO_SHORT
    assert admitted is False


def test_unknown_duration_is_admitted(session: Session, config: Config) -> None:
    """A missing duration must not be read as zero and skipped as a Short."""
    video = make_row(session)
    admitted, reason = _admit(session, video, video_info(duration_seconds=None), config)
    assert admitted is True and reason is None


# --------------------------------------------------------------------------
# quota accounting
# --------------------------------------------------------------------------


def test_quota_accumulates_per_day(session: Session) -> None:
    record_quota(session, 12)
    record_quota(session, 8)
    row = session.exec(select(ApiUsage)).one()
    assert row.units == 20
    assert row.day == f"{utcnow():%Y-%m-%d}"


def test_zero_quota_is_not_recorded(session: Session) -> None:
    record_quota(session, 0)
    assert session.exec(select(ApiUsage)).all() == []
