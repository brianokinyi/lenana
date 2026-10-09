"""Chunking, overlap and cross-chunk merge."""

from __future__ import annotations

import pytest

from lenana.llm.chunking import chunk_transcript
from lenana.llm.merge import merge_calls
from lenana.llm.schemas import RawCall
from lenana.sources.transcripts.base import TranscriptSegment


def segments(count: int, step: float = 5.0) -> list[TranscriptSegment]:
    return [
        TranscriptSegment(start=i * step, duration=step, text=f"line {i}") for i in range(count)
    ]


def test_short_transcript_is_one_chunk() -> None:
    chunks = chunk_transcript(segments(10), chunk_seconds=1200, overlap_seconds=60)
    assert len(chunks) == 1
    assert len(chunks[0].segments) == 10


def test_windows_advance_by_chunk_minus_overlap() -> None:
    # 1 hour of transcript, 20-minute chunks, 60s overlap -> step is 19 minutes.
    chunks = chunk_transcript(segments(720, step=5.0), chunk_seconds=1200, overlap_seconds=60)
    starts = [c.start_seconds for c in chunks]
    assert starts[:3] == [0.0, 1140.0, 2280.0]
    assert all(c.segments for c in chunks), "no empty chunks"


def test_overlap_duplicates_boundary_segments() -> None:
    """A segment just before a boundary must appear in two chunks, which is the
    whole point of the overlap."""
    chunks = chunk_transcript(segments(400, step=5.0), chunk_seconds=1200, overlap_seconds=60)
    assert len(chunks) >= 2
    first = {s.start for s in chunks[0].segments}
    second = {s.start for s in chunks[1].segments}
    shared = first & second
    assert shared, "consecutive chunks should share the overlap window"
    assert all(1140.0 <= s < 1200.0 for s in shared)


def test_every_segment_appears_at_least_once() -> None:
    source = segments(500, step=7.0)
    chunks = chunk_transcript(source, chunk_seconds=600, overlap_seconds=30)
    covered = {s.start for c in chunks for s in c.segments}
    assert covered == {s.start for s in source}


def test_prompt_text_uses_integer_seconds() -> None:
    chunks = chunk_transcript(
        [TranscriptSegment(start=83.4, duration=2.0, text="sol to 300")],
        chunk_seconds=600,
        overlap_seconds=0,
    )
    assert chunks[0].to_prompt_text() == "[83] sol to 300"


def test_empty_transcript_yields_no_chunks() -> None:
    assert chunk_transcript([], chunk_seconds=600, overlap_seconds=60) == []


def test_overlap_must_be_smaller_than_chunk() -> None:
    with pytest.raises(ValueError, match="overlap"):
        chunk_transcript(segments(10), chunk_seconds=60, overlap_seconds=60)


def test_unordered_segments_are_sorted() -> None:
    unordered = [
        TranscriptSegment(start=30.0, duration=5.0, text="c"),
        TranscriptSegment(start=10.0, duration=5.0, text="a"),
        TranscriptSegment(start=20.0, duration=5.0, text="b"),
    ]
    chunks = chunk_transcript(unordered, chunk_seconds=600, overlap_seconds=0)
    assert [s.text for s in chunks[0].segments] == ["a", "b", "c"]


# --------------------------------------------------------------------------
# merge
# --------------------------------------------------------------------------


def call(
    symbol: str = "SOL",
    direction: str = "up",
    speaker: str = "Host",
    ts: float = 100.0,
    quote: str = "sol is going up",
    target: float | None = None,
    timeframe: str | None = None,
) -> RawCall:
    return RawCall(
        token_symbol=symbol,
        token_name=None,
        direction=direction,  # type: ignore[arg-type]
        price_target=target,
        target_currency="USD" if target else None,
        timeframe_text=timeframe,
        timeframe_days=None,
        confidence="medium",
        speaker=speaker,
        quote=quote,
        timestamp_seconds=ts,
        is_sponsored=False,
        is_conditional=False,
    )


def test_duplicate_across_overlap_is_merged() -> None:
    merged = merge_calls([call(ts=1150.0), call(ts=1155.0)])
    assert len(merged) == 1


def test_merge_keeps_the_call_with_a_target() -> None:
    merged = merge_calls(
        [
            call(ts=100.0, quote="sol up a lot and then some more words here"),
            call(ts=110.0, quote="sol 300", target=300.0),
        ]
    )
    assert len(merged) == 1
    assert merged[0].price_target == 300.0


def test_merge_prefers_timeframe_then_length() -> None:
    merged = merge_calls(
        [
            call(ts=100.0, quote="a much longer quote without any timeframe at all"),
            call(ts=105.0, quote="sol up by friday", timeframe="by friday"),
        ]
    )
    assert merged[0].timeframe_text == "by friday"


def test_different_tokens_are_not_merged() -> None:
    assert len(merge_calls([call(symbol="SOL"), call(symbol="ETH")])) == 2


def test_different_directions_are_not_merged() -> None:
    assert len(merge_calls([call(direction="up"), call(direction="down")])) == 2


def test_different_speakers_are_not_merged() -> None:
    assert len(merge_calls([call(speaker="Host"), call(speaker="Guest")])) == 2


def test_same_call_much_later_is_a_separate_call() -> None:
    """A host repeating a call 40 minutes on is making a second call, not a
    duplicate of the first."""
    merged = merge_calls([call(ts=100.0), call(ts=2500.0)])
    assert len(merged) == 2


def test_merged_output_is_time_ordered() -> None:
    merged = merge_calls([call(symbol="ETH", ts=500.0), call(symbol="SOL", ts=100.0)])
    assert [c.timestamp_seconds for c in merged] == [100.0, 500.0]


def test_symbol_is_normalised() -> None:
    assert call(symbol="$sol").token_symbol == "SOL"
