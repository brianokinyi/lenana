"""Telegram message rendering. Pure functions, no I/O.

Kept free of database and network concerns because this is where the fiddly
edge cases live — HTML escaping and the 4096-character limit — and those are
worth testing directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from ..enums import Confidence, Direction, Divergence
from ..utils import fmt_duration, fmt_price, fmt_timestamp, to_display, truncate

#: Telegram's hard limit for a single message.
TELEGRAM_MAX_CHARS = 4096

#: Quotes are trimmed so one verbose call cannot push a whole alert over the
#: limit on its own.
MAX_QUOTE_CHARS = 320

_DIRECTION_EMOJI = {Direction.UP: "🟢", Direction.DOWN: "🔴"}
_DIRECTION_LABEL = {Direction.UP: "UP", Direction.DOWN: "DOWN"}


def escape_html(text: str) -> str:
    """Escape the three characters Telegram's HTML parse mode reserves.

    Order matters: `&` must be replaced first, or the escapes introduced for
    `<` and `>` would themselves be escaped.
    """
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass
class AlertCall:
    token_symbol: str
    direction: Direction
    price_target: float | None
    target_currency: str | None
    timeframe_text: str | None
    confidence: Confidence
    speaker: str
    quote: str
    timestamp_seconds: int
    is_conditional: bool = False


@dataclass
class AlertCrosscheck:
    token_symbol: str
    bull_count: int
    bear_count: int
    channels: list[str]
    divergence: Divergence


@dataclass
class VideoAlert:
    channel_name: str
    title: str
    published_at: datetime
    duration_seconds: int | None
    video_url: str
    calls: list[AlertCall]
    crosschecks: list[AlertCrosscheck] = field(default_factory=list)
    hit_rate_pct: float | None = None
    scored_count: int = 0
    crosscheck_window_days: int = 5
    min_scored_for_rank: int = 10


def _call_block(call: AlertCall, video_url: str) -> str:
    emoji = _DIRECTION_EMOJI.get(call.direction, "⚪")
    label = _DIRECTION_LABEL.get(call.direction, str(call.direction).upper())
    marker = " 🔸" if call.is_conditional else ""

    target = fmt_price(call.price_target, call.target_currency)
    timeframe = escape_html(call.timeframe_text) if call.timeframe_text else "none given"
    quote = escape_html(truncate(call.quote.strip(), MAX_QUOTE_CHARS))
    stamp = fmt_timestamp(call.timestamp_seconds)
    deep_link = f"{video_url}&t={int(call.timestamp_seconds)}s"

    return (
        f"{emoji} <b>{escape_html(call.token_symbol)}</b> — {label}{marker}\n"
        f"   Target: {escape_html(target)} · Timeframe: {timeframe} · "
        f"Confidence: {call.confidence}\n"
        f"   Speaker: {escape_html(call.speaker)}\n"
        f'   "<i>{quote}</i>" <a href="{deep_link}">[{stamp}]</a>'
    )


def _crosscheck_block(alert: VideoAlert) -> str:
    if not alert.crosschecks:
        return ""
    channel_count = len({c for x in alert.crosschecks for c in x.channels})
    lines = [
        f"🔀 <b>CROSS-CHECK</b> (±{alert.crosscheck_window_days} days, {channel_count} channels)"
    ]
    for x in alert.crosschecks:
        names = ", ".join(escape_html(c) for c in x.channels) or "no other channels"
        warning = " ⚠️ split" if x.divergence.is_split else ""
        lines.append(
            f"   {escape_html(x.token_symbol)} → {x.bull_count} 🟢 / {x.bear_count} 🔴 "
            f"({names}){warning}"
        )
    return "\n".join(lines)


def _track_record_line(alert: VideoAlert) -> str:
    if alert.scored_count < alert.min_scored_for_rank or alert.hit_rate_pct is None:
        return f"📊 Channel track record: not enough data (n={alert.scored_count})"
    return f"📊 Channel track record: {alert.hit_rate_pct:.0f}% (n={alert.scored_count})"


def render_video_alert(alert: VideoAlert, tz: ZoneInfo) -> str:
    """Render the alert for one video."""
    published = to_display(alert.published_at, tz)
    header = (
        f"📡 <b>NEW CALLS — {escape_html(alert.channel_name)}</b>\n"
        f'🎙 "{escape_html(truncate(alert.title, 120))}" · '
        f"{published:%Y-%m-%d %H:%M} · {fmt_duration(alert.duration_seconds)}"
    )

    parts = [header, ""]
    parts.extend(_call_block(call, alert.video_url) for call in alert.calls)

    crosscheck = _crosscheck_block(alert)
    if crosscheck:
        parts.extend(["", crosscheck])

    parts.extend(["", _track_record_line(alert), f"🔗 {alert.video_url}"])
    return "\n".join(parts)


def render_backfill_summary(video_count: int, call_count: int, days: int) -> str:
    """One message for a whole backfill, instead of an alert per video."""
    return (
        f"🗂 <b>Backfill complete</b>\n"
        f"   {video_count} videos processed over the last {days} days\n"
        f"   {call_count} verified calls recorded\n"
        f"   Individual alerts were suppressed for backfilled videos."
    )


def render_health_alert(title: str, problems: list[str]) -> str:
    lines = [f"🩺 <b>{escape_html(title)}</b>"]
    lines.extend(f"   • {escape_html(p)}" for p in problems)
    return "\n".join(lines)


def split_message(text: str, limit: int = TELEGRAM_MAX_CHARS) -> list[str]:
    """Split a message on line boundaries, never mid-line.

    Telegram's HTML parser rejects a message with an unclosed tag, so splitting
    mid-line would produce a 400 from the API. Every call block is written as
    whole lines with balanced tags, which makes line boundaries safe cut points.

    A single line longer than the limit is hard-wrapped as a last resort; that
    cannot happen with our own formatting, but an unbounded field would
    otherwise be able to wedge the outbox permanently.
    """
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append("\n".join(current))
                current, current_len = [], 0
            parts.append(line[:limit])
            line = line[limit:]

        extra = len(line) + (1 if current else 0)
        if current_len + extra > limit:
            parts.append("\n".join(current))
            current, current_len = [line], len(line)
        else:
            current.append(line)
            current_len += extra

    if current:
        parts.append("\n".join(current))
    return [p for p in parts if p]


__all__ = [
    "MAX_QUOTE_CHARS",
    "TELEGRAM_MAX_CHARS",
    "AlertCall",
    "AlertCrosscheck",
    "VideoAlert",
    "escape_html",
    "render_backfill_summary",
    "render_health_alert",
    "render_video_alert",
    "split_message",
]
