"""Discover stage: find new videos and apply the admission rules.

Admission rules, in the order they are applied:

1. **Shorts are skipped** (`duration < min_duration_seconds`). They carry no
   analysis worth extracting and would dominate the video count.
2. **Running livestreams are held back.** Captions for a live stream are
   provisional, so a video is only admitted once `actualEndTime` is set.
3. **Finished streams wait `livestream_settle_hours`.** YouTube generates
   captions for a long stream well after it ends; transcribing immediately
   would burn the whole retry ladder on a video whose captions simply do not
   exist yet.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import Session, col, select

from ..config import ChannelConfig, Config
from ..enums import SkipReason, VideoState
from ..logging import get_logger
from ..models import ApiUsage, Channel, Video, utcnow
from ..sources.youtube_api import VideoInfo, YouTubeApi, YouTubeApiError
from ..sources.youtube_rss import YouTubeRss
from .runner import StageResult, defer, skip

log = get_logger("pipeline.discover")

KV_BACKFILL_DONE = "backfill_done"


def ensure_channel(session: Session, channel_cfg: ChannelConfig, api: YouTubeApi) -> Channel:
    """Resolve a configured handle to a `Channel` row, resolving IDs once.

    The uploads playlist ID is stable for the life of a channel, so this costs
    one quota unit per channel ever, not per run.
    """
    channel = session.exec(select(Channel).where(Channel.handle == channel_cfg.handle)).first()
    if channel is None:
        channel = Channel(handle=channel_cfg.handle, default_host=channel_cfg.default_host)
        session.add(channel)

    # Keep the label in sync with config without re-resolving the API ids.
    channel.default_host = channel_cfg.default_host
    channel.enabled = channel_cfg.enabled

    if not channel.channel_id or not channel.uploads_playlist_id:
        info = api.resolve_channel(channel_cfg.handle)
        channel.channel_id = info.channel_id
        channel.name = info.title
        channel.uploads_playlist_id = info.uploads_playlist_id
        channel.resolved_at = utcnow()
        log.info(
            "channel resolved",
            extra={"handle": channel_cfg.handle, "channel_id": info.channel_id},
        )

    session.add(channel)
    session.commit()
    session.refresh(channel)
    return channel


def _admit(
    session: Session, video: Video, info: VideoInfo, cfg: Config
) -> tuple[bool, SkipReason | None]:
    """Apply the admission rules to a newly seen video."""
    dc = cfg.app.discovery

    if info.is_live_now:
        # Not a skip: the stream will end, and then it becomes processable.
        defer(session, video, timedelta(minutes=30), "livestream still running")
        return False, None

    if info.duration_seconds is not None and info.duration_seconds < dc.min_duration_seconds:
        skip(session, video, SkipReason.TOO_SHORT)
        return False, SkipReason.TOO_SHORT

    if info.actual_end_time is not None:
        ready_at = info.actual_end_time + timedelta(hours=dc.livestream_settle_hours)
        if utcnow() < ready_at:
            defer(
                session,
                video,
                ready_at - utcnow(),
                f"waiting {dc.livestream_settle_hours}h after stream end for captions",
            )
            return False, None

    return True, None


def record_quota(session: Session, units: int, service: str = "youtube") -> None:
    """Accumulate today's API units, for the quota health check."""
    if units <= 0:
        return
    day = f"{utcnow():%Y-%m-%d}"
    row = session.exec(
        select(ApiUsage).where(ApiUsage.day == day).where(ApiUsage.service == service)
    ).first()
    if row is None:
        row = ApiUsage(day=day, service=service, units=0)
    row.units += units
    row.updated_at = utcnow()
    session.add(row)
    session.commit()


def discover(
    session: Session,
    cfg: Config,
    days: int | None = None,
    is_backfill: bool = False,
) -> StageResult:
    """Find new videos for every enabled channel.

    On the first ever run (or an explicit `backfill`) this looks back
    `days`; afterwards it only looks for videos newer than the newest one
    already stored for that channel.
    """
    result = StageResult(stage="discover")
    api = YouTubeApi(cfg)
    dc = cfg.app.discovery

    for channel_cfg in cfg.app.enabled_channels:
        try:
            channel = ensure_channel(session, channel_cfg, api)
        except YouTubeApiError as exc:
            result.failed += 1
            log.error(
                "channel resolution failed",
                extra={"handle": channel_cfg.handle, "error": str(exc)},
            )
            continue

        assert channel.id is not None
        since = _discovery_floor(session, channel.id, cfg, days)

        try:
            video_ids = api.list_uploads(
                channel.uploads_playlist_id or "",
                published_after=since,
                max_items=dc.max_videos_per_run if not is_backfill else 200,
            )
        except YouTubeApiError as exc:
            log.warning(
                "uploads listing failed, falling back to RSS",
                extra={"handle": channel_cfg.handle, "error": str(exc)},
            )
            video_ids = [
                e.video_id
                for e in YouTubeRss(cfg).recent(channel.channel_id or "")
                if e.published_at is None or since is None or e.published_at >= since
            ]
            if not video_ids:
                result.failed += 1
                continue

        known = set(
            session.exec(select(Video.video_id).where(col(Video.video_id).in_(video_ids))).all()
        )
        fresh = [v for v in video_ids if v not in known]
        if not fresh:
            continue

        try:
            infos = api.get_videos(fresh)
        except YouTubeApiError as exc:
            result.failed += 1
            log.error("video metadata failed", extra={"error": str(exc)})
            continue

        for info in infos:
            video = Video(
                video_id=info.video_id,
                channel_pk=channel.id,
                title=info.title,
                published_at=info.published_at,
                duration_seconds=info.duration_seconds,
                is_livestream=info.is_livestream,
                actual_end_time=info.actual_end_time,
                state=VideoState.DISCOVERED,
                is_backfill=is_backfill,
            )
            session.add(video)
            admitted, skip_reason = _admit(session, video, info, cfg)
            if admitted:
                result.processed += 1
            elif skip_reason is not None:
                result.skipped += 1
            session.commit()

        log.info(
            "channel discovered",
            extra={
                "handle": channel_cfg.handle,
                "new_videos": len(fresh),
                "admitted": result.processed,
            },
        )

    record_quota(session, api.quota_used)
    result.notes["quota_units"] = api.quota_used
    return result


def _discovery_floor(
    session: Session, channel_pk: int, cfg: Config, days: int | None
) -> datetime | None:
    """The timestamp to discover from for one channel."""
    from datetime import datetime  # noqa: F401  (used in the annotation)

    if days is not None:
        return utcnow() - timedelta(days=days)

    newest = session.exec(
        select(Video.published_at)
        .where(Video.channel_pk == channel_pk)
        .order_by(col(Video.published_at).desc())
        .limit(1)
    ).first()
    if newest is not None:
        return newest
    # First run for this channel: fall back to the configured backfill window.
    return utcnow() - timedelta(days=cfg.app.discovery.backfill_days)


__all__ = ["KV_BACKFILL_DONE", "discover", "ensure_channel", "record_quota"]
