"""Extract stage: run Claude over stored transcripts.

Each video gets its own `ExtractionRun` row recording the prompt version,
model and token cost. Re-extracting adds a run rather than replacing one,
which is what makes `reprocess --prompt-version v2` able to compare prompts
over the same history.
"""

from __future__ import annotations

from sqlmodel import Session, col, select

from ..config import Config
from ..enums import Confidence, Direction, VerifyStatus, VideoState
from ..llm.client import ClaudeExtractor, ExtractionError
from ..llm.extract import VideoContext, extract_from_segments
from ..logging import get_logger
from ..models import Call, Channel, ExtractionRun, Transcript, utcnow
from ..sources.transcripts.base import TranscriptSegment
from .runner import StageResult, advance, claim_videos

log = get_logger("pipeline.extract")


def segments_from_row(transcript: Transcript) -> list[TranscriptSegment]:
    """Rehydrate stored segment dicts into the dataclass the rest of the code uses."""
    return [
        TranscriptSegment(
            start=float(s.get("start", 0.0)),
            duration=float(s.get("duration", 0.0)),
            text=str(s.get("text", "")),
        )
        for s in transcript.segments or []
    ]


def extract(
    session: Session,
    cfg: Config,
    prompt_version: str | None = None,
    extractor: ClaudeExtractor | None = None,
) -> StageResult:
    """Extract calls from transcribed videos."""
    result = StageResult(stage="extract")
    videos = claim_videos(session, VideoState.TRANSCRIBED, cfg.app.reliability.stage_batch_size)
    if not videos:
        return result

    extractor = extractor or ClaudeExtractor(cfg)
    total_cost = 0.0

    for video in videos:
        if video.id is None:  # pragma: no cover - a claimed row always has an id
            continue
        video_pk = video.id
        transcript = session.exec(select(Transcript).where(Transcript.video_pk == video.id)).first()
        if transcript is None:
            # Should be unreachable: a transcribed video has a transcript. If it
            # happens, send it back rather than crashing the batch.
            video.state = VideoState.DISCOVERED
            session.add(video)
            session.commit()
            result.failed += 1
            continue

        channel = session.get(Channel, video.channel_pk)
        context = VideoContext(
            video_id=video.video_id,
            title=video.title,
            channel_name=(channel.name or channel.handle) if channel else video.video_id,
            published_at=video.published_at,
            default_host=channel.default_host if channel else "Host",
        )

        try:
            extraction = extract_from_segments(
                segments_from_row(transcript),
                context,
                cfg,
                extractor=extractor,
                prompt_version=prompt_version,
            )
        except (ExtractionError, Exception) as exc:
            # Extraction failures are not transcript failures: the transcript is
            # fine, so the video stays in TRANSCRIBED and is retried next tick
            # without consuming the transcript retry ladder.
            result.failed += 1
            video.last_error = f"{type(exc).__name__}: {exc}"[:1000]
            video.updated_at = utcnow()
            session.add(video)
            session.commit()
            log.error(
                "extraction failed",
                extra={"video_id": video.video_id, "error": str(exc)[:300]},
            )
            continue

        # Supersede earlier runs so queries have one active run per video.
        for previous in session.exec(
            select(ExtractionRun)
            .where(ExtractionRun.video_pk == video.id)
            .where(col(ExtractionRun.is_active).is_(True))
        ).all():
            previous.is_active = False
            session.add(previous)

        run = ExtractionRun(
            video_pk=video_pk,
            prompt_version=extraction.prompt_version,
            model=extraction.model,
            chunk_count=extraction.chunk_count,
            input_tokens=extraction.usage.input_tokens,
            output_tokens=extraction.usage.output_tokens,
            cache_read_tokens=extraction.usage.cache_read_tokens,
            cost_usd=extraction.usage.cost_usd,
            market_sentiment=extraction.market_sentiment,
            summary=extraction.summary,
        )
        session.add(run)
        session.flush()  # assign run.id before the calls reference it
        assert run.id is not None

        for raw in extraction.calls:
            session.add(
                Call(
                    video_pk=video_pk,
                    extraction_run_pk=run.id,
                    token_symbol=raw.token_symbol,
                    token_name=raw.token_name,
                    direction=Direction(raw.direction),
                    price_target=raw.price_target,
                    target_currency=raw.target_currency,
                    timeframe_text=raw.timeframe_text,
                    timeframe_days=raw.timeframe_days,
                    confidence=Confidence(raw.confidence),
                    speaker=raw.speaker,
                    quote=raw.quote,
                    timestamp_seconds=int(raw.timestamp_seconds),
                    is_sponsored=raw.is_sponsored,
                    is_conditional=raw.is_conditional,
                    verify_status=VerifyStatus.PENDING,
                )
            )

        advance(session, video, VideoState.EXTRACTED)
        session.commit()
        result.processed += 1
        total_cost += extraction.usage.cost_usd or 0.0

    result.notes["cost_usd"] = round(total_cost, 6)
    return result


__all__ = ["extract", "segments_from_row"]
