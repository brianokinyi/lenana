"""Enrich stage: map tickers to coins and record the entry price.

The entry price is taken at **publish time plus the call's timestamp**, i.e.
when the words were actually spoken. On a three-hour stream, attributing a
call made at 2:45:00 to the publish timestamp would score it against a price
from three hours earlier — in crypto that is a materially different number.

This stage also runs a **target plausibility check**. Auto-captions mangle
numbers ("nine hundred" becoming "$900,000"), and a mangled target survives
quote verification because the quote genuinely matches the transcript. If a
target is wildly out of line with the live price, the target is dropped and
the direction kept: a call with a correct direction and no target is useful,
while an absurd target would be scored against a number nobody said.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import Session, select

from ..config import Config
from ..enums import MappingStatus, VerifyStatus, VideoState
from ..logging import get_logger
from ..models import Call, Video
from ..prices.coingecko import CoinGecko, resolve_symbol
from .runner import StageResult, advance, claim_videos

log = get_logger("pipeline.enrich")

#: A target more than 50x above or below the price when the call was made is
#: treated as a transcription artefact rather than a prediction.
PLAUSIBLE_TARGET_RATIO = 50.0


def entry_time_for(video: Video, call: Call, cfg: Config) -> datetime:
    """When the call was spoken."""
    from datetime import datetime  # noqa: F401  (annotation only)

    if cfg.app.enrichment.entry_price_at_call_timestamp:
        return video.published_at + timedelta(seconds=call.timestamp_seconds)
    return video.published_at


def target_is_plausible(target: float, entry_price: float) -> bool:
    if entry_price <= 0 or target <= 0:
        return False
    ratio = target / entry_price
    return 1 / PLAUSIBLE_TARGET_RATIO <= ratio <= PLAUSIBLE_TARGET_RATIO


def enrich(session: Session, cfg: Config, client: CoinGecko | None = None) -> StageResult:
    """Map symbols to CoinGecko ids and record entry prices."""
    result = StageResult(stage="enrich")
    videos = claim_videos(session, VideoState.VERIFIED, cfg.app.reliability.stage_batch_size)
    if not videos:
        return result

    client = client or CoinGecko(cfg)
    if client.coin_list_is_stale(session):
        client.refresh_coin_list(session)

    mapped = ambiguous = unmapped = dropped_targets = 0

    for video in videos:
        calls = list(
            session.exec(
                select(Call)
                .where(Call.video_pk == video.id)
                .where(Call.verify_status == VerifyStatus.VERIFIED)
            ).all()
        )

        for call in calls:
            coin_id, status = resolve_symbol(session, call.token_symbol)
            call.coingecko_id = coin_id
            call.mapping_status = MappingStatus(status)

            if status == "unmapped" or coin_id is None:
                call.mapping_note = f"no coin found for symbol {call.token_symbol}"
                unmapped += 1
                session.add(call)
                continue
            if status == "ambiguous":
                call.mapping_note = (
                    f"symbol {call.token_symbol} matches several coins; "
                    f"chose the highest market cap"
                )
                ambiguous += 1
            else:
                mapped += 1

            entry_at = entry_time_for(video, call, cfg)
            call.entry_price_at = entry_at
            call.entry_price_usd = client.price_at(session, coin_id, entry_at)

            if (
                call.entry_price_usd
                and call.price_target
                and not target_is_plausible(call.price_target, call.entry_price_usd)
            ):
                log.warning(
                    "implausible target dropped",
                    extra={
                        "video_id": video.video_id,
                        "token": call.token_symbol,
                        "target": call.price_target,
                        "entry_price": call.entry_price_usd,
                    },
                )
                note = (
                    f"target {call.price_target:g} dropped: implausible against "
                    f"entry price {call.entry_price_usd:g} (likely a caption artefact)"
                )
                call.mapping_note = f"{call.mapping_note + '; ' if call.mapping_note else ''}{note}"
                call.price_target = None
                dropped_targets += 1

            session.add(call)

        advance(session, video, VideoState.ENRICHED)
        session.commit()
        result.processed += 1

    result.notes.update(
        {
            "mapped": mapped,
            "ambiguous": ambiguous,
            "unmapped": unmapped,
            "targets_dropped": dropped_targets,
            "coingecko_calls": client.calls_made,
        }
    )
    return result


__all__ = ["PLAUSIBLE_TARGET_RATIO", "enrich", "entry_time_for", "target_is_plausible"]
