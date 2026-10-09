"""Quote verification: the anti-hallucination guard."""

from __future__ import annotations

from lenana.config import Config
from lenana.enums import RejectReason
from lenana.llm.schemas import RawCall, validate_call
from lenana.sources.transcripts.base import TranscriptSegment
from lenana.verify.quotes import locate_quote, verify_quote, window_text

TRANSCRIPT_LINES = [
    (0.0, "welcome back everyone to another episode"),
    (5.0, "today we are looking at the charts"),
    (600.0, "i think soul is going to three hundred"),
    (605.0, "before the end of this year for sure"),
    (610.0, "and that is my honest opinion on it"),
    (2000.0, "ethereum looks weak here i would stay away"),
]


def transcript() -> list[TranscriptSegment]:
    return [TranscriptSegment(start=s, duration=5.0, text=t) for s, t in TRANSCRIPT_LINES]


def test_exact_quote_verifies(config: Config) -> None:
    verdict = verify_quote("i think soul is going to three hundred", 600.0, transcript(), config)
    assert verdict.ok
    assert verdict.score == 100.0


def test_quote_with_added_punctuation_verifies(config: Config) -> None:
    """Claude reproduces punctuation and casing that auto-captions never had,
    so raw string comparison would wrongly reject this."""
    verdict = verify_quote(
        "I think SOL is going to three hundred, before the end of this year!",
        602.0,
        transcript(),
        config,
    )
    assert verdict.ok, f"score was {verdict.score}"


def test_quote_spanning_two_segments_verifies(config: Config) -> None:
    verdict = verify_quote(
        "going to three hundred before the end of this year", 603.0, transcript(), config
    )
    assert verdict.ok


def test_fabricated_quote_is_rejected(config: Config) -> None:
    verdict = verify_quote(
        "bitcoin will absolutely reach one million dollars next week", 600.0, transcript(), config
    )
    assert not verdict.ok
    assert verdict.reason is RejectReason.QUOTE_NOT_FOUND


def test_real_quote_at_the_wrong_timestamp_is_rejected(config: Config) -> None:
    """The quote exists, but 1400s away from where the model claimed it. The
    window check is what catches a misattributed call."""
    verdict = verify_quote("i think soul is going to three hundred", 2000.0, transcript(), config)
    assert not verdict.ok
    assert verdict.reason is RejectReason.QUOTE_NOT_FOUND


def test_diagnostic_finds_the_quote_elsewhere(config: Config) -> None:
    """Distinguishes "the model moved a real quote" from "the model invented
    one" — the distinction that makes a dry-run report actionable."""
    located = locate_quote("i think soul is going to three hundred", transcript(), config)
    assert located is not None
    at, score = located
    assert score >= config.app.verification.fuzzy_threshold
    assert 500 <= at <= 700


def test_too_short_quote_is_rejected(config: Config) -> None:
    verdict = verify_quote("sol up", 600.0, transcript(), config)
    assert not verdict.ok
    assert verdict.reason is RejectReason.QUOTE_TOO_SHORT


def test_timestamp_past_end_of_transcript_is_rejected(config: Config) -> None:
    verdict = verify_quote("i think soul is going to three hundred", 99999.0, transcript(), config)
    assert not verdict.ok
    assert verdict.reason is RejectReason.TIMESTAMP_OUT_OF_RANGE


def test_negative_timestamp_is_rejected(config: Config) -> None:
    verdict = verify_quote("i think soul is going to three hundred", -5.0, transcript(), config)
    assert not verdict.ok
    assert verdict.reason is RejectReason.TIMESTAMP_OUT_OF_RANGE


def test_empty_transcript_rejects(config: Config) -> None:
    verdict = verify_quote("anything at all here", 10.0, [], config)
    assert not verdict.ok
    assert verdict.reason is RejectReason.QUOTE_NOT_FOUND


def test_window_text_respects_radius(config: Config) -> None:
    text = window_text(transcript(), 600.0, 10.0)
    assert "three hundred" in text
    assert "ethereum" not in text
    assert "welcome back" not in text


def test_threshold_is_configurable(config: Config) -> None:
    loose = "i think sol is going to about three hundred or so maybe"
    config.app.verification.fuzzy_threshold = 100
    strict_verdict = verify_quote(loose, 600.0, transcript(), config)
    config.app.verification.fuzzy_threshold = 60
    loose_verdict = verify_quote(loose, 600.0, transcript(), config)
    assert not strict_verdict.ok
    assert loose_verdict.ok


# --------------------------------------------------------------------------
# strict value validation
# --------------------------------------------------------------------------


def raw(**overrides: object) -> RawCall:
    base: dict[str, object] = {
        "token_symbol": "SOL",
        "token_name": None,
        "direction": "up",
        "price_target": 300.0,
        "target_currency": "USD",
        "timeframe_text": "end of year",
        "timeframe_days": 80.0,
        "confidence": "medium",
        "speaker": "Host",
        "quote": "sol is going to three hundred",
        "timestamp_seconds": 600.0,
        "is_sponsored": False,
        "is_conditional": False,
    }
    base.update(overrides)
    return RawCall.model_construct(**base)


def test_valid_call_passes_validation() -> None:
    call, reason, _ = validate_call(raw())
    assert call is not None and reason is None
    assert call.timestamp_seconds == 600


def test_negative_target_is_rejected_with_its_own_reason() -> None:
    call, reason, detail = validate_call(raw(price_target=-5.0))
    assert call is None
    assert reason is RejectReason.NEGATIVE_TARGET
    assert detail and "price_target" in detail


def test_zero_target_is_rejected() -> None:
    call, reason, _ = validate_call(raw(price_target=0.0))
    assert call is None and reason is RejectReason.NEGATIVE_TARGET


def test_unknown_direction_is_rejected() -> None:
    call, reason, _ = validate_call(raw(direction="sideways"))
    assert call is None
    assert reason is RejectReason.UNKNOWN_DIRECTION


def test_absurd_timeframe_is_rejected() -> None:
    call, reason, _ = validate_call(raw(timeframe_days=99999.0))
    assert call is None and reason is RejectReason.SCHEMA_INVALID


def test_empty_quote_is_rejected() -> None:
    call, reason, _ = validate_call(raw(quote=""))
    assert call is None and reason is RejectReason.SCHEMA_INVALID


def test_null_target_is_allowed() -> None:
    """A call with a direction and no number is still a useful call."""
    call, reason, _ = validate_call(raw(price_target=None, target_currency=None))
    assert call is not None and reason is None
    assert call.price_target is None
