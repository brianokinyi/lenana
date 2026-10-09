"""SQLModel table definitions.

Conventions that hold across every table:

* **All datetimes are timezone-aware UTC.** SQLModel maps `datetime` to its
  `UTCDateTime` type, which refuses naive values, normalises to UTC on write,
  and returns aware UTC on read — so a value loaded from the database can
  never be mistaken for local time. `DISPLAY_TZ` is applied only when
  formatting output. Use `utcnow()` below, never `datetime.now()`.
* **Rows that represent observed facts are append-only.** A `Call` is never
  mutated after verification; scoring writes `CallScore`, cross-checking
  writes `Crosscheck`, and re-extraction creates a new `ExtractionRun`. This
  is what makes `reprocess --prompt-version v2` safe to run against history.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, Index, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlmodel import Field, SQLModel

from .enums import (
    Confidence,
    Direction,
    Divergence,
    MappingStatus,
    OutboxKind,
    OutboxState,
    Outcome,
    Sentiment,
    TranscriptProvider,
    VerifyStatus,
    VideoState,
)


def enum_type(enum_cls: type[StrEnum]) -> SAEnum:
    """Column type that stores an enum's **value**, not its member name.

    SQLAlchemy's default is to persist `Enum.name`, which would write
    `"VERIFIED"` where every other part of this system — config patterns, log
    fields, raw SQL in a debugging session — says `"verified"`. `native_enum`
    is off because SQLite has no ENUM type and a VARCHAR + CHECK constraint is
    what it would emit anyway.
    """
    return SAEnum(
        enum_cls,
        values_callable=lambda e: [member.value for member in e],
        native_enum=False,
        length=32,
    )


def utcnow() -> datetime:
    """Timezone-aware UTC now. The single source of 'current time' for rows."""
    return datetime.now(UTC)


# --------------------------------------------------------------------------
# Channels and videos
# --------------------------------------------------------------------------


class Channel(SQLModel, table=True):
    __tablename__ = "channel"

    id: int | None = Field(default=None, primary_key=True)
    handle: str = Field(index=True, unique=True, description="e.g. @CryptoBanterGroup")
    channel_id: str | None = Field(default=None, index=True, unique=True, description="UC...")
    name: str | None = None
    uploads_playlist_id: str | None = None
    #: Label applied to calls the extractor attributes to the unnamed host.
    default_host: str = "Host"
    enabled: bool = True
    resolved_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Video(SQLModel, table=True):
    """One row per video; carries the state machine."""

    __tablename__ = "video"
    __table_args__ = (
        Index("ix_video_state_retry", "state", "next_retry_at"),
        Index("ix_video_channel_published", "channel_pk", "published_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    video_id: str = Field(index=True, unique=True, description="11-char YouTube id")
    channel_pk: int = Field(foreign_key="channel.id", index=True)

    title: str = ""
    published_at: datetime = Field(index=True)
    duration_seconds: int | None = None
    is_livestream: bool = False
    actual_end_time: datetime | None = None

    state: VideoState = Field(
        default=VideoState.DISCOVERED, sa_type=enum_type(VideoState), index=True
    )
    skip_reason: str | None = None

    attempts: int = 0
    last_error: str | None = None
    last_error_kind: str | None = None
    next_retry_at: datetime | None = Field(default=None, index=True)

    #: Backfilled videos are processed normally but never alerted individually;
    #: the backfill command sends one summary message instead.
    is_backfill: bool = False

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    def url_at(self, seconds: int) -> str:
        return f"{self.url}&t={int(seconds)}s"


class Transcript(SQLModel, table=True):
    __tablename__ = "transcript"

    id: int | None = Field(default=None, primary_key=True)
    video_pk: int = Field(foreign_key="video.id", index=True, unique=True)
    provider: TranscriptProvider = Field(sa_type=enum_type(TranscriptProvider))
    language: str | None = None
    is_generated: bool = True
    #: [{"start": float, "duration": float, "text": str}, ...] in source order.
    segments: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    full_text: str = ""
    char_count: int = 0
    fetched_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


class ExtractionRun(SQLModel, table=True):
    """One Claude extraction pass over one video.

    Re-running with an improved prompt adds a row rather than replacing one, so
    old results stay comparable against new ones.
    """

    __tablename__ = "extraction_run"
    __table_args__ = (Index("ix_run_video_version", "video_pk", "prompt_version"),)

    id: int | None = Field(default=None, primary_key=True)
    video_pk: int = Field(foreign_key="video.id", index=True)
    prompt_version: str
    model: str
    chunk_count: int = 0

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    #: None when the model has no price configured; token counts remain valid.
    cost_usd: float | None = None

    market_sentiment: Sentiment | None = Field(default=None, sa_type=enum_type(Sentiment))
    summary: str | None = None

    is_active: bool = Field(default=True, description="False once superseded by a newer run")
    created_at: datetime = Field(default_factory=utcnow)


class Call(SQLModel, table=True):
    """A single explicit directional prediction. Immutable after verification."""

    __tablename__ = "call"
    __table_args__ = (
        Index("ix_call_symbol_created", "token_symbol", "created_at"),
        Index("ix_call_verify_mapping", "verify_status", "mapping_status"),
        Index("ix_call_coin_entry", "coingecko_id", "entry_price_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    video_pk: int = Field(foreign_key="video.id", index=True)
    extraction_run_pk: int = Field(foreign_key="extraction_run.id", index=True)

    # --- as extracted ---
    token_symbol: str = Field(index=True)
    token_name: str | None = None
    direction: Direction = Field(sa_type=enum_type(Direction))
    price_target: float | None = None
    target_currency: str | None = None
    timeframe_text: str | None = None
    timeframe_days: float | None = None
    confidence: Confidence = Field(default=Confidence.MEDIUM, sa_type=enum_type(Confidence))
    speaker: str = "Host"
    quote: str = ""
    timestamp_seconds: int = 0
    is_sponsored: bool = False
    is_conditional: bool = False

    # --- verification (deterministic guard, no LLM) ---
    verify_status: VerifyStatus = Field(
        default=VerifyStatus.PENDING, sa_type=enum_type(VerifyStatus), index=True
    )
    verify_score: float | None = Field(default=None, description="rapidfuzz 0-100")
    verify_reason: str | None = None
    verified_at: datetime | None = None

    # --- enrichment ---
    coingecko_id: str | None = Field(default=None, index=True)
    mapping_status: MappingStatus = Field(
        default=MappingStatus.PENDING, sa_type=enum_type(MappingStatus)
    )
    mapping_note: str | None = None
    entry_price_usd: float | None = None
    #: publish time + timestamp_seconds, i.e. when the call was actually spoken.
    entry_price_at: datetime | None = None

    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------
# Scoring and cross-checking (never mutate Call)
# --------------------------------------------------------------------------


class CallScore(SQLModel, table=True):
    __tablename__ = "call_score"

    id: int | None = Field(default=None, primary_key=True)
    call_pk: int = Field(foreign_key="call.id", index=True, unique=True)

    due_at: datetime = Field(index=True, description="entry_price_at + timeframe_days")
    scored_at: datetime | None = None

    price_at_eval: float | None = None
    pct_change: float | None = None
    outcome: Outcome | None = Field(default=None, sa_type=enum_type(Outcome))

    #: From 4h OHLC candles over [entry_price_at, due_at] — real wicks, so a
    #: target touched intraday is detected even if price closed away from it.
    target_touched: bool | None = None
    window_high: float | None = None
    window_low: float | None = None

    #: Recorded because CoinGecko's OHLC window is anchored to "now" and cannot
    #: be reconstructed for an old call later.
    price_source: str | None = None
    granularity: str | None = None


class Crosscheck(SQLModel, table=True):
    """Snapshot of what other channels said about the same coin.

    Computed at alert time (necessarily backward-looking) and recomputed
    nightly once the full ±window has elapsed.
    """

    __tablename__ = "crosscheck"

    id: int | None = Field(default=None, primary_key=True)
    call_pk: int = Field(foreign_key="call.id", index=True, unique=True)

    bull_count: int = 0
    bear_count: int = 0
    channel_count: int = 0
    divergence: Divergence = Field(
        default=Divergence.ALIGNED, sa_type=enum_type(Divergence)
    )
    #: [{"channel": str, "direction": str, "video_id": str}, ...]
    channels: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    window_days: int = 5
    computed_at: datetime = Field(default_factory=utcnow)
    recomputed_at: datetime | None = None


# --------------------------------------------------------------------------
# Notification outbox
# --------------------------------------------------------------------------


class Outbox(SQLModel, table=True):
    """Transactional outbox: the no-lost-alerts / no-duplicate-alerts guarantee.

    A row is inserted in the *same transaction* as the state change that caused
    it, so a crash can never leave "alerted but not recorded" or the reverse.
    `dedupe_key` is UNIQUE, which makes duplicate suppression a database
    constraint rather than application logic.
    """

    __tablename__ = "outbox"
    __table_args__ = (Index("ix_outbox_state_next", "state", "next_attempt_at"),)

    id: int | None = Field(default=None, primary_key=True)
    dedupe_key: str = Field(unique=True, index=True)
    kind: OutboxKind = Field(sa_type=enum_type(OutboxKind))
    chat_id: str
    body: str
    #: Messages over the 4096-char limit are split into several rows.
    part_index: int = 0
    part_total: int = 1

    state: OutboxState = Field(
        default=OutboxState.PENDING, sa_type=enum_type(OutboxState), index=True
    )
    attempts: int = 0
    last_error: str | None = None
    next_attempt_at: datetime = Field(default_factory=utcnow, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    sent_at: datetime | None = None


# --------------------------------------------------------------------------
# Prices
# --------------------------------------------------------------------------


class Coin(SQLModel, table=True):
    """Cached CoinGecko market list, used to resolve symbol collisions by
    picking the highest market cap among candidates."""

    __tablename__ = "coin"
    __table_args__ = (Index("ix_coin_symbol_mcap", "symbol", "market_cap"),)

    coingecko_id: str = Field(primary_key=True)
    symbol: str = Field(index=True)
    name: str = ""
    market_cap: float | None = None
    market_cap_rank: int | None = None
    refreshed_at: datetime = Field(default_factory=utcnow)


class PriceCache(SQLModel, table=True):
    """Every price point we have ever fetched, keyed to the hour.

    Permanent cache: CoinGecko's demo tier is rate-limited, and historical
    points never change.
    """

    __tablename__ = "price_cache"
    __table_args__ = (UniqueConstraint("coingecko_id", "ts_hour", name="uq_price_coin_hour"),)

    id: int | None = Field(default=None, primary_key=True)
    coingecko_id: str = Field(index=True)
    ts_hour: datetime = Field(index=True, description="aware UTC, truncated to the hour")
    price_usd: float
    source: str = "market_chart_range"
    fetched_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


class StageRun(SQLModel, table=True):
    """Per-stage execution record; the health stage reads this to decide
    whether a stage is failing repeatedly."""

    __tablename__ = "stage_run"
    __table_args__ = (Index("ix_stage_run_stage_started", "stage", "started_at"),)

    id: int | None = Field(default=None, primary_key=True)
    stage: str = Field(index=True)
    started_at: datetime = Field(default_factory=utcnow, index=True)
    finished_at: datetime | None = None
    ok: bool = False
    processed: int = 0
    failed: int = 0
    error: str | None = None


class ApiUsage(SQLModel, table=True):
    """Daily API unit counters, for the YouTube quota health check."""

    __tablename__ = "api_usage"
    __table_args__ = (UniqueConstraint("day", "service", name="uq_usage_day_service"),)

    id: int | None = Field(default=None, primary_key=True)
    day: str = Field(index=True, description="YYYY-MM-DD in UTC")
    service: str
    units: int = 0
    updated_at: datetime = Field(default_factory=utcnow)


class KV(SQLModel, table=True):
    """Small cursor store: backfill_done, last_discover_at, and friends."""

    __tablename__ = "kv"

    key: str = Field(primary_key=True)
    value: str = ""
    updated_at: datetime = Field(default_factory=utcnow)


__all__ = [
    "KV",
    "ApiUsage",
    "Call",
    "CallScore",
    "Channel",
    "Coin",
    "Crosscheck",
    "ExtractionRun",
    "Outbox",
    "PriceCache",
    "StageRun",
    "Transcript",
    "Video",
    "enum_type",
    "utcnow",
]
