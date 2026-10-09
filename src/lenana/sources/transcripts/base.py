"""Transcript provider interface and failure classification.

Transcript fetching is the least reliable part of this system: YouTube
actively blocks datacenter IPs, captions appear late on long streams, and the
error surface differs per provider. Everything here exists to turn that mess
into one of four outcomes the pipeline can act on — see `FailureKind`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

from ...config import Config
from ...enums import FailureKind, TranscriptProvider


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """One caption cue.

    `duration` is how long the cue stays on screen, not how long the speech
    lasted, so consecutive segments can overlap. Chunking relies on `start`
    only, for that reason.
    """

    start: float
    duration: float
    text: str


@dataclass
class FetchedTranscriptData:
    """A successfully fetched transcript, normalised across providers."""

    video_id: str
    provider: TranscriptProvider
    segments: list[TranscriptSegment]
    language: str | None = None
    language_code: str | None = None
    is_generated: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def full_text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())

    @property
    def duration_seconds(self) -> float:
        if not self.segments:
            return 0.0
        last = self.segments[-1]
        return last.start + last.duration

    def to_segment_dicts(self) -> list[dict[str, Any]]:
        """JSON-serialisable form for the `transcript.segments` column."""
        return [asdict(s) for s in self.segments]

    def __len__(self) -> int:
        return len(self.segments)


class TranscriptFailure(Exception):
    """A classified transcript fetch failure.

    Carrying the classification on the exception is what lets the transcribe
    stage decide between "retry with backoff" and "give up and skip" without
    re-inspecting provider-specific error types.
    """

    def __init__(
        self,
        kind: FailureKind,
        message: str,
        provider: TranscriptProvider | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.provider = provider
        self.cause = cause

    @property
    def is_retryable(self) -> bool:
        return self.kind.is_retryable

    def __str__(self) -> str:
        where = f"[{self.provider}] " if self.provider else ""
        return f"{where}{self.kind}: {self.message}"


class ProviderUnavailable(TranscriptFailure):
    """The provider cannot run at all (disabled, missing dependency).

    Classified as TRANSIENT so the orchestrator moves to the next provider
    rather than condemning the video.
    """

    def __init__(self, message: str, provider: TranscriptProvider | None = None) -> None:
        super().__init__(FailureKind.TRANSIENT, message, provider)


def classify_message(message: str, cfg: Config) -> FailureKind:
    """Classify an error by its text, using patterns from config.yaml.

    yt-dlp does not hardcode YouTube's bot-detection wording — it is
    server-supplied text interpolated into the error — so there is no
    exception type to match on. Keeping the patterns in config means a change
    in YouTube's phrasing is a config edit, not a release.
    """
    text = (message or "").lower()
    tc = cfg.app.transcripts

    # Order matters: "no captions" and "unavailable" are more specific
    # conclusions than "blocked", and a blocked response can mention either.
    for pattern in tc.no_captions_message_patterns:
        if pattern.lower() in text:
            return FailureKind.NO_CAPTIONS
    for pattern in tc.unavailable_message_patterns:
        if pattern.lower() in text:
            return FailureKind.UNAVAILABLE
    for pattern in tc.blocked_message_patterns:
        if pattern.lower() in text:
            return FailureKind.BLOCKED
    return FailureKind.TRANSIENT


class TranscriptSource(ABC):
    """One way of getting a transcript."""

    name: TranscriptProvider

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    @abstractmethod
    def is_available(self) -> bool:
        """Whether this provider can be attempted at all."""

    @abstractmethod
    def fetch(self, video_id: str) -> FetchedTranscriptData:
        """Fetch a transcript, or raise `TranscriptFailure`."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name}>"


__all__ = [
    "FetchedTranscriptData",
    "ProviderUnavailable",
    "TranscriptFailure",
    "TranscriptSegment",
    "TranscriptSource",
    "classify_message",
]
