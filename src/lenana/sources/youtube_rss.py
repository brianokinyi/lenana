"""Channel RSS feed: fallback discovery when the Data API is unavailable.

The feed costs no quota and needs no key, so it keeps discovery alive through
an API outage or an exhausted quota. It carries only the last ~15 uploads and
no duration or livestream details, so it is strictly a fallback: videos found
this way still need a `videos.list` enrichment before they can be filtered for
Shorts or unfinished streams.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx
from defusedxml import ElementTree

from ..config import Config
from ..httpc import build_client, retry_http
from ..logging import get_logger
from ..utils import parse_rfc3339

log = get_logger("youtube.rss")

FEED_URL = "https://www.youtube.com/feeds/videos.xml"
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


@dataclass
class RssEntry:
    video_id: str
    title: str
    published_at: datetime | None


class YouTubeRss:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    @retry_http()
    def _get(self, channel_id: str) -> str:
        with build_client(self.cfg, use_proxy=False) as client:
            response = client.get(FEED_URL, params={"channel_id": channel_id})
            response.raise_for_status()
            return response.text

    def recent(self, channel_id: str) -> list[RssEntry]:
        """Recent uploads for a channel, newest first. Never raises."""
        try:
            payload = self._get(channel_id)
        except httpx.HTTPError as exc:
            log.warning("rss fetch failed", extra={"channel_id": channel_id, "error": str(exc)})
            return []

        try:
            # defusedxml: the feed is third-party XML, so entity expansion and
            # external DTD attacks are a real consideration.
            root = ElementTree.fromstring(payload)
        except Exception as exc:
            log.warning("rss parse failed", extra={"channel_id": channel_id, "error": str(exc)})
            return []

        entries: list[RssEntry] = []
        for entry in root.findall("atom:entry", _NS):
            vid = entry.findtext("yt:videoId", namespaces=_NS)
            if not vid:
                continue
            entries.append(
                RssEntry(
                    video_id=vid,
                    title=entry.findtext("atom:title", default="", namespaces=_NS) or "",
                    published_at=parse_rfc3339(
                        entry.findtext("atom:published", default="", namespaces=_NS) or ""
                    ),
                )
            )
        return entries


__all__ = ["RssEntry", "YouTubeRss"]
