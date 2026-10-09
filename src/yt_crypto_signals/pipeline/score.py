"""Score stage: compare price now against price when the call was made.

Rules:

* A call is due once `timeframe_days` has elapsed from when it was spoken
  (default 30 days when no timeframe was given).
* **Hit** = price moved the predicted way by more than the threshold (5%).
  **Miss** = moved the opposite way by more than the threshold.
  **Neutral** = inside the band either way, so the call was not wrong but did
  not pay either.
* `price_at_eval` is a **point price** at the due time, not a window average.
  With a ±5% band a single wick rarely flips the verdict, and a point price is
  reproducible against a chart; an average would not be.
* `target_touched`, `window_high` and `window_low` come from **4-hour OHLC
  candles** with true highs and lows, so an intraday touch is detected.

Raw calls are never modified; results live in `call_score`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import Session, col, select

from ..config import Config
from ..enums import Direction, Outcome, VerifyStatus
from ..logging import get_logger
from ..models import Call, CallScore, utcnow
from ..prices.coingecko import CoinGecko
from ..utils import pct_change
from .runner import StageResult

log = get_logger("pipeline.score")


def due_at_for(call: Call, cfg: Config) -> datetime | None:
    from datetime import datetime  # noqa: F401  (annotation only)

    if call.entry_price_at is None:
        return None
    days = call.timeframe_days or cfg.app.scoring.default_timeframe_days
    return call.entry_price_at + timedelta(days=float(days))


def classify(direction: Direction, change_pct: float, threshold_pct: float) -> Outcome:
    """Turn a percentage move into an outcome."""
    if abs(change_pct) <= threshold_pct:
        return Outcome.NEUTRAL
    moved_up = change_pct > 0
    predicted_up = direction == Direction.UP
    return Outcome.HIT if moved_up == predicted_up else Outcome.MISS


def target_was_touched(
    direction: Direction, target: float, window_high: float, window_low: float
) -> bool:
    """Whether a price target was reached at any point in the window.

    Direction decides which side of the candle matters: an up call is touched
    by the window high, a down call by the window low. Using the close would
    miss a target that was hit intraday and given back.
    """
    if direction is Direction.UP:
        return window_high >= target
    return window_low <= target


def ensure_scores_scheduled(session: Session, cfg: Config) -> int:
    """Create a pending `call_score` row for every enriched, verified call.

    Scheduling and scoring are separate so `due_at` is fixed at enrichment
    time and cannot drift if the default timeframe is later reconfigured.
    """
    created = 0
    calls = session.exec(
        select(Call)
        .where(Call.verify_status == VerifyStatus.VERIFIED)
        .where(col(Call.entry_price_at).is_not(None))
        .where(col(Call.coingecko_id).is_not(None))
    ).all()

    for call in calls:
        existing = session.exec(select(CallScore).where(CallScore.call_pk == call.id)).first()
        if existing is not None:
            continue
        due = due_at_for(call, cfg)
        if due is None or call.id is None:
            continue
        session.add(CallScore(call_pk=call.id, due_at=due))
        created += 1

    if created:
        session.commit()
    return created


def score(session: Session, cfg: Config, client: CoinGecko | None = None) -> StageResult:
    """Score every call whose timeframe has elapsed."""
    result = StageResult(stage="score")
    result.notes["scheduled"] = ensure_scores_scheduled(session, cfg)

    now = utcnow()
    pending = list(
        session.exec(
            select(CallScore, Call)
            .join(Call, col(CallScore.call_pk) == col(Call.id))
            .where(col(CallScore.scored_at).is_(None))
            .where(col(CallScore.due_at) <= now)
            .order_by(col(CallScore.due_at))
            .limit(200)
        ).all()
    )
    if not pending:
        return result

    client = client or CoinGecko(cfg)
    threshold = cfg.app.scoring.hit_threshold_pct
    outcomes: dict[str, int] = {}

    for row, call in pending:
        if not call.coingecko_id or not call.entry_price_usd or not call.entry_price_at:
            # Cannot be scored; leave unscored rather than guessing.
            continue

        price_then = client.price_at(session, call.coingecko_id, row.due_at)
        if price_then is None:
            log.warning(
                "no price at due time; will retry next run",
                extra={"call_id": call.id, "coin": call.coingecko_id},
            )
            continue

        change = pct_change(call.entry_price_usd, price_then)
        if change is None:
            continue

        outcome = classify(Direction(call.direction), change, threshold)
        row.price_at_eval = price_then
        row.pct_change = change
        row.outcome = outcome
        row.scored_at = utcnow()
        row.price_source = "market_chart_range"
        row.granularity = "hourly"

        window = client.ohlc_window(call.coingecko_id, call.entry_price_at, row.due_at)
        if window is not None:
            row.window_high, row.window_low = window
            if call.price_target is not None:
                row.target_touched = target_was_touched(
                    Direction(call.direction),
                    call.price_target,
                    row.window_high,
                    row.window_low,
                )

        session.add(row)
        session.commit()
        result.processed += 1
        outcomes[str(outcome)] = outcomes.get(str(outcome), 0) + 1

    result.notes.update(outcomes)
    result.notes["coingecko_calls"] = client.calls_made
    return result


__all__ = [
    "classify",
    "due_at_for",
    "ensure_scores_scheduled",
    "score",
    "target_was_touched",
]
