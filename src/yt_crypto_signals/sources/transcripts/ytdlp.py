"""Provider 2: yt-dlp subtitle extraction (version 2026.8.19).

Fallback for when the direct caption endpoint is blocked. yt-dlp's YouTube
extractor negotiates a player client and can attach a PO token, which often
succeeds where the lighter provider fails.

Deliberate choices:

* **No video download, and no subtitle files.** `extract_info(download=False)`
  returns caption *URLs*; we fetch the chosen one ourselves. That avoids
  temp-file cleanup and keeps the bytes on the proxy to a minimum.
* **No cookies, ever.** When the bgutil PO-token provider is active, sending
  cookies alongside the token is counter-productive and risks the account. The
  provider has no cookie path at all, which makes that unrepresentable rather
  than merely discouraged.
* **Classification is text-based.** YouTube supplies the bot-detection wording
  at runtime; yt-dlp does not hardcode it, so there is no exception type to
  match. Patterns live in `config.yaml`.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError

from ...enums import FailureKind, TranscriptProvider
from ...httpc import build_client
from ...logging import get_logger
from .base import (
    FetchedTranscriptData,
    TranscriptFailure,
    TranscriptSegment,
    TranscriptSource,
    classify_message,
)

log = get_logger("transcripts.ytdlp")

#: Preference order. json3 is structured and exact; vtt needs parsing and
#: rounds timestamps to centiseconds.
_FORMAT_PREFERENCE = ("json3", "srv3", "vtt", "srv1")

_VTT_CUE_RE = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2}[.,]\d{3})"
)
_VTT_TAG_RE = re.compile(r"<[^>]+>")


def _vtt_timestamp_to_seconds(value: str) -> float:
    hours, minutes, rest = value.split(":")
    seconds = rest.replace(",", ".")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_json3(payload: str) -> list[TranscriptSegment]:
    """Parse YouTube's json3 caption format.

    Events without a `segs` array are window/style directives rather than
    text, and are skipped.
    """
    data = json.loads(payload)
    segments: list[TranscriptSegment] = []
    for event in data.get("events") or []:
        segs = event.get("segs")
        if not segs:
            continue
        text = "".join(seg.get("utf8", "") for seg in segs)
        if not text.strip():
            continue
        start_ms = event.get("tStartMs")
        if start_ms is None:
            continue
        segments.append(
            TranscriptSegment(
                start=float(start_ms) / 1000.0,
                duration=float(event.get("dDurationMs") or 0) / 1000.0,
                text=text.replace("\n", " ").strip(),
            )
        )
    return segments


def parse_vtt(payload: str) -> list[TranscriptSegment]:
    """Parse WebVTT.

    Auto-generated VTT repeats each line as a rolling caption, so consecutive
    duplicate texts are collapsed — otherwise the transcript would contain
    every phrase two or three times and inflate extraction cost.
    """
    segments: list[TranscriptSegment] = []
    blocks = re.split(r"\n\s*\n", payload)
    for block in blocks:
        match = _VTT_CUE_RE.search(block)
        if not match:
            continue
        lines = [
            _VTT_TAG_RE.sub("", line).strip()
            for line in block.splitlines()
            if line.strip() and not _VTT_CUE_RE.search(line) and not line.startswith("WEBVTT")
        ]
        text = " ".join(line for line in lines if line)
        if not text:
            continue
        if segments and segments[-1].text == text:
            continue
        start = _vtt_timestamp_to_seconds(match.group("start"))
        end = _vtt_timestamp_to_seconds(match.group("end"))
        segments.append(TranscriptSegment(start=start, duration=max(end - start, 0.0), text=text))
    return segments


def _pick_track(info: dict[str, Any], languages: list[str]) -> tuple[str, str, bool] | None:
    """Choose a caption track: (url, ext, is_generated).

    Manual captions are preferred over automatic ones because auto-captions
    mangle tickers, which is the dominant source of bad extractions.
    """
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}

    for source, is_generated in ((manual, False), (auto, True)):
        for lang in languages:
            tracks = source.get(lang)
            if not tracks:
                continue
            for ext in _FORMAT_PREFERENCE:
                for track in tracks:
                    if track.get("ext") == ext and track.get("url"):
                        return str(track["url"]), ext, is_generated
            # Nothing in our preferred formats; take whatever has a URL.
            for track in tracks:
                if track.get("url"):
                    return str(track["url"]), str(track.get("ext") or "vtt"), is_generated
    return None


class YtDlpSource(TranscriptSource):
    name = TranscriptProvider.YT_DLP

    def is_available(self) -> bool:
        return True

    def _ydl_options(self) -> dict[str, Any]:
        from ...httpc import proxy_url_for

        tc = self.cfg.app.transcripts
        opts: dict[str, Any] = {
            "skip_download": True,
            "writesubtitles": False,
            "writeautomaticsub": False,
            "subtitleslangs": list(tc.languages),
            "subtitlesformat": tc.ytdlp_subtitle_format,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            # Our tenacity ladder owns retries; yt-dlp's own retrying would
            # stack on top of it and stretch a single run past the timer.
            "retries": 0,
            "extractor_retries": 1,
            "socket_timeout": self.cfg.app.reliability.http_timeout_seconds,
            # Never cookies: see the module docstring.
            "cookiefile": None,
            "cookiesfrombrowser": None,
        }
        proxy = proxy_url_for(self.cfg)
        if proxy:
            opts["proxy"] = proxy
        if self.po_token_provider_configured():
            # Exact key/option per the bgutil-ytdlp-pot-provider docs.
            opts["extractor_args"] = {
                "youtubepot-bgutilhttp": {"base_url": tc.ytdlp_po_token_provider_url}
            }
        return opts

    def po_token_provider_configured(self) -> bool:
        """True when the bgutil PO-token plugin is importable.

        Checked rather than assumed: pointing `extractor_args` at a provider
        that is not installed is silently ignored by yt-dlp, which would make
        a misconfigured sidecar look like a YouTube-side block.
        """
        if not self.cfg.app.transcripts.ytdlp_po_token_provider_url:
            return False
        import importlib.util

        for module in ("yt_dlp_plugins.extractor.getpot_bgutil_http", "bgutil_ytdlp_pot_provider"):
            try:
                if importlib.util.find_spec(module) is not None:
                    return True
            except (ImportError, AttributeError, ValueError):
                # yt-dlp installs a meta-path finder for `yt_dlp_plugins` that
                # *raises* ModuleNotFoundError rather than returning None when
                # no plugin directories exist, so a plain find_spec would
                # propagate instead of answering the question.
                continue
        return False

    def _extract_info(self, video_id: str) -> dict[str, Any]:
        url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            with YoutubeDL(self._ydl_options()) as ydl:
                info = ydl.extract_info(url, download=False)
        except (DownloadError, ExtractorError) as exc:
            raise TranscriptFailure(
                classify_message(str(exc), self.cfg), str(exc), provider=self.name, cause=exc
            ) from exc
        except Exception as exc:
            raise TranscriptFailure(
                FailureKind.TRANSIENT, str(exc), provider=self.name, cause=exc
            ) from exc
        if not isinstance(info, dict):
            raise TranscriptFailure(
                FailureKind.TRANSIENT, "yt-dlp returned no metadata", provider=self.name
            )
        return info

    def _download_track(self, url: str) -> str:
        try:
            with build_client(self.cfg, use_proxy=True) as client:
                response = client.get(url)
                response.raise_for_status()
                return response.text
        except httpx.HTTPStatusError as exc:
            kind = (
                FailureKind.BLOCKED
                if exc.response.status_code in (401, 403, 429)
                else FailureKind.TRANSIENT
            )
            raise TranscriptFailure(
                kind,
                f"caption download failed with HTTP {exc.response.status_code}",
                provider=self.name,
                cause=exc,
            ) from exc
        except httpx.HTTPError as exc:
            raise TranscriptFailure(
                FailureKind.TRANSIENT, f"caption download failed: {exc}", provider=self.name
            ) from exc

    def fetch(self, video_id: str) -> FetchedTranscriptData:
        info = self._extract_info(video_id)

        if info.get("is_live"):
            raise TranscriptFailure(
                FailureKind.UNAVAILABLE,
                "video is still live; captions are not final",
                provider=self.name,
            )

        track = _pick_track(info, self.cfg.app.transcripts.languages)
        if track is None:
            available = sorted(
                set(info.get("subtitles") or {}) | set(info.get("automatic_captions") or {})
            )
            raise TranscriptFailure(
                FailureKind.NO_CAPTIONS,
                f"no captions in {self.cfg.app.transcripts.languages}; "
                f"available: {available[:10] or 'none'}",
                provider=self.name,
            )

        url, ext, is_generated = track
        payload = self._download_track(url)

        try:
            segments = parse_json3(payload) if ext in ("json3", "srv3") else parse_vtt(payload)
        except (json.JSONDecodeError, ValueError) as exc:
            raise TranscriptFailure(
                FailureKind.TRANSIENT,
                f"could not parse {ext} captions: {exc}",
                provider=self.name,
                cause=exc,
            ) from exc

        if not segments:
            raise TranscriptFailure(
                FailureKind.NO_CAPTIONS,
                f"{ext} caption track parsed to zero segments",
                provider=self.name,
            )

        log.info(
            "transcript fetched",
            extra={
                "video_id": video_id,
                "provider": str(self.name),
                "segments": len(segments),
                "format": ext,
                "generated": is_generated,
                "po_token_provider": self.po_token_provider_configured(),
            },
        )
        return FetchedTranscriptData(
            video_id=video_id,
            provider=self.name,
            segments=segments,
            language=info.get("language"),
            language_code=info.get("language"),
            is_generated=is_generated,
            extra={"subtitle_format": ext, "title": info.get("title")},
        )


__all__ = ["YtDlpSource", "parse_json3", "parse_vtt"]
