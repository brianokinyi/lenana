"""Scoring thresholds and due-date calculation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from yt_crypto_signals.config import Config
from yt_crypto_signals.enums import Confidence, Direction, Outcome, VerifyStatus
from yt_crypto_signals.models import Call
from yt_crypto_signals.pipeline.score import classify, due_at_for, target_was_touched

ENTRY = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def make_call(**overrides: object) -> Call:
    base: dict[str, object] = {
        "video_pk": 1,
        "extraction_run_pk": 1,
        "token_symbol": "SOL",
        "direction": Direction.UP,
        "confidence": Confidence.MEDIUM,
        "speaker": "Host",
        "quote": "q",
        "timestamp_seconds": 0,
        "verify_status": VerifyStatus.VERIFIED,
        "entry_price_at": ENTRY,
        "entry_price_usd": 100.0,
    }
    base.update(overrides)
    return Call(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# classify
# --------------------------------------------------------------------------


def test_up_call_that_rises_past_threshold_is_a_hit() -> None:
    assert classify(Direction.UP, 10.0, 5.0) is Outcome.HIT


def test_up_call_that_falls_past_threshold_is_a_miss() -> None:
    assert classify(Direction.UP, -10.0, 5.0) is Outcome.MISS


def test_down_call_that_falls_is_a_hit() -> None:
    assert classify(Direction.DOWN, -12.0, 5.0) is Outcome.HIT


def test_down_call_that_rises_is_a_miss() -> None:
    assert classify(Direction.DOWN, 12.0, 5.0) is Outcome.MISS


def test_move_inside_the_band_is_neutral() -> None:
    """Not wrong, but it did not pay either."""
    assert classify(Direction.UP, 3.0, 5.0) is Outcome.NEUTRAL
    assert classify(Direction.DOWN, -4.9, 5.0) is Outcome.NEUTRAL
    assert classify(Direction.UP, 0.0, 5.0) is Outcome.NEUTRAL


def test_exactly_at_the_threshold_is_neutral() -> None:
    assert classify(Direction.UP, 5.0, 5.0) is Outcome.NEUTRAL
    assert classify(Direction.UP, -5.0, 5.0) is Outcome.NEUTRAL


def test_just_past_the_threshold_flips() -> None:
    assert classify(Direction.UP, 5.01, 5.0) is Outcome.HIT
    assert classify(Direction.UP, -5.01, 5.0) is Outcome.MISS


def test_threshold_is_configurable() -> None:
    assert classify(Direction.UP, 8.0, 10.0) is Outcome.NEUTRAL
    assert classify(Direction.UP, 8.0, 5.0) is Outcome.HIT


# --------------------------------------------------------------------------
# due dates
# --------------------------------------------------------------------------


def test_due_date_uses_the_stated_timeframe(config: Config) -> None:
    call = make_call(timeframe_days=7.0)
    assert due_at_for(call, config) == ENTRY + timedelta(days=7)


def test_missing_timeframe_falls_back_to_the_default(config: Config) -> None:
    config.app.scoring.default_timeframe_days = 30
    call = make_call(timeframe_days=None)
    assert due_at_for(call, config) == ENTRY + timedelta(days=30)


def test_fractional_timeframes_are_supported(config: Config) -> None:
    call = make_call(timeframe_days=0.5)
    assert due_at_for(call, config) == ENTRY + timedelta(hours=12)


def test_call_without_an_entry_time_cannot_be_scheduled(config: Config) -> None:
    assert due_at_for(make_call(entry_price_at=None), config) is None


# --------------------------------------------------------------------------
# target touch semantics
# --------------------------------------------------------------------------


def test_up_call_target_touched_by_the_high() -> None:
    """Uses the high, not the close, so a target hit intraday and given back
    still counts as touched."""
    assert target_was_touched(Direction.UP, 300.0, window_high=305.0, window_low=180.0)


def test_up_call_target_not_reached() -> None:
    assert not target_was_touched(Direction.UP, 300.0, window_high=295.0, window_low=180.0)


def test_up_call_target_exactly_reached_counts() -> None:
    assert target_was_touched(Direction.UP, 300.0, window_high=300.0, window_low=180.0)


def test_down_call_target_touched_by_the_low() -> None:
    assert target_was_touched(Direction.DOWN, 80.0, window_high=120.0, window_low=75.0)


def test_down_call_target_not_reached() -> None:
    assert not target_was_touched(Direction.DOWN, 80.0, window_high=120.0, window_low=85.0)


def test_down_call_ignores_the_high() -> None:
    """A down call is not satisfied by the price spiking upward."""
    assert not target_was_touched(Direction.DOWN, 80.0, window_high=500.0, window_low=90.0)
