"""Single-video processing, used by `process --video ... --dry-run`.

This is the inspection path: it runs the full extract-then-verify chain on one
video and reports everything, including the calls that were thrown away and
why. Rejections are the interesting half — they are how you tell a prompt
problem from a verification problem.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import Config
from ..enums import RejectReason
from ..llm.client import ClaudeExtractor
from ..llm.extract import ExtractionResult, VideoContext, extract_from_segments
from ..llm.schemas import RawCall, ValidatedCall, validate_call
from ..logging import get_logger
from ..sources.transcripts.base import FetchedTranscriptData, TranscriptSegment
from ..sources.transcripts.registry import fetch_transcript
from ..sources.youtube_api import VideoInfo, YouTubeApi
from ..utils import fmt_timestamp
from ..verify.quotes import QuoteVerdict, locate_quote, verify_quote

log = get_logger("pipeline.process")


@dataclass
class VerifiedCall:
    call: ValidatedCall
    verdict: QuoteVerdict

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.call.model_dump(mode="json"),
            "timestamp": fmt_timestamp(self.call.timestamp_seconds),
            "verify_score": round(self.verdict.score, 1),
        }


@dataclass
class RejectedCall:
    raw: RawCall
    reason: RejectReason
    detail: str | None
    score: float | None = None
    #: Where the quote best matches anywhere in the transcript. Distinguishes
    #: "the model moved a real quote" from "the model invented a quote".
    best_match_at: float | None = None
    best_match_score: float | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "token_symbol": self.raw.token_symbol,
            "direction": self.raw.direction,
            "speaker": self.raw.speaker,
            "quote": self.raw.quote,
            "claimed_timestamp": fmt_timestamp(int(self.raw.timestamp_seconds)),
            "reason": str(self.reason),
            "detail": self.detail,
        }
        if self.score is not None:
            out["score"] = round(self.score, 1)
        if self.best_match_score is not None:
            out["best_match_elsewhere"] = {
                "at": fmt_timestamp(int(self.best_match_at or 0)),
                "score": round(self.best_match_score, 1),
            }
        return out


@dataclass
class VerificationOutcome:
    verified: list[VerifiedCall] = field(default_factory=list)
    rejected: list[RejectedCall] = field(default_factory=list)

    @property
    def alertable(self) -> list[VerifiedCall]:
        """Verified, non-sponsored calls — the only ones worth alerting on."""
        return [v for v in self.verified if not v.call.is_sponsored]


def verify_calls(
    calls: list[RawCall],
    segments: list[TranscriptSegment],
    cfg: Config,
    diagnose: bool = False,
) -> VerificationOutcome:
    """Apply strict validation and quote verification to extracted calls.

    `diagnose` additionally searches the whole transcript for a rejected
    quote, which is slower but makes a dry-run report far more useful.
    """
    outcome = VerificationOutcome()

    for raw in calls:
        validated, reason, detail = validate_call(raw)
        if validated is None:
            assert reason is not None
            outcome.rejected.append(RejectedCall(raw=raw, reason=reason, detail=detail))
            continue

        verdict = verify_quote(validated.quote, validated.timestamp_seconds, segments, cfg)
        if verdict.ok:
            outcome.verified.append(VerifiedCall(call=validated, verdict=verdict))
            continue

        rejected = RejectedCall(
            raw=raw,
            reason=verdict.reason or RejectReason.QUOTE_NOT_FOUND,
            detail=verdict.detail,
            score=verdict.score,
        )
        if diagnose:
            located = locate_quote(validated.quote, segments, cfg)
            if located:
                rejected.best_match_at, rejected.best_match_score = located
        outcome.rejected.append(rejected)

    log.info(
        "verification complete",
        extra={"verified": len(outcome.verified), "rejected": len(outcome.rejected)},
    )
    return outcome


@dataclass
class DryRunReport:
    video: dict[str, Any]
    transcript: dict[str, Any]
    extraction: dict[str, Any]
    verified_calls: list[dict[str, Any]]
    rejected_calls: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def process_video(
    video_id: str,
    cfg: Config,
    channel_name: str | None = None,
    default_host: str = "Host",
    extractor: ClaudeExtractor | None = None,
) -> DryRunReport:
    """Fetch, extract and verify one video. Stores nothing, sends nothing."""
    api = YouTubeApi(cfg)
    info: VideoInfo | None = api.get_video(video_id)
    if info is None:
        raise ValueError(f"video {video_id} not found via the YouTube Data API")

    if info.is_live_now:
        raise ValueError(
            f"video {video_id} is still live; captions are provisional until the stream ends"
        )

    outcome = fetch_transcript(video_id, cfg)
    if not outcome.ok or outcome.data is None:
        failure = outcome.failure
        raise RuntimeError(
            f"no transcript available for {video_id}: "
            f"{failure.kind if failure else 'unknown'} - {failure.message if failure else ''}"
        )
    transcript: FetchedTranscriptData = outcome.data

    # Resolve the channel name for the prompt, falling back to the id so the
    # prompt is never missing context.
    resolved_channel = channel_name
    if resolved_channel is None:
        for channel_cfg in cfg.app.enabled_channels:
            resolved_channel = channel_cfg.handle
            default_host = channel_cfg.default_host
            break
        resolved_channel = resolved_channel or info.channel_id

    context = VideoContext(
        video_id=video_id,
        title=info.title,
        channel_name=resolved_channel,
        published_at=info.published_at,
        default_host=default_host,
    )

    result: ExtractionResult = extract_from_segments(
        transcript.segments, context, cfg, extractor=extractor
    )
    verification = verify_calls(result.calls, transcript.segments, cfg, diagnose=True)

    return DryRunReport(
        video={
            "video_id": video_id,
            "title": info.title,
            "channel": resolved_channel,
            "published_at": info.published_at.isoformat(),
            "duration_seconds": info.duration_seconds,
            "is_livestream": info.is_livestream,
            "url": f"https://www.youtube.com/watch?v={video_id}",
        },
        transcript={
            "provider": str(transcript.provider),
            "language": transcript.language_code,
            "is_generated": transcript.is_generated,
            "segments": len(transcript.segments),
            "characters": len(transcript.full_text),
        },
        extraction={
            "model": result.model,
            "prompt_version": result.prompt_version,
            "chunks": result.chunk_count,
            "raw_calls": result.raw_call_count,
            "after_merge": len(result.calls),
            "duplicates_merged": result.merged_away,
            "market_sentiment": str(result.market_sentiment),
            "summary": result.summary,
            "usage": result.usage.as_dict(),
            "verified": len(verification.verified),
            "rejected": len(verification.rejected),
            "alertable_non_sponsored": len(verification.alertable),
        },
        verified_calls=[v.as_dict() for v in verification.verified],
        rejected_calls=[r.as_dict() for r in verification.rejected],
    )


__all__ = [
    "DryRunReport",
    "RejectedCall",
    "VerificationOutcome",
    "VerifiedCall",
    "process_video",
    "verify_calls",
]
