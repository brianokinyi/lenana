"""Provider orchestration: try each source in configured order.

The orchestrator's job is choosing what to report when *every* provider
fails. It keeps the most informative classification rather than the last one,
because the last provider to run is not necessarily the one that understood
the problem — `no_captions` from a provider that reached YouTube tells us more
than `transient` from one that could not connect.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...config import Config
from ...enums import FailureKind, TranscriptProvider
from ...logging import get_logger
from .base import FetchedTranscriptData, TranscriptFailure, TranscriptSource
from .whisper import WhisperSource
from .ytapi import YouTubeTranscriptApiSource
from .ytdlp import YtDlpSource

log = get_logger("transcripts")

_SOURCE_TYPES: dict[str, type[TranscriptSource]] = {
    TranscriptProvider.YOUTUBE_TRANSCRIPT_API.value: YouTubeTranscriptApiSource,
    TranscriptProvider.YT_DLP.value: YtDlpSource,
    TranscriptProvider.WHISPER.value: WhisperSource,
}

#: Which classification wins when providers disagree. A definite answer about
#: the video beats an inability to reach it.
_KIND_PRIORITY: dict[FailureKind, int] = {
    FailureKind.UNAVAILABLE: 3,
    FailureKind.NO_CAPTIONS: 2,
    FailureKind.BLOCKED: 1,
    FailureKind.TRANSIENT: 0,
}


@dataclass
class ProviderAttempt:
    provider: TranscriptProvider
    ok: bool
    kind: FailureKind | None = None
    message: str | None = None
    segments: int = 0
    elapsed_ms: int = 0


@dataclass
class TranscriptOutcome:
    """Result of trying every provider, including the ones that failed.

    `test-proxy` prints the attempt list, which is what makes a VPS
    misconfiguration diagnosable in one command.
    """

    data: FetchedTranscriptData | None
    attempts: list[ProviderAttempt]
    failure: TranscriptFailure | None = None

    @property
    def ok(self) -> bool:
        return self.data is not None


def build_sources(cfg: Config) -> list[TranscriptSource]:
    """Instantiate providers in configured order, skipping unknown names."""
    sources: list[TranscriptSource] = []
    for name in cfg.app.transcripts.provider_order:
        source_type = _SOURCE_TYPES.get(name)
        if source_type is None:
            log.warning("unknown transcript provider in config", extra={"provider": name})
            continue
        sources.append(source_type(cfg))

    # Whisper is never in the default order; append it only when enabled, so
    # turning the flag on does not also require a config.yaml edit.
    if cfg.settings.whisper_fallback and not any(
        s.name is TranscriptProvider.WHISPER for s in sources
    ):
        sources.append(WhisperSource(cfg))
    return sources


def fetch_transcript(
    video_id: str, cfg: Config, sources: list[TranscriptSource] | None = None
) -> TranscriptOutcome:
    """Try providers in order and return the first success.

    Never raises for a transcript problem: the outcome carries the
    classification so the caller decides between retry, skip and alert.
    """
    import time

    sources = sources if sources is not None else build_sources(cfg)
    attempts: list[ProviderAttempt] = []
    worst: TranscriptFailure | None = None

    for source in sources:
        if not source.is_available():
            attempts.append(
                ProviderAttempt(
                    provider=source.name,
                    ok=False,
                    kind=FailureKind.TRANSIENT,
                    message="provider not available",
                )
            )
            continue

        started = time.monotonic()
        try:
            data = source.fetch(video_id)
        except TranscriptFailure as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            attempts.append(
                ProviderAttempt(
                    provider=source.name,
                    ok=False,
                    kind=exc.kind,
                    message=exc.message,
                    elapsed_ms=elapsed,
                )
            )
            log.warning(
                "transcript provider failed",
                extra={
                    "video_id": video_id,
                    "provider": str(source.name),
                    "kind": str(exc.kind),
                    "error": exc.message,
                    "elapsed_ms": elapsed,
                },
            )
            if worst is None or _KIND_PRIORITY[exc.kind] > _KIND_PRIORITY[worst.kind]:
                worst = exc
            continue

        attempts.append(
            ProviderAttempt(
                provider=source.name,
                ok=True,
                segments=len(data),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        )
        return TranscriptOutcome(data=data, attempts=attempts)

    if worst is None:
        worst = TranscriptFailure(FailureKind.TRANSIENT, "no transcript provider was available")
    return TranscriptOutcome(data=None, attempts=attempts, failure=worst)


__all__ = [
    "ProviderAttempt",
    "TranscriptOutcome",
    "build_sources",
    "fetch_transcript",
]
