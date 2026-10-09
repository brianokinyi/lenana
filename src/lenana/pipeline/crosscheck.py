"""Cross-check stage: what did other channels say about the same coin?

Necessarily backward-looking at alert time — a call another channel makes two
hours from now does not exist yet. Rather than delay alerts (latency is the
point of the tool), the snapshot is computed now, labelled as of now, and
recomputed nightly once the full ±window has elapsed so the stored row and the
scorecard reflect the complete picture.
"""

from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session, col, select

from ..config import Config
from ..enums import Divergence, VerifyStatus, VideoState
from ..logging import get_logger
from ..models import Call, Channel, Crosscheck, Video, utcnow
from .runner import StageResult, advance, claim_videos

log = get_logger("pipeline.crosscheck")


def _peer_calls(
    session: Session, call: Call, video: Video, window: timedelta
) -> list[tuple[Call, str]]:
    """Verified calls on the same coin from *other* channels inside the window.

    Matching is on `coingecko_id` rather than the ticker so that two channels
    using different symbols for the same asset still line up.
    """
    if not call.coingecko_id:
        return []
    centre = call.entry_price_at or video.published_at

    rows = session.exec(
        select(Call, Channel.name, Channel.handle)
        .join(Video, col(Call.video_pk) == col(Video.id))
        .join(Channel, col(Video.channel_pk) == col(Channel.id))
        .where(Call.coingecko_id == call.coingecko_id)
        .where(Call.verify_status == VerifyStatus.VERIFIED)
        .where(col(Call.is_sponsored).is_(False))
        .where(Video.channel_pk != video.channel_pk)
        .where(col(Call.entry_price_at).is_not(None))
        .where(col(Call.entry_price_at) >= centre - window)
        .where(col(Call.entry_price_at) <= centre + window)
    ).all()
    return [(row[0], row[1] or row[2]) for row in rows]


def crosscheck(session: Session, cfg: Config) -> StageResult:
    """Compute a cross-check snapshot for each verified call."""
    result = StageResult(stage="crosscheck")
    videos = claim_videos(session, VideoState.ENRICHED, cfg.app.reliability.stage_batch_size)
    if not videos:
        return result

    window = timedelta(days=cfg.app.crosscheck.window_days)
    splits = 0

    for video in videos:
        calls = list(
            session.exec(
                select(Call)
                .where(Call.video_pk == video.id)
                .where(Call.verify_status == VerifyStatus.VERIFIED)
            ).all()
        )

        for call in calls:
            if call.id is None:  # pragma: no cover - persisted rows have ids
                continue
            peers = _peer_calls(session, call, video, window)

            # The call itself counts towards its own direction, so the numbers
            # in the alert read as "this many channels said up".
            bull = 1 if call.direction == "up" else 0
            bear = 1 if call.direction == "down" else 0
            channels: list[dict[str, str]] = []
            for peer, channel_name in peers:
                if peer.direction == "up":
                    bull += 1
                else:
                    bear += 1
                channels.append(
                    {
                        "channel": channel_name,
                        "direction": str(peer.direction),
                        "video_id": str(peer.video_pk),
                    }
                )

            if not peers:
                divergence = Divergence.SOLO
            elif bull > 0 and bear > 0:
                divergence = Divergence.SPLIT
                splits += 1
            else:
                divergence = Divergence.ALIGNED

            existing = session.exec(select(Crosscheck).where(Crosscheck.call_pk == call.id)).first()
            row = existing or Crosscheck(call_pk=call.id)
            row.bull_count = bull
            row.bear_count = bear
            row.channel_count = len({c["channel"] for c in channels}) + 1
            row.divergence = divergence
            row.channels = channels
            row.window_days = cfg.app.crosscheck.window_days
            if existing is not None:
                row.recomputed_at = utcnow()
            session.add(row)

        advance(session, video, VideoState.CROSSCHECKED)
        session.commit()
        result.processed += 1

    result.notes["splits"] = splits
    return result


def recompute_recent(session: Session, cfg: Config, days: int = 10) -> int:
    """Recompute cross-checks for recent calls, once the window has filled.

    Run nightly: the alert-time snapshot could only see the past, and this is
    what makes the stored data reflect the full ±window.
    """
    window = timedelta(days=cfg.app.crosscheck.window_days)
    since = utcnow() - timedelta(days=days)
    updated = 0

    rows = session.exec(
        select(Call, Video)
        .join(Video, col(Call.video_pk) == col(Video.id))
        .where(Call.verify_status == VerifyStatus.VERIFIED)
        .where(col(Call.entry_price_at).is_not(None))
        .where(col(Call.entry_price_at) >= since)
    ).all()

    for call, video in rows:
        if call.id is None:  # pragma: no cover - persisted rows have ids
            continue
        peers = _peer_calls(session, call, video, window)
        bull = 1 if call.direction == "up" else 0
        bear = 1 if call.direction == "down" else 0
        channels: list[dict[str, str]] = []
        for peer, channel_name in peers:
            if peer.direction == "up":
                bull += 1
            else:
                bear += 1
            channels.append(
                {
                    "channel": channel_name,
                    "direction": str(peer.direction),
                    "video_id": str(peer.video_pk),
                }
            )

        existing = session.exec(select(Crosscheck).where(Crosscheck.call_pk == call.id)).first()
        row = existing or Crosscheck(call_pk=call.id)
        row.bull_count = bull
        row.bear_count = bear
        row.channel_count = len({c["channel"] for c in channels}) + 1
        row.divergence = (
            Divergence.SOLO
            if not peers
            else (Divergence.SPLIT if bull > 0 and bear > 0 else Divergence.ALIGNED)
        )
        row.channels = channels
        row.recomputed_at = utcnow()
        session.add(row)
        updated += 1

    session.commit()
    log.info("crosschecks recomputed", extra={"count": updated})
    return updated


__all__ = ["crosscheck", "recompute_recent"]
