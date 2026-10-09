"""Verify stage: the deterministic anti-hallucination guard.

Runs no LLM. A call survives only if its quote actually appears in the stored
transcript within ±`quote_window_seconds` of the timestamp the model claimed,
and only if its values pass strict validation. Rejected calls are kept with a
reason rather than deleted, so the rejection rate is measurable over time.
"""

from __future__ import annotations

from sqlmodel import Session, col, select

from ..config import Config
from ..enums import SkipReason, VerifyStatus, VideoState
from ..llm.schemas import RawCall
from ..logging import get_logger
from ..models import Call, Transcript, Video, utcnow
from ..verify.quotes import verify_quote
from .extract import segments_from_row
from .runner import StageResult, advance, claim_videos, skip

log = get_logger("pipeline.verify")


def _as_raw_call(call: Call) -> RawCall:
    """Rebuild the wire model from a stored row so the same validation path
    runs in the pipeline as in the dry-run."""
    return RawCall.model_construct(
        token_symbol=call.token_symbol,
        token_name=call.token_name,
        direction=str(call.direction),
        price_target=call.price_target,
        target_currency=call.target_currency,
        timeframe_text=call.timeframe_text,
        timeframe_days=call.timeframe_days,
        confidence=str(call.confidence),
        speaker=call.speaker,
        quote=call.quote,
        timestamp_seconds=float(call.timestamp_seconds),
        is_sponsored=call.is_sponsored,
        is_conditional=call.is_conditional,
    )


def verify(session: Session, cfg: Config) -> StageResult:
    """Verify every pending call on extracted videos."""
    result = StageResult(stage="verify")
    videos = claim_videos(session, VideoState.EXTRACTED, cfg.app.reliability.stage_batch_size)
    if not videos:
        return result

    verified_total = 0
    rejected_total = 0

    for video in videos:
        transcript = session.exec(select(Transcript).where(Transcript.video_pk == video.id)).first()
        segments = segments_from_row(transcript) if transcript else []

        calls = list(
            session.exec(
                select(Call)
                .where(Call.video_pk == video.id)
                .where(Call.verify_status == VerifyStatus.PENDING)
            ).all()
        )

        from ..llm.schemas import validate_call

        kept = 0
        for call in calls:
            validated, reason, detail = validate_call(_as_raw_call(call))
            if validated is None:
                call.verify_status = VerifyStatus.REJECTED
                call.verify_reason = f"{reason}: {detail}"[:500] if reason else detail
                call.verified_at = utcnow()
                session.add(call)
                rejected_total += 1
                continue

            verdict = verify_quote(validated.quote, validated.timestamp_seconds, segments, cfg)
            call.verify_score = verdict.score
            call.verified_at = utcnow()
            if verdict.ok:
                call.verify_status = VerifyStatus.VERIFIED
                call.verify_reason = None
                kept += 1
                verified_total += 1
            else:
                call.verify_status = VerifyStatus.REJECTED
                call.verify_reason = f"{verdict.reason}: {verdict.detail}"[:500]
                rejected_total += 1
                log.warning(
                    "call rejected",
                    extra={
                        "video_id": video.video_id,
                        "token": call.token_symbol,
                        "reason": str(verdict.reason),
                        "score": round(verdict.score, 1),
                    },
                )
            session.add(call)

        if kept == 0:
            # Nothing survived: there is nothing to enrich, cross-check or
            # alert, so the video ends here rather than walking the rest of
            # the pipeline to produce an empty alert.
            skip(session, video, SkipReason.NO_VERIFIED_CALLS)
            result.skipped += 1
        else:
            advance(session, video, VideoState.VERIFIED)
            result.processed += 1

        session.commit()

    result.notes["verified_calls"] = verified_total
    result.notes["rejected_calls"] = rejected_total
    return result


def verified_calls_for(session: Session, video: Video) -> list[Call]:
    return list(
        session.exec(
            select(Call)
            .where(Call.video_pk == video.id)
            .where(Call.verify_status == VerifyStatus.VERIFIED)
            .order_by(col(Call.timestamp_seconds))
        ).all()
    )


__all__ = ["verified_calls_for", "verify"]
