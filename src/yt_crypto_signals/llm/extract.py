"""Extraction orchestration: transcript -> chunks -> Claude -> merged calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..config import Config
from ..enums import Sentiment
from ..logging import get_logger
from ..sources.transcripts.base import TranscriptSegment
from .chunking import Chunk, chunk_transcript
from .client import ClaudeExtractor, Usage, load_prompt
from .merge import merge_calls
from .schemas import ChunkExtraction, RawCall, sentiment_of

log = get_logger("llm.extract")


@dataclass
class VideoContext:
    """Everything the prompt needs to know about the video."""

    video_id: str
    title: str
    channel_name: str
    published_at: datetime
    default_host: str = "Host"


@dataclass
class ExtractionResult:
    calls: list[RawCall]
    market_sentiment: Sentiment
    summary: str
    usage: Usage
    prompt_version: str
    model: str
    chunk_count: int
    #: Per-chunk results, retained for the dry-run report.
    chunk_results: list[tuple[int, ChunkExtraction]] = field(default_factory=list)
    raw_call_count: int = 0

    @property
    def merged_away(self) -> int:
        """How many duplicates the overlap produced and merging removed."""
        return self.raw_call_count - len(self.calls)


def _overall_sentiment(chunks: list[ChunkExtraction]) -> Sentiment:
    """Majority sentiment across chunks, with neutral as the tie-break.

    A long stream swings between moods; the modal chunk sentiment represents
    the video better than either the first or the last chunk.
    """
    if not chunks:
        return Sentiment.NEUTRAL
    counts: dict[Sentiment, int] = {}
    for chunk in chunks:
        sentiment = sentiment_of(chunk.market_sentiment)
        counts[sentiment] = counts.get(sentiment, 0) + 1
    best = max(counts.values())
    winners = [s for s, c in counts.items() if c == best]
    return Sentiment.NEUTRAL if len(winners) > 1 else winners[0]


def extract_from_segments(
    segments: list[TranscriptSegment],
    context: VideoContext,
    cfg: Config,
    extractor: ClaudeExtractor | None = None,
    prompt_version: str | None = None,
) -> ExtractionResult:
    """Extract every call from a transcript.

    One request per chunk. A failing chunk aborts the video rather than
    silently producing a partial result — a half-extracted video would look
    identical to one that genuinely had fewer calls.
    """
    ec = cfg.app.extraction
    version = prompt_version or ec.prompt_version
    prompt = load_prompt(version)
    extractor = extractor or ClaudeExtractor(cfg)

    chunks: list[Chunk] = chunk_transcript(
        segments, chunk_seconds=ec.chunk_minutes * 60, overlap_seconds=ec.chunk_overlap_seconds
    )
    log.info(
        "extracting",
        extra={
            "video_id": context.video_id,
            "chunks": len(chunks),
            "model": extractor.model,
            "prompt_version": version,
        },
    )

    usage = Usage()
    chunk_results: list[tuple[int, ChunkExtraction]] = []
    all_calls: list[RawCall] = []

    for chunk in chunks:
        rendered = prompt.render(
            channel=context.channel_name,
            title=context.title,
            publish_date=context.published_at.strftime("%Y-%m-%d"),
            default_host=context.default_host,
            chunk_index=chunk.index + 1,
            chunk_total=len(chunks),
            chunk_start=_hms(chunk.start_seconds),
            chunk_end=_hms(chunk.end_seconds),
            transcript=chunk.to_prompt_text(),
        )
        result, usage = extractor.extract(rendered, ChunkExtraction, usage)
        chunk_results.append((chunk.index, result))
        all_calls.extend(result.calls)
        log.debug(
            "chunk extracted",
            extra={
                "video_id": context.video_id,
                "chunk": chunk.index,
                "calls": len(result.calls),
            },
        )

    merged = merge_calls(all_calls)
    log.info(
        "extraction complete",
        extra={
            "video_id": context.video_id,
            "raw_calls": len(all_calls),
            "merged_calls": len(merged),
            **usage.as_dict(),
        },
    )

    return ExtractionResult(
        calls=merged,
        market_sentiment=_overall_sentiment([r for _, r in chunk_results]),
        summary=_combined_summary([r for _, r in chunk_results]),
        usage=usage,
        prompt_version=version,
        model=extractor.model,
        chunk_count=len(chunks),
        chunk_results=chunk_results,
        raw_call_count=len(all_calls),
    )


def _combined_summary(chunks: list[ChunkExtraction]) -> str:
    """One sentence for the video: the first chunk's summary is the headline.

    Concatenating every chunk summary would produce a paragraph that no alert
    has room for, so the opening segment wins.
    """
    for chunk in chunks:
        if chunk.summary.strip():
            return chunk.summary.strip()
    return ""


def _hms(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


__all__ = ["ExtractionResult", "VideoContext", "extract_from_segments"]
