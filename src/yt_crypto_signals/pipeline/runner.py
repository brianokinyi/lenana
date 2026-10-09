"""Stage framework: batching, state transitions, retry bookkeeping.

Every stage follows the same contract, which is what makes the pipeline
crash-safe:

* It selects only rows in **its own input state**, so stages are
  order-independent and can never consume their own output.
* It commits **per batch**, so an unhandled error leaves that batch's rows in
  their original state to be retried on the next tick — nothing is left
  half-processed.
* It records a `StageRun` row either way, which is what the health stage reads
  to decide whether something is persistently broken.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlmodel import Session, col, select

from ..config import Config
from ..enums import FailureKind, SkipReason, VideoState
from ..logging import get_logger
from ..models import StageRun, Video, utcnow
from ..utils import backoff_delay

log = get_logger("pipeline")


@dataclass
class StageResult:
    stage: str
    processed: int = 0
    failed: int = 0
    skipped: int = 0
    notes: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "processed": self.processed,
            "failed": self.failed,
            "skipped": self.skipped,
            **self.notes,
        }


@contextmanager
def record_stage(session: Session, stage: str) -> Iterator[StageRun]:
    """Record a stage execution, successful or not.

    The row is committed before the stage body runs so that a hard crash (OOM,
    SIGKILL) still leaves evidence the stage started.
    """
    run = StageRun(stage=stage, started_at=utcnow())
    session.add(run)
    session.commit()
    try:
        yield run
        run.ok = True
    except Exception as exc:
        run.ok = False
        run.error = f"{type(exc).__name__}: {exc}"[:1000]
        log.exception("stage failed", extra={"stage": stage})
        raise
    finally:
        run.finished_at = utcnow()
        session.add(run)
        session.commit()


def claim_videos(
    session: Session,
    state: VideoState,
    limit: int,
    respect_retry: bool = True,
) -> list[Video]:
    """Videos in `state` that are due for processing, oldest first.

    `respect_retry` honours `next_retry_at`, which is how both the transcript
    backoff ladder and the 2-hour livestream settle delay are enforced without
    a dedicated state for each.
    """
    query = select(Video).where(Video.state == state)
    if respect_retry:
        now = utcnow()
        query = query.where(
            (col(Video.next_retry_at).is_(None)) | (col(Video.next_retry_at) <= now)
        )
    return list(session.exec(query.order_by(col(Video.published_at)).limit(limit)).all())


def advance(session: Session, video: Video, state: VideoState) -> None:
    """Move a video to its next state and clear transient error bookkeeping."""
    video.state = state
    video.updated_at = utcnow()
    video.last_error = None
    video.last_error_kind = None
    video.next_retry_at = None
    session.add(video)


def skip(session: Session, video: Video, reason: SkipReason) -> None:
    video.state = VideoState.SKIPPED
    video.skip_reason = reason.value
    video.updated_at = utcnow()
    video.next_retry_at = None
    session.add(video)
    log.info("video skipped", extra={"video_id": video.video_id, "reason": reason.value})


def defer(session: Session, video: Video, until_delta: timedelta, note: str) -> None:
    """Leave a video in its current state but not due until later.

    Used for livestreams whose captions have not settled yet; a separate state
    would add a transition without adding information.
    """
    video.next_retry_at = utcnow() + until_delta
    video.last_error = note
    video.updated_at = utcnow()
    session.add(video)


def record_failure(
    session: Session,
    video: Video,
    kind: FailureKind,
    message: str,
    cfg: Config,
) -> bool:
    """Apply the retry ladder after a transcript failure.

    Returns True when the video will be retried, False when it has been
    condemned to `permanent_failed` (which the health stage then reports).
    """
    video.attempts += 1
    video.last_error = message[:1000]
    video.last_error_kind = kind.value
    video.updated_at = utcnow()

    if not kind.is_retryable:
        reason = (
            SkipReason.NO_CAPTIONS if kind is FailureKind.NO_CAPTIONS else SkipReason.UNAVAILABLE
        )
        skip(session, video, reason)
        return False

    delay = backoff_delay(cfg.app.reliability.transcript_retry_ladder_minutes, video.attempts)
    if delay is None:
        video.state = VideoState.PERMANENT_FAILED
        video.next_retry_at = None
        session.add(video)
        log.error(
            "video permanently failed",
            extra={
                "video_id": video.video_id,
                "attempts": video.attempts,
                "kind": kind.value,
                "error": message[:200],
            },
        )
        return False

    video.state = VideoState.TRANSCRIPT_FAILED
    video.next_retry_at = utcnow() + delay
    session.add(video)
    log.warning(
        "transcript failed, will retry",
        extra={
            "video_id": video.video_id,
            "attempts": video.attempts,
            "kind": kind.value,
            "retry_in_minutes": int(delay.total_seconds() // 60),
        },
    )
    return True


def requeue_failed(session: Session, cfg: Config, limit: int) -> list[Video]:
    """Transcript-failed videos whose backoff has elapsed.

    They are moved back to `discovered` so the transcribe stage picks them up
    through its normal query, rather than the stage needing to know about two
    input states.
    """
    due = claim_videos(session, VideoState.TRANSCRIPT_FAILED, limit, respect_retry=True)
    for video in due:
        video.state = VideoState.DISCOVERED
        video.updated_at = utcnow()
        session.add(video)
    if due:
        session.commit()
        log.info("requeued transcript failures", extra={"count": len(due)})
    return due


def batched(items: list[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


StageFn = Callable[[Session, Config], StageResult]

__all__ = [
    "StageFn",
    "StageResult",
    "advance",
    "batched",
    "claim_videos",
    "defer",
    "record_failure",
    "record_stage",
    "requeue_failed",
    "skip",
]
