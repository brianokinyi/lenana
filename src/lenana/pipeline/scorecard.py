"""Weekly scorecard: who is actually right?

Ranks channels and speakers by hit rate, with a minimum sample size so a
channel with three lucky calls does not top the table. Also reports the most
discussed coins and month-to-date extraction cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlmodel import Session, col, func, select

from ..config import Config
from ..enums import Outcome, VerifyStatus
from ..logging import get_logger
from ..models import Call, CallScore, Channel, ExtractionRun, Video, utcnow
from ..notify.format import escape_html

log = get_logger("pipeline.scorecard")


@dataclass
class RankRow:
    name: str
    hits: int
    misses: int
    neutrals: int

    @property
    def scored(self) -> int:
        return self.hits + self.misses + self.neutrals

    @property
    def hit_rate(self) -> float:
        return (self.hits / self.scored * 100.0) if self.scored else 0.0


@dataclass
class Scorecard:
    channels: list[RankRow] = field(default_factory=list)
    speakers: list[RankRow] = field(default_factory=list)
    below_threshold: list[RankRow] = field(default_factory=list)
    top_coins: list[tuple[str, int]] = field(default_factory=list)
    month_cost_usd: float = 0.0
    calls_this_week: int = 0
    min_scored: int = 10


def _rank(rows: list[tuple[str, str | None, int]]) -> dict[str, RankRow]:
    """Aggregate (name, outcome, count) tuples into per-name tallies.

    Outcomes arrive as their stored string values, so they are parsed back into
    the enum rather than compared loosely.
    """
    ranked: dict[str, RankRow] = {}
    for name, raw_outcome, count in rows:
        label = name or "unknown"
        row = ranked.setdefault(label, RankRow(name=label, hits=0, misses=0, neutrals=0))
        try:
            outcome = Outcome(raw_outcome) if raw_outcome is not None else None
        except ValueError:
            continue
        if outcome is Outcome.HIT:
            row.hits += count
        elif outcome is Outcome.MISS:
            row.misses += count
        elif outcome is Outcome.NEUTRAL:
            row.neutrals += count
    return ranked


def build_scorecard(session: Session, cfg: Config) -> Scorecard:
    min_scored = cfg.app.scorecard.min_scored_for_rank
    card = Scorecard(min_scored=min_scored)

    channel_rows = session.exec(
        select(
            func.coalesce(Channel.name, Channel.handle),
            CallScore.outcome,
            func.count(),
        )
        .join(Call, col(CallScore.call_pk) == col(Call.id))
        .join(Video, col(Call.video_pk) == col(Video.id))
        .join(Channel, col(Video.channel_pk) == col(Channel.id))
        .where(col(CallScore.outcome).is_not(None))
        .group_by(func.coalesce(Channel.name, Channel.handle), col(CallScore.outcome))
    ).all()
    ranked_channels = _rank([(str(r[0]), r[1], int(r[2])) for r in channel_rows])

    card.channels = sorted(
        (r for r in ranked_channels.values() if r.scored >= min_scored),
        key=lambda r: (-r.hit_rate, -r.scored),
    )
    card.below_threshold = sorted(
        (r for r in ranked_channels.values() if r.scored < min_scored),
        key=lambda r: -r.scored,
    )

    speaker_rows = session.exec(
        select(Call.speaker, CallScore.outcome, func.count())
        .join(CallScore, col(CallScore.call_pk) == col(Call.id))
        .where(col(CallScore.outcome).is_not(None))
        .group_by(col(Call.speaker), col(CallScore.outcome))
    ).all()
    card.speakers = sorted(
        (
            r
            for r in _rank([(str(s[0]), s[1], int(s[2])) for s in speaker_rows]).values()
            if r.scored >= min_scored
        ),
        key=lambda r: (-r.hit_rate, -r.scored),
    )

    week_ago = utcnow() - timedelta(days=7)
    coin_rows = session.exec(
        select(Call.token_symbol, func.count())
        .where(Call.verify_status == VerifyStatus.VERIFIED)
        .where(col(Call.created_at) >= week_ago)
        .group_by(col(Call.token_symbol))
        .order_by(func.count().desc())
        .limit(10)
    ).all()
    card.top_coins = [(str(r[0]), int(r[1])) for r in coin_rows]
    card.calls_this_week = sum(count for _, count in card.top_coins)

    month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    card.month_cost_usd = float(
        session.scalar(
            select(func.coalesce(func.sum(ExtractionRun.cost_usd), 0.0)).where(
                col(ExtractionRun.created_at) >= month_start
            )
        )
        or 0.0
    )
    return card


def render_scorecard(card: Scorecard) -> str:
    lines = ["📋 <b>WEEKLY SCORECARD</b>", ""]

    lines.append(f"<b>Channels</b> (min {card.min_scored} scored calls)")
    if card.channels:
        for index, row in enumerate(card.channels, start=1):
            lines.append(
                f"   {index}. {escape_html(row.name)} — {row.hit_rate:.0f}% "
                f"({row.hits}H/{row.misses}M/{row.neutrals}N, n={row.scored})"
            )
    else:
        lines.append("   not enough scored calls yet")
    if card.below_threshold:
        pending = ", ".join(
            f"{escape_html(r.name)} (n={r.scored})" for r in card.below_threshold[:6]
        )
        lines.append(f"   <i>building history: {pending}</i>")

    lines.extend(["", f"<b>Speakers</b> (min {card.min_scored} scored calls)"])
    if card.speakers:
        for index, row in enumerate(card.speakers, start=1):
            lines.append(
                f"   {index}. {escape_html(row.name)} — {row.hit_rate:.0f}% (n={row.scored})"
            )
    else:
        lines.append("   not enough scored calls yet")

    lines.extend(["", "<b>Most-called coins this week</b>"])
    if card.top_coins:
        lines.extend(f"   {escape_html(symbol)} x{count}" for symbol, count in card.top_coins)
    else:
        lines.append("   no verified calls this week")

    lines.extend(["", f"💰 Claude cost month-to-date: ${card.month_cost_usd:.2f}"])
    return "\n".join(lines)


__all__ = ["RankRow", "Scorecard", "build_scorecard", "render_scorecard"]
