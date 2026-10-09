"""Alert stage: queue Telegram messages for crosschecked videos.

The enqueue and the state change happen in **one transaction**, which is the
whole point of the outbox: there is no instant at which a video is marked
alerted but no message exists, or a message exists for a video that was not.

Backfilled videos are never alerted individually — thirty days of history
would arrive as a wall of notifications. The backfill command sends one
summary instead.
"""

from __future__ import annotations

from sqlmodel import Session, col, func, select

from ..config import Config
from ..enums import OutboxKind, Outcome, VerifyStatus, VideoState
from ..logging import get_logger
from ..models import Call, CallScore, Channel, Crosscheck, ExtractionRun, Video
from ..notify.format import AlertCall, AlertCrosscheck, VideoAlert, render_video_alert
from ..notify.outbox import enqueue
from .runner import StageResult, advance, claim_videos

log = get_logger("pipeline.alert")


def channel_hit_rate(session: Session, channel_pk: int) -> tuple[float | None, int]:
    """(hit rate %, number of scored calls) for a channel.

    Neutral outcomes are excluded from the numerator but counted in the
    denominator, so a channel that is merely vague does not score as accurate.
    """
    rows = session.exec(
        select(CallScore.outcome, func.count())
        .join(Call, col(CallScore.call_pk) == col(Call.id))
        .join(Video, col(Call.video_pk) == col(Video.id))
        .where(Video.channel_pk == channel_pk)
        .where(col(CallScore.outcome).is_not(None))
        .group_by(col(CallScore.outcome))
    ).all()

    counts = {outcome: count for outcome, count in rows}
    total = sum(counts.values())
    if total == 0:
        return None, 0
    hits = counts.get(Outcome.HIT, 0)
    return hits / total * 100.0, total


def build_alert(session: Session, video: Video, cfg: Config) -> VideoAlert | None:
    """Assemble the alert for a video, or None when there is nothing to say."""
    calls = list(
        session.exec(
            select(Call)
            .where(Call.video_pk == video.id)
            .where(Call.verify_status == VerifyStatus.VERIFIED)
            .where(col(Call.is_sponsored).is_(False))
            .order_by(col(Call.timestamp_seconds))
        ).all()
    )
    if not calls:
        return None

    channel = session.get(Channel, video.channel_pk)
    channel_name = (channel.name or channel.handle) if channel else "unknown channel"

    alert_calls = [
        AlertCall(
            token_symbol=c.token_symbol,
            direction=c.direction,
            price_target=c.price_target,
            target_currency=c.target_currency,
            timeframe_text=c.timeframe_text,
            confidence=c.confidence,
            speaker=c.speaker,
            quote=c.quote,
            timestamp_seconds=c.timestamp_seconds,
            is_conditional=c.is_conditional,
        )
        for c in calls
    ]

    # One cross-check line per coin, not per call: two calls on the same coin
    # in one video would otherwise print the same line twice.
    crosschecks: list[AlertCrosscheck] = []
    seen_symbols: set[str] = set()
    for call in calls:
        if call.token_symbol in seen_symbols:
            continue
        row = session.exec(select(Crosscheck).where(Crosscheck.call_pk == call.id)).first()
        if row is None or not row.channels:
            continue
        seen_symbols.add(call.token_symbol)
        crosschecks.append(
            AlertCrosscheck(
                token_symbol=call.token_symbol,
                bull_count=row.bull_count,
                bear_count=row.bear_count,
                channels=sorted(
                    {str(c.get("channel", "")) for c in row.channels if c.get("channel")}
                ),
                divergence=row.divergence,
            )
        )

    hit_rate, scored = channel_hit_rate(session, video.channel_pk)

    return VideoAlert(
        channel_name=channel_name,
        title=video.title,
        published_at=video.published_at,
        duration_seconds=video.duration_seconds,
        video_url=video.url,
        calls=alert_calls,
        crosschecks=crosschecks,
        hit_rate_pct=hit_rate,
        scored_count=scored,
        crosscheck_window_days=cfg.app.crosscheck.window_days,
        min_scored_for_rank=cfg.app.scorecard.min_scored_for_rank,
    )


def alert(session: Session, cfg: Config) -> StageResult:
    """Queue alerts for crosschecked videos."""
    result = StageResult(stage="alert")
    videos = claim_videos(session, VideoState.CROSSCHECKED, cfg.app.reliability.stage_batch_size)
    if not videos:
        return result

    for video in videos:
        if video.is_backfill:
            # Processed and stored, but not announced; the backfill summary
            # covers these in one message.
            advance(session, video, VideoState.DONE)
            session.commit()
            result.skipped += 1
            continue

        built = build_alert(session, video, cfg)
        if built is None:
            advance(session, video, VideoState.DONE)
            session.commit()
            result.skipped += 1
            continue

        run = session.exec(
            select(ExtractionRun)
            .where(ExtractionRun.video_pk == video.id)
            .where(col(ExtractionRun.is_active).is_(True))
        ).first()
        run_id = run.id if run else 0

        body = render_video_alert(built, cfg.settings.tz)

        # Single transaction: enqueue + state change commit together.
        enqueue(
            session,
            dedupe_key=f"video_alert:{video.video_id}:{run_id}",
            kind=OutboxKind.VIDEO_ALERT,
            body=body,
            cfg=cfg,
        )
        advance(session, video, VideoState.ALERT_QUEUED)
        session.commit()
        result.processed += 1
        log.info(
            "alert queued",
            extra={"video_id": video.video_id, "calls": len(built.calls)},
        )

    return result


def finalise_queued(session: Session, cfg: Config) -> StageResult:
    """Move alert_queued videos to done once their outbox rows are gone.

    Separate from sending so that a Telegram outage cannot strand videos: they
    stay in `alert_queued` until their message actually leaves.
    """
    from ..enums import OutboxState
    from ..models import Outbox

    result = StageResult(stage="finalise")
    videos = claim_videos(session, VideoState.ALERT_QUEUED, 100)

    for video in videos:
        prefix = f"video_alert:{video.video_id}:"
        pending = session.exec(
            select(func.count())
            .select_from(Outbox)
            .where(col(Outbox.dedupe_key).startswith(prefix))
            .where(Outbox.state == OutboxState.PENDING)
        ).one()
        if pending:
            continue
        advance(session, video, VideoState.DONE)
        result.processed += 1

    session.commit()
    return result


__all__ = ["alert", "build_alert", "channel_hit_rate", "finalise_queued"]
