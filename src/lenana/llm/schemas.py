"""Pydantic models for Claude extraction, in two tiers.

The two-tier split is deliberate and load-bearing:

**Wire models** (`RawCall`, `ChunkExtraction`) are what `client.messages.parse`
validates against. They constrain *shape* — the API enforces the enums and
required fields during generation — but carry no value constraints. The
anthropic SDK's `transform_schema` moves numeric bounds out of a JSON schema
into the field description anyway, so a bound here would not be enforced by
the API; it would only turn a bad value into a raised exception at parse time.

**Strict validation** (`validate_call`) runs afterwards in the verify stage.
That way a nonsensical value becomes a *logged rejection with a reason* —
which is what the anti-hallucination requirement asks for — instead of an
exception that loses the whole video's extraction.

Every wire field is required (nullable where optional) because strict
structured outputs expect a complete `required` list.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..enums import Confidence, Direction, RejectReason, Sentiment

# --------------------------------------------------------------------------
# Wire models — the json_schema sent to Claude
# --------------------------------------------------------------------------


class RawCall(BaseModel):
    """One explicit directional call, exactly as Claude reports it."""

    model_config = ConfigDict(extra="forbid")

    token_symbol: str = Field(description="Ticker in uppercase, e.g. BTC, SOL, ETH. No $ prefix.")
    token_name: str | None = Field(
        description="Full project name if stated, e.g. 'Solana'. Null if only the ticker was used."
    )
    direction: Literal["up", "down"] = Field(
        description="Whether the speaker predicts the price rises or falls."
    )
    price_target: float | None = Field(
        description="Numeric target if one was given, e.g. 300. Null if no number was stated."
    )
    target_currency: str | None = Field(
        description="Currency of price_target, usually USD. Null when there is no target."
    )
    timeframe_text: str | None = Field(
        description="Timeframe exactly as spoken, e.g. 'by end of year'. Null if none."
    )
    timeframe_days: float | None = Field(
        description="Your normalised estimate of timeframe_text in days. Null if none."
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description="How certain the speaker sounded, judged from their wording only."
    )
    speaker: str = Field(
        description="Speaker's name only if clearly identified in the transcript; "
        "otherwise exactly 'Host' or 'Guest'."
    )
    quote: str = Field(
        description="Verbatim quote from the transcript containing the call. "
        "Copy the words exactly as they appear; do not paraphrase or clean up."
    )
    timestamp_seconds: float = Field(
        description="The bracketed number starting the transcript line where the quote begins."
    )
    is_sponsored: bool = Field(
        description="True if this call occurs inside an advert, sponsorship or affiliate segment."
    )
    is_conditional: bool = Field(
        description="True if the call depends on a stated condition, e.g. 'if BTC holds 100k'."
    )

    @field_validator("token_symbol")
    @classmethod
    def _normalise_symbol(cls, v: str) -> str:
        return v.strip().lstrip("$").upper()


class ChunkExtraction(BaseModel):
    """Claude's complete response for one transcript chunk."""

    model_config = ConfigDict(extra="forbid")

    calls: list[RawCall] = Field(
        description="Every explicit directional call in this chunk. Empty list if there are none."
    )
    market_sentiment: Literal["bullish", "bearish", "neutral"] = Field(
        description="Overall tone of this chunk about the crypto market as a whole."
    )
    summary: str = Field(description="One sentence summarising this chunk.")


# --------------------------------------------------------------------------
# Strict validation — applied after parsing, failures become rejections
# --------------------------------------------------------------------------


class ValidatedCall(BaseModel):
    """A call that passed value validation. Enum-typed, ready to store."""

    model_config = ConfigDict(extra="forbid")

    token_symbol: str = Field(min_length=1, max_length=20)
    token_name: str | None = None
    direction: Direction
    price_target: float | None = Field(default=None, gt=0)
    target_currency: str | None = None
    timeframe_text: str | None = None
    timeframe_days: float | None = Field(default=None, gt=0, le=3650)
    confidence: Confidence
    speaker: str = Field(min_length=1, max_length=120)
    quote: str = Field(min_length=1)
    timestamp_seconds: int = Field(ge=0)
    is_sponsored: bool = False
    is_conditional: bool = False


#: Pydantic field -> rejection reason, so a validation failure is reported as
#: the specific thing that was wrong rather than a generic schema error.
_FIELD_REJECTIONS: dict[str, RejectReason] = {
    "price_target": RejectReason.NEGATIVE_TARGET,
    "direction": RejectReason.UNKNOWN_DIRECTION,
}


def validate_call(raw: RawCall) -> tuple[ValidatedCall | None, RejectReason | None, str | None]:
    """Apply strict validation to a raw call.

    Returns `(call, None, None)` on success, or `(None, reason, detail)` so the
    caller can store the rejection with an explanation.
    """
    # Enum coercion happens first and is reported specifically: converting
    # inside the model constructor would raise a bare ValueError that the
    # field-to-reason mapping below could not attribute.
    try:
        direction = Direction(raw.direction)
    except ValueError:
        return (
            None,
            RejectReason.UNKNOWN_DIRECTION,
            f"direction must be 'up' or 'down', got {raw.direction!r}",
        )
    try:
        confidence = Confidence(raw.confidence)
    except ValueError:
        return (
            None,
            RejectReason.SCHEMA_INVALID,
            f"confidence must be low/medium/high, got {raw.confidence!r}",
        )

    try:
        call = ValidatedCall(
            token_symbol=raw.token_symbol,
            token_name=raw.token_name,
            direction=direction,
            price_target=raw.price_target,
            target_currency=raw.target_currency,
            timeframe_text=raw.timeframe_text,
            timeframe_days=raw.timeframe_days,
            confidence=confidence,
            speaker=raw.speaker,
            quote=raw.quote,
            timestamp_seconds=int(raw.timestamp_seconds),
            is_sponsored=raw.is_sponsored,
            is_conditional=raw.is_conditional,
        )
    except (ValidationError, ValueError) as exc:
        reason = RejectReason.SCHEMA_INVALID
        detail = str(exc).replace("\n", " ")
        if isinstance(exc, ValidationError):
            for err in exc.errors():
                field = str(err["loc"][0]) if err["loc"] else ""
                if field in _FIELD_REJECTIONS:
                    reason = _FIELD_REJECTIONS[field]
                    break
            detail = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
            )
        return None, reason, detail
    return call, None, None


def sentiment_of(value: str) -> Sentiment:
    try:
        return Sentiment(value)
    except ValueError:
        return Sentiment.NEUTRAL


__all__ = [
    "ChunkExtraction",
    "RawCall",
    "ValidatedCall",
    "sentiment_of",
    "validate_call",
]
