"""Enumerations shared across the pipeline.

All of these are `StrEnum`, which matters for two reasons: SQLite stores
readable text (so hand-written SQL against the database needs no decoder
ring), and members interpolate as their value. A plain `(str, Enum)` mixin
renders as `Direction.UP` inside an f-string, which would have shipped
straight into the Telegram messages.
"""

from enum import StrEnum


class VideoState(StrEnum):
    """The video state machine.

    Each pipeline stage selects only rows in its own input state and writes the
    next one, which is what makes the stages idempotent and order-independent.
    """

    DISCOVERED = "discovered"
    TRANSCRIBED = "transcribed"
    EXTRACTED = "extracted"
    VERIFIED = "verified"
    ENRICHED = "enriched"
    CROSSCHECKED = "crosschecked"
    ALERT_QUEUED = "alert_queued"
    DONE = "done"

    # Terminal/failure states
    TRANSCRIPT_FAILED = "transcript_failed"
    PERMANENT_FAILED = "permanent_failed"
    SKIPPED = "skipped"


#: The happy path, in order. `runner.py` uses this to sequence stages.
PIPELINE_ORDER: tuple[VideoState, ...] = (
    VideoState.DISCOVERED,
    VideoState.TRANSCRIBED,
    VideoState.EXTRACTED,
    VideoState.VERIFIED,
    VideoState.ENRICHED,
    VideoState.CROSSCHECKED,
    VideoState.ALERT_QUEUED,
    VideoState.DONE,
)


class FailureKind(StrEnum):
    """Classification of a transcript fetch failure.

    Only TRANSIENT and BLOCKED are retried; the other two are dead ends and
    retrying them only burns proxy bandwidth and quota.
    """

    BLOCKED = "blocked"
    NO_CAPTIONS = "no_captions"
    UNAVAILABLE = "unavailable"
    TRANSIENT = "transient"

    @property
    def is_retryable(self) -> bool:
        return self in (FailureKind.BLOCKED, FailureKind.TRANSIENT)


class SkipReason(StrEnum):
    TOO_SHORT = "too_short"
    STILL_LIVE = "still_live"
    NO_CAPTIONS = "no_captions"
    UNAVAILABLE = "unavailable"
    NO_VERIFIED_CALLS = "no_verified_calls"


class TranscriptProvider(StrEnum):
    YOUTUBE_TRANSCRIPT_API = "youtube_transcript_api"
    YT_DLP = "yt_dlp"
    WHISPER = "whisper"


class Direction(StrEnum):
    UP = "up"
    DOWN = "down"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Sentiment(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class Divergence(StrEnum):
    """Whether other channels agreed about a coin inside the cross-check window.

    Richer than a boolean: SOLO (nobody else called it) is a meaningfully
    different signal from ALIGNED (others agreed), and both differ from SPLIT.
    """

    SOLO = "solo"
    ALIGNED = "aligned"
    SPLIT = "split"

    @property
    def is_split(self) -> bool:
        return self is Divergence.SPLIT


class VerifyStatus(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class RejectReason(StrEnum):
    """Why the deterministic guard threw a call away. Always logged."""

    QUOTE_NOT_FOUND = "quote_not_found"
    QUOTE_TOO_SHORT = "quote_too_short"
    TIMESTAMP_OUT_OF_RANGE = "timestamp_out_of_range"
    SCHEMA_INVALID = "schema_invalid"
    NEGATIVE_TARGET = "negative_target"
    UNKNOWN_DIRECTION = "unknown_direction"


class MappingStatus(StrEnum):
    PENDING = "pending"
    MAPPED = "mapped"
    AMBIGUOUS = "ambiguous"
    UNMAPPED = "unmapped"


class Outcome(StrEnum):
    HIT = "hit"
    MISS = "miss"
    NEUTRAL = "neutral"


class OutboxState(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class OutboxKind(StrEnum):
    VIDEO_ALERT = "video_alert"
    BACKFILL_SUMMARY = "backfill_summary"
    SCORECARD = "scorecard"
    HEALTH = "health"
    TEST = "test"
