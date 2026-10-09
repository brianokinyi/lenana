"""Small pure helpers. No I/O, no config — trivially testable."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

#: PT1H23M45S / P1DT2H / PT45S  (YouTube contentDetails.duration)
_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)

#: Any YouTube URL shape, or a bare 11-character id.
_VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/|/v/)(?P<id>[A-Za-z0-9_-]{11})")
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def human_datetime(value: datetime | str) -> str:
    """Display a localized date with a 12-hour clock, retaining nonzero minutes."""
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    clock = str(dt.hour % 12 or 12)
    if dt.minute:
        clock += f":{dt.minute:02d}"
    return f"{dt.day} {dt:%b %Y} {clock}{dt:%p}"


def human_date(value: str) -> str:
    dt = datetime.fromisoformat(value)
    return f"{dt.day} {dt:%b %Y}"


def parse_iso8601_duration(value: str) -> int | None:
    """Seconds from an ISO-8601 duration, or None if unparseable.

    YouTube returns `P0D` for livestreams that are still running, which
    correctly yields 0 and lets the caller treat it as "no duration yet".
    """
    if not value:
        return None
    m = _ISO_DURATION_RE.match(value.strip())
    if not m:
        return None
    parts = m.groupdict()
    total = 0.0
    total += int(parts["days"] or 0) * 86400
    total += int(parts["hours"] or 0) * 3600
    total += int(parts["minutes"] or 0) * 60
    total += float(parts["seconds"] or 0)
    return int(total)


def extract_video_id(url_or_id: str) -> str | None:
    """Pull the 11-character video id out of any YouTube URL, or pass an id through."""
    value = (url_or_id or "").strip()
    if not value:
        return None
    if _BARE_ID_RE.match(value):
        return value
    m = _VIDEO_ID_RE.search(value)
    return m.group("id") if m else None


def parse_rfc3339(value: str) -> datetime | None:
    """Parse a YouTube API timestamp into timezone-aware UTC.

    The API returns `2026-10-09T14:32:05Z`. A value without an offset is
    assumed to be UTC, which is what the YouTube API documents.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def floor_hour(dt: datetime) -> datetime:
    """Truncate to the hour — the granularity of the price cache."""
    return dt.replace(minute=0, second=0, microsecond=0)


def to_display(dt: datetime, tz: ZoneInfo) -> datetime:
    """Convert a stored UTC datetime into the display timezone.

    Tolerates a naive value by assuming UTC, so a datetime that reached us
    from outside the database cannot silently shift by the local offset.
    """
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return aware.astimezone(tz)


def fmt_duration(seconds: int | None) -> str:
    """3725 -> '1:02:05'; 125 -> '2:05'."""
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def fmt_timestamp(seconds: int | None) -> str:
    """Transcript position as mm:ss (or h:mm:ss past an hour)."""
    return fmt_duration(seconds) if seconds is not None else "--:--"


def fmt_price(value: float | None, currency: str | None = "USD") -> str:
    """Human-readable price that keeps precision for sub-cent tokens."""
    if value is None:
        return "none given"
    symbol = "$" if (currency or "USD").upper() == "USD" else f"{currency} "
    if value >= 1000:
        return f"{symbol}{value:,.0f}"
    if value >= 1:
        return f"{symbol}{value:,.2f}"
    if value >= 0.01:
        return f"{symbol}{value:.4f}"
    return f"{symbol}{value:.8f}".rstrip("0")


def pct_change(old: float, new: float) -> float | None:
    """Percentage change, or None when the base is unusable."""
    if old is None or new is None or old <= 0:
        return None
    return (new - old) / old * 100.0


def backoff_delay(ladder_minutes: list[int], attempt: int) -> timedelta | None:
    """Delay for a retry attempt, or None once the ladder is exhausted.

    `attempt` is 1-based: the first retry uses rung 0.
    """
    index = max(attempt - 1, 0)
    if index >= len(ladder_minutes):
        return None
    return timedelta(minutes=ladder_minutes[index])


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - len(suffix), 0)] + suffix


def normalize_for_match(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Used on both sides of quote verification: auto-captions have no
    punctuation and inconsistent casing, while Claude's quote reproduces what
    it 'heard', so comparing raw strings would reject valid matches.
    """
    lowered = (text or "").lower()
    stripped = re.sub(r"[^a-z0-9\s]+", " ", lowered)
    return re.sub(r"\s+", " ", stripped).strip()


__all__ = [
    "backoff_delay",
    "extract_video_id",
    "floor_hour",
    "fmt_duration",
    "fmt_price",
    "fmt_timestamp",
    "normalize_for_match",
    "parse_iso8601_duration",
    "parse_rfc3339",
    "pct_change",
    "to_display",
    "truncate",
]
