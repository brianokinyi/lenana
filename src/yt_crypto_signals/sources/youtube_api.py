"""YouTube Data API v3: channel resolution and video metadata.

Quota shape (10,000 units/day by default): `channels.list`, `playlistItems.list`
and `videos.list` each cost 1 unit. Four channels polled every 30 minutes costs
roughly 400 units/day, so quota is not a practical constraint — but usage is
recorded anyway so the health stage can warn before a surprise.

`search.list` is deliberately **not** used anywhere: it costs 100 units per
call and returns less reliable ordering than walking the uploads playlist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ..config import Config
from ..logging import get_logger
from ..utils import parse_iso8601_duration, parse_rfc3339

log = get_logger("youtube")

#: Quota units per API method, for usage accounting.
QUOTA_COSTS = {"channels.list": 1, "playlistItems.list": 1, "videos.list": 1}


class YouTubeApiError(RuntimeError):
    """A YouTube Data API call failed."""

    def __init__(self, message: str, status: int | None = None, reason: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason

    @property
    def is_quota_exceeded(self) -> bool:
        return self.reason in ("quotaExceeded", "dailyLimitExceeded")


@dataclass
class ChannelInfo:
    channel_id: str
    title: str
    uploads_playlist_id: str


@dataclass
class VideoInfo:
    video_id: str
    channel_id: str
    title: str
    published_at: datetime
    duration_seconds: int | None
    is_livestream: bool
    actual_end_time: datetime | None
    scheduled_start_time: datetime | None = None

    @property
    def is_live_now(self) -> bool:
        """A livestream that has started but not ended.

        Captions for a running stream are provisional, so these are held back
        until the stream ends.
        """
        return self.is_livestream and self.actual_end_time is None


class YouTubeApi:
    """Thin wrapper over the discovery client, with quota accounting."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        # cache_discovery=False: the file cache is unwritable under a hardened
        # systemd unit with ProtectSystem=strict and logs a warning each run.
        self._service = build(
            "youtube",
            "v3",
            developerKey=cfg.settings.youtube_api_key,
            cache_discovery=False,
        )
        self.quota_used = 0

    def _spend(self, method: str) -> None:
        self.quota_used += QUOTA_COSTS.get(method, 1)

    @staticmethod
    def _wrap(exc: HttpError) -> YouTubeApiError:
        status = getattr(exc.resp, "status", None)
        reason = None
        try:
            errors = exc.error_details or []
            if errors and isinstance(errors, list):
                reason = errors[0].get("reason")
        except Exception:
            reason = None
        return YouTubeApiError(str(exc), status=status, reason=reason)

    def resolve_channel(self, handle: str) -> ChannelInfo:
        """Resolve an @handle to its channel ID and uploads playlist.

        Done once per channel and cached in the database; the uploads playlist
        ID is stable for the life of the channel.
        """
        try:
            self._spend("channels.list")
            response = (
                self._service.channels()
                .list(part="snippet,contentDetails", forHandle=handle)
                .execute()
            )
        except HttpError as exc:
            raise self._wrap(exc) from exc

        items = response.get("items") or []
        if not items:
            raise YouTubeApiError(
                f"no channel found for handle {handle!r}; check the spelling in config.yaml"
            )
        item = items[0]
        uploads = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if not uploads:
            raise YouTubeApiError(f"channel {handle!r} has no uploads playlist")
        return ChannelInfo(
            channel_id=item["id"],
            title=item.get("snippet", {}).get("title", handle),
            uploads_playlist_id=uploads,
        )

    def list_uploads(
        self, uploads_playlist_id: str, published_after: datetime | None = None, max_items: int = 50
    ) -> list[str]:
        """Video IDs from a channel's uploads playlist, newest first.

        Paginates only while items are newer than `published_after`: the
        playlist is in reverse-chronological order, so the first older item
        means every later page is older too.
        """
        video_ids: list[str] = []
        page_token: str | None = None

        while len(video_ids) < max_items:
            try:
                self._spend("playlistItems.list")
                request = self._service.playlistItems().list(
                    part="contentDetails",
                    playlistId=uploads_playlist_id,
                    maxResults=min(50, max_items - len(video_ids)),
                    pageToken=page_token,
                )
                response = request.execute()
            except HttpError as exc:
                raise self._wrap(exc) from exc

            reached_older = False
            for item in response.get("items") or []:
                details = item.get("contentDetails", {})
                vid = details.get("videoId")
                if not vid:
                    continue
                published = parse_rfc3339(details.get("videoPublishedAt", ""))
                if published_after and published and published < published_after:
                    reached_older = True
                    continue
                video_ids.append(vid)

            page_token = response.get("nextPageToken")
            if not page_token or reached_older:
                break

        return video_ids

    def get_videos(self, video_ids: list[str]) -> list[VideoInfo]:
        """Metadata for up to 50 videos per call.

        `liveStreamingDetails` is requested because it is the only way to tell
        a finished stream from a running one, and `contentDetails.duration` is
        how Shorts are identified.
        """
        results: list[VideoInfo] = []
        for start in range(0, len(video_ids), 50):
            batch = video_ids[start : start + 50]
            try:
                self._spend("videos.list")
                response = (
                    self._service.videos()
                    .list(
                        part="snippet,contentDetails,liveStreamingDetails",
                        id=",".join(batch),
                    )
                    .execute()
                )
            except HttpError as exc:
                raise self._wrap(exc) from exc

            for item in response.get("items") or []:
                results.append(_video_info_from_item(item))
        return results

    def get_video(self, video_id: str) -> VideoInfo | None:
        videos = self.get_videos([video_id])
        return videos[0] if videos else None


def _video_info_from_item(item: dict[str, Any]) -> VideoInfo:
    snippet = item.get("snippet", {})
    content = item.get("contentDetails", {})
    live = item.get("liveStreamingDetails", {})
    return VideoInfo(
        video_id=item["id"],
        channel_id=snippet.get("channelId", ""),
        title=snippet.get("title", ""),
        published_at=parse_rfc3339(snippet.get("publishedAt", "")) or datetime.min,
        duration_seconds=parse_iso8601_duration(content.get("duration", "")),
        is_livestream=bool(live),
        actual_end_time=parse_rfc3339(live.get("actualEndTime", "")),
        scheduled_start_time=parse_rfc3339(live.get("scheduledStartTime", "")),
    )


__all__ = [
    "QUOTA_COSTS",
    "ChannelInfo",
    "VideoInfo",
    "YouTubeApi",
    "YouTubeApiError",
]
