"""Provider 1: youtube-transcript-api (version 1.2.4).

Preferred because it hits YouTube's caption endpoint directly — no video
metadata round-trip, no subtitle file, lowest bandwidth through the proxy.

Two version-specific facts shape this code:

* Cookie authentication is **disabled** in 1.2.4 (the loading code is
  commented out upstream, "not working properly with YouTube's most recent
  changes"). So the "never send cookies alongside a PO token" rule is
  automatically satisfied here; it only needs enforcing in the yt-dlp path.
* `WebshareProxyConfig` runs its own retry loop. We pin it low from config so
  it does not multiply with the transcribe stage's retry ladder.
"""

from __future__ import annotations

from typing import Any

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    AgeRestricted,
    CouldNotRetrieveTranscript,
    InvalidVideoId,
    IpBlocked,
    NoTranscriptFound,
    PoTokenRequired,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeDataUnparsable,
    YouTubeRequestFailed,
)
from youtube_transcript_api.proxies import (
    GenericProxyConfig,
    ProxyConfig,
    WebshareProxyConfig,
)

from ...config import Config, ProxyProvider
from ...enums import FailureKind, TranscriptProvider
from ...logging import get_logger
from .base import (
    FetchedTranscriptData,
    TranscriptFailure,
    TranscriptSegment,
    TranscriptSource,
)

log = get_logger("transcripts.ytapi")


def build_proxy_config(cfg: Config) -> ProxyConfig | None:
    """Translate our proxy settings into the library's config object.

    Returns None for `PROXY_PROVIDER=none`, which is the right setting on a
    residential IP; YouTube generally only blocks datacenter ranges.
    """
    s = cfg.settings
    if s.proxy_provider is ProxyProvider.WEBSHARE:
        return WebshareProxyConfig(
            proxy_username=s.webshare_proxy_username,
            proxy_password=s.webshare_proxy_password,
            filter_ip_locations=cfg.app.transcripts.webshare_ip_locations or None,
            retries_when_blocked=cfg.app.transcripts.webshare_retries_when_blocked,
        )
    if s.proxy_provider is ProxyProvider.GENERIC:
        return GenericProxyConfig(http_url=s.proxy_url, https_url=s.proxy_url)
    return None


#: Exception type -> classification. Checked most-specific-first because
#: `IpBlocked` subclasses `RequestBlocked`.
_FAILURE_MAP: tuple[tuple[type[BaseException], FailureKind], ...] = (
    (IpBlocked, FailureKind.BLOCKED),
    (RequestBlocked, FailureKind.BLOCKED),
    # A PO token is required: this IP/session is untrusted. yt-dlp with the
    # bgutil provider can still succeed, so treat it as blocked (retryable)
    # rather than terminal.
    (PoTokenRequired, FailureKind.BLOCKED),
    (TranscriptsDisabled, FailureKind.NO_CAPTIONS),
    (NoTranscriptFound, FailureKind.NO_CAPTIONS),
    (AgeRestricted, FailureKind.UNAVAILABLE),
    (InvalidVideoId, FailureKind.UNAVAILABLE),
    (VideoUnplayable, FailureKind.UNAVAILABLE),
    (VideoUnavailable, FailureKind.UNAVAILABLE),
    (YouTubeRequestFailed, FailureKind.TRANSIENT),
    (YouTubeDataUnparsable, FailureKind.TRANSIENT),
)


def classify_exception(exc: BaseException) -> FailureKind:
    """Map a library exception to a failure kind."""
    for exc_type, kind in _FAILURE_MAP:
        if isinstance(exc, exc_type):
            return kind
    return FailureKind.TRANSIENT


class YouTubeTranscriptApiSource(TranscriptSource):
    name = TranscriptProvider.YOUTUBE_TRANSCRIPT_API

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg)
        self._api: YouTubeTranscriptApi | None = None

    def is_available(self) -> bool:
        return True

    def _client(self) -> YouTubeTranscriptApi:
        if self._api is None:
            self._api = YouTubeTranscriptApi(proxy_config=build_proxy_config(self.cfg))
        return self._api

    def fetch(self, video_id: str) -> FetchedTranscriptData:
        languages = self.cfg.app.transcripts.languages
        try:
            fetched: Any = self._client().fetch(video_id, languages=languages)
        except CouldNotRetrieveTranscript as exc:
            raise TranscriptFailure(
                classify_exception(exc), str(exc), provider=self.name, cause=exc
            ) from exc
        except Exception as exc:  # network/SSL/proxy errors from `requests`
            raise TranscriptFailure(
                FailureKind.TRANSIENT, str(exc), provider=self.name, cause=exc
            ) from exc

        segments = [
            TranscriptSegment(start=float(s.start), duration=float(s.duration), text=s.text)
            for s in fetched.snippets
        ]
        if not segments:
            # An empty caption track is indistinguishable from none at all.
            raise TranscriptFailure(
                FailureKind.NO_CAPTIONS,
                "transcript returned zero segments",
                provider=self.name,
            )

        log.info(
            "transcript fetched",
            extra={
                "video_id": video_id,
                "provider": str(self.name),
                "segments": len(segments),
                "language": fetched.language_code,
                "generated": fetched.is_generated,
            },
        )
        return FetchedTranscriptData(
            video_id=video_id,
            provider=self.name,
            segments=segments,
            language=fetched.language,
            language_code=fetched.language_code,
            is_generated=bool(fetched.is_generated),
        )


__all__ = ["YouTubeTranscriptApiSource", "build_proxy_config", "classify_exception"]
