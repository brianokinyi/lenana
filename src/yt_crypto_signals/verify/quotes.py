"""Deterministic quote verification. No LLM involved.

This is the anti-hallucination guard. A call survives only if its quote
actually appears in the transcript near the timestamp the model claimed. A
fabricated quote has nowhere to match; a real quote attributed to the wrong
moment fails the window check.

Two details make it work on auto-generated captions:

* **Normalised comparison.** Captions have no punctuation and inconsistent
  casing, while the model reproduces what it "heard" with punctuation. Raw
  string comparison would reject valid matches, so both sides are normalised
  (lowercased, punctuation stripped, whitespace collapsed).
* **`partial_ratio`, not `ratio`.** The quote is a short span inside a much
  longer window of transcript text. A whole-string similarity score would be
  dominated by the surrounding words and never clear the threshold.
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz

from ..config import Config
from ..enums import RejectReason
from ..sources.transcripts.base import TranscriptSegment
from ..utils import normalize_for_match


@dataclass(frozen=True)
class QuoteVerdict:
    ok: bool
    score: float
    reason: RejectReason | None = None
    detail: str | None = None
    #: Where the best match was found, useful when the model's timestamp drifts.
    matched_at: float | None = None


def window_text(
    segments: list[TranscriptSegment], centre_seconds: float, radius_seconds: float
) -> str:
    """Concatenated transcript text within ±radius of a point in time."""
    low = centre_seconds - radius_seconds
    high = centre_seconds + radius_seconds
    return " ".join(s.text.strip() for s in segments if low <= s.start <= high and s.text.strip())


def verify_quote(
    quote: str,
    timestamp_seconds: float,
    segments: list[TranscriptSegment],
    cfg: Config,
) -> QuoteVerdict:
    """Check that `quote` appears in the transcript near `timestamp_seconds`."""
    vc = cfg.app.verification
    cleaned = (quote or "").strip()

    if len(cleaned) < vc.min_quote_chars:
        return QuoteVerdict(
            ok=False,
            score=0.0,
            reason=RejectReason.QUOTE_TOO_SHORT,
            detail=f"quote is {len(cleaned)} chars, minimum is {vc.min_quote_chars}",
        )

    if not segments:
        return QuoteVerdict(
            ok=False, score=0.0, reason=RejectReason.QUOTE_NOT_FOUND, detail="no transcript"
        )

    transcript_end = segments[-1].start + max(segments[-1].duration, 0.0)
    if timestamp_seconds < 0 or timestamp_seconds > transcript_end + vc.quote_window_seconds:
        return QuoteVerdict(
            ok=False,
            score=0.0,
            reason=RejectReason.TIMESTAMP_OUT_OF_RANGE,
            detail=(
                f"timestamp {timestamp_seconds:.0f}s is outside the transcript "
                f"(ends at {transcript_end:.0f}s)"
            ),
        )

    needle = normalize_for_match(cleaned)
    haystack = normalize_for_match(
        window_text(segments, timestamp_seconds, float(vc.quote_window_seconds))
    )
    if not haystack:
        return QuoteVerdict(
            ok=False,
            score=0.0,
            reason=RejectReason.QUOTE_NOT_FOUND,
            detail=f"no transcript text within ±{vc.quote_window_seconds}s of "
            f"{timestamp_seconds:.0f}s",
        )

    score = float(fuzz.partial_ratio(needle, haystack))
    if score >= vc.fuzzy_threshold:
        return QuoteVerdict(ok=True, score=score, matched_at=timestamp_seconds)

    return QuoteVerdict(
        ok=False,
        score=score,
        reason=RejectReason.QUOTE_NOT_FOUND,
        detail=(
            f"best match {score:.1f} is below the {vc.fuzzy_threshold} threshold "
            f"within ±{vc.quote_window_seconds}s of {timestamp_seconds:.0f}s"
        ),
    )


def locate_quote(
    quote: str, segments: list[TranscriptSegment], cfg: Config
) -> tuple[float, float] | None:
    """Best (timestamp, score) for a quote anywhere in the transcript.

    Diagnostic only — used by the dry-run report to show that a rejected call's
    quote exists but at a different time, which distinguishes a timestamp slip
    from a fabrication.
    """
    needle = normalize_for_match(quote)
    if not needle or not segments:
        return None

    best_score = -1.0
    best_at = 0.0
    step = max(cfg.app.verification.quote_window_seconds, 1)
    end = segments[-1].start + max(segments[-1].duration, 0.0)
    centre = 0.0
    while centre <= end + step:
        haystack = normalize_for_match(window_text(segments, centre, float(step)))
        if haystack:
            score = float(fuzz.partial_ratio(needle, haystack))
            if score > best_score:
                best_score, best_at = score, centre
        centre += step
    return (best_at, best_score) if best_score >= 0 else None


__all__ = ["QuoteVerdict", "locate_quote", "verify_quote", "window_text"]
