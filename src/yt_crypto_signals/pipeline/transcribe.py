"""Transcribe stage: fetch and store transcripts for discovered videos."""

from __future__ import annotations

from sqlmodel import Session, select

from ..config import Config
from ..enums import VideoState
from ..logging import get_logger
from ..models import Transcript, utcnow
from ..sources.transcripts.registry import build_sources, fetch_transcript
from .runner import StageResult, advance, batched, claim_videos, record_failure, requeue_failed

log = get_logger("pipeline.transcribe")


def transcribe(session: Session, cfg: Config) -> StageResult:
    """Fetch transcripts, storing timestamped segments and the winning provider."""
    result = StageResult(stage="transcribe")
    batch_size = cfg.app.reliability.stage_batch_size

    # Videos whose backoff has elapsed rejoin the normal queue first.
    requeue_failed(session, cfg, limit=batch_size)

    videos = claim_videos(session, VideoState.DISCOVERED, batch_size)
    if not videos:
        return result

    # Providers are built once per stage run: the youtube-transcript-api
    # client holds a requests Session whose connection pool is worth reusing
    # across a batch.
    sources = build_sources(cfg)

    for group in batched(videos, 5):
        for video in group:
            outcome = fetch_transcript(video.video_id, cfg, sources=sources)

            if not outcome.ok or outcome.data is None:
                failure = outcome.failure
                assert failure is not None
                record_failure(session, video, failure.kind, str(failure), cfg)
                result.failed += 1
                continue

            data = outcome.data
            existing = session.exec(
                select(Transcript).where(Transcript.video_pk == video.id)
            ).first()
            if existing is not None:
                # Re-transcribing replaces the stored text in place; the
                # transcript is a cache of an external fact, not an observation
                # of our own, so there is no history worth keeping.
                existing.provider = data.provider
                existing.language = data.language_code
                existing.is_generated = data.is_generated
                existing.segments = data.to_segment_dicts()
                existing.full_text = data.full_text
                existing.char_count = len(data.full_text)
                existing.fetched_at = utcnow()
                session.add(existing)
            else:
                session.add(
                    Transcript(
                        video_pk=video.id,
                        provider=data.provider,
                        language=data.language_code,
                        is_generated=data.is_generated,
                        segments=data.to_segment_dicts(),
                        full_text=data.full_text,
                        char_count=len(data.full_text),
                    )
                )

            advance(session, video, VideoState.TRANSCRIBED)
            result.processed += 1

        session.commit()

    return result


__all__ = ["transcribe"]
