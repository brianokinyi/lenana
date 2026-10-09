"""Deduplicate calls across overlapping chunks.

The 60-second overlap means a call near a boundary is extracted twice. Two
rows for one statement would double-count that channel in the cross-check and
in its hit rate, so merging is a correctness requirement, not tidiness.

Identity is (speaker, token, direction), per the spec. Among duplicates we
keep the *clearest* quote rather than the first one seen, because the chunk
that saw the call whole usually produced the better quote.
"""

from __future__ import annotations

from collections.abc import Iterable

from .schemas import RawCall

#: Calls further apart than this are treated as separate statements even when
#: speaker, token and direction match — a host repeating a call 40 minutes
#: later is a second call, not a duplicate of the first.
DEFAULT_MERGE_WINDOW_SECONDS = 180.0


def _identity(call: RawCall) -> tuple[str, str, str]:
    return (call.speaker.strip().lower(), call.token_symbol.strip().upper(), call.direction)


def _quality(call: RawCall) -> tuple[int, int, int]:
    """Ranking key for choosing between duplicate quotes.

    Prefers a call that carries a price target, then one with a timeframe,
    then the longer quote — in that order, because a target is the most useful
    thing a call can have and length alone is a weak signal.
    """
    return (
        1 if call.price_target is not None else 0,
        1 if call.timeframe_text else 0,
        len(call.quote or ""),
    )


def merge_calls(
    calls: Iterable[RawCall],
    window_seconds: float = DEFAULT_MERGE_WINDOW_SECONDS,
) -> list[RawCall]:
    """Collapse duplicate calls, keeping the best quote for each.

    Returns calls ordered by timestamp.
    """
    groups: dict[tuple[str, str, str], list[RawCall]] = {}
    for call in calls:
        groups.setdefault(_identity(call), []).append(call)

    merged: list[RawCall] = []
    for group in groups.values():
        group.sort(key=lambda c: c.timestamp_seconds)
        cluster: list[RawCall] = []
        for call in group:
            if cluster and call.timestamp_seconds - cluster[-1].timestamp_seconds > window_seconds:
                merged.append(max(cluster, key=_quality))
                cluster = []
            cluster.append(call)
        if cluster:
            merged.append(max(cluster, key=_quality))

    merged.sort(key=lambda c: (c.timestamp_seconds, c.token_symbol))
    return merged


__all__ = ["DEFAULT_MERGE_WINDOW_SECONDS", "merge_calls"]
