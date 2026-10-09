"""Full-pipeline sequencing.

Stages run in order inside one process and one file lock. A stage that throws
does not abort the run: later stages operate on different states and may still
have useful work to do, and the outbox flush at the end must happen regardless
so a transient extraction error cannot strand an already-queued alert.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session

from ..config import Config
from ..enums import OutboxKind, VerifyStatus
from ..logging import get_logger
from ..models import Call, Video, utcnow
from ..notify.format import render_backfill_summary
from ..notify.outbox import enqueue_committed, send_pending
from .alert import alert, finalise_queued
from .crosscheck import crosscheck
from .discover import discover
from .enrich import enrich
from .extract import extract
from .maintenance import daily_backup, daily_health, weekly_scorecard
from .mentions import mentions
from .report import daily_report, tracker_prices
from .runner import StageResult, record_stage
from .score import score
from .transcribe import transcribe
from .verify import verify

log = get_logger("pipeline.run")

StageCallable = Callable[[Session, Config], StageResult]

#: The happy path, in order.
STAGES: dict[str, StageCallable] = {
    "discover": discover,
    "transcribe": transcribe,
    "mentions": mentions,
    "extract": extract,
    "verify": verify,
    "enrich": enrich,
    "crosscheck": crosscheck,
    "alert": alert,
    "score": score,
    "tracker_prices": tracker_prices,
    "daily_report": daily_report,
    "daily_backup": daily_backup,
    "daily_health": daily_health,
    "weekly_scorecard": weekly_scorecard,
    "finalise": finalise_queued,
}


@dataclass
class RunReport:
    stages: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    outbox: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def run_pipeline(
    session: Session,
    cfg: Config,
    only: list[str] | None = None,
    send: bool = True,
) -> RunReport:
    """Run the pipeline once."""
    report = RunReport()
    selected = only or list(STAGES)

    for name in selected:
        stage_fn = STAGES.get(name)
        if stage_fn is None:
            report.errors.append(f"unknown stage: {name}")
            continue
        try:
            with record_stage(session, name) as run:
                result = stage_fn(session, cfg)
                run.processed = result.processed
                run.failed = result.failed
            report.stages.append(result.as_dict())
            log.info("stage complete", extra=result.as_dict())
        except Exception as exc:
            message = f"{name}: {type(exc).__name__}: {exc}"
            report.errors.append(message)
            log.exception("stage raised", extra={"stage": name})

    if send:
        # Always flush, even after a stage error: alerts already queued in a
        # committed transaction must not wait for the next tick.
        try:
            summary = send_pending(session, cfg)
            report.outbox = {
                "claimed": summary.claimed,
                "sent": summary.sent,
                "failed": summary.failed,
                "gave_up": summary.gave_up,
            }
        except Exception as exc:
            report.errors.append(f"outbox: {type(exc).__name__}: {exc}")
            log.exception("outbox send raised")

    return report


def run_backfill(session: Session, cfg: Config, days: int) -> RunReport:
    """Backfill history, then send a single summary message.

    Individual alerts are suppressed by the `is_backfill` flag on each video;
    announcing thirty days of history one video at a time would be unusable.
    """
    from sqlmodel import col, func, select

    report = RunReport()

    with record_stage(session, "backfill_discover") as run:
        result = discover(session, cfg, days=days, is_backfill=True)
        run.processed = result.processed
        run.failed = result.failed
    report.stages.append(result.as_dict())

    # Backfill can admit far more videos than one batch, so each stage is run
    # until it stops making progress.
    for name in ("transcribe", "mentions", "extract", "verify", "enrich", "crosscheck", "alert"):
        stage_fn = STAGES[name]
        while True:
            try:
                with record_stage(session, f"backfill_{name}") as run:
                    result = stage_fn(session, cfg)
                    run.processed = result.processed
                    run.failed = result.failed
            except Exception as exc:
                report.errors.append(f"{name}: {type(exc).__name__}: {exc}")
                log.exception("backfill stage raised", extra={"stage": name})
                break
            report.stages.append(result.as_dict())
            if result.processed == 0:
                break

    video_count = int(
        session.scalar(
            select(func.count()).select_from(Video).where(col(Video.is_backfill).is_(True))
        )
        or 0
    )
    call_count = int(
        session.scalar(
            select(func.count())
            .select_from(Call)
            .join(Video, col(Call.video_pk) == col(Video.id))
            .where(col(Video.is_backfill).is_(True))
            .where(Call.verify_status == VerifyStatus.VERIFIED)
        )
        or 0
    )

    enqueue_committed(
        session,
        dedupe_key=f"backfill_summary:{utcnow():%Y%m%d%H%M%S}",
        kind=OutboxKind.BACKFILL_SUMMARY,
        body=render_backfill_summary(video_count, call_count, days),
        cfg=cfg,
    )
    summary = send_pending(session, cfg)
    report.outbox = {"claimed": summary.claimed, "sent": summary.sent, "failed": summary.failed}
    return report


__all__ = ["STAGES", "RunReport", "run_backfill", "run_pipeline"]
