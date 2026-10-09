"""Split a transcript into overlapping time windows for extraction.

Chunking is about extraction *quality*, not context limits: a 20-minute window
of a panel show is one coherent discussion, whereas a whole three-hour stream
invites the model to blur who said what. The 60-second overlap exists so a
call straddling a boundary is seen whole by at least one chunk — the merge
step then removes the duplicate.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..sources.transcripts.base import TranscriptSegment


@dataclass(frozen=True)
class Chunk:
    index: int
    start_seconds: float
    end_seconds: float
    segments: list[TranscriptSegment]

    @property
    def is_empty(self) -> bool:
        return not self.segments

    def to_prompt_text(self) -> str:
        """Render as `[seconds] text` lines.

        Raw integer seconds rather than mm:ss so the model can copy the number
        into `timestamp_seconds` verbatim — asking it to convert mm:ss back to
        seconds is arithmetic it does not need to be doing.
        """
        return "\n".join(f"[{int(s.start)}] {s.text.strip()}" for s in self.segments)

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())


def chunk_transcript(
    segments: list[TranscriptSegment],
    chunk_seconds: int,
    overlap_seconds: int,
) -> list[Chunk]:
    """Split segments into overlapping windows, ordered by time.

    Windows advance by `chunk_seconds - overlap_seconds`. A segment belongs to
    a window when its *start* falls inside it, so no segment is ever split and
    the renderable text of each chunk is contiguous.
    """
    if not segments:
        return []
    if chunk_seconds <= 0:
        raise ValueError("chunk_seconds must be positive")
    if overlap_seconds >= chunk_seconds:
        raise ValueError("overlap_seconds must be smaller than chunk_seconds")

    ordered = sorted(segments, key=lambda s: s.start)
    total = ordered[-1].start + max(ordered[-1].duration, 0.0)
    step = chunk_seconds - overlap_seconds

    chunks: list[Chunk] = []
    window_start = 0.0
    index = 0
    while window_start < total or index == 0:
        window_end = window_start + chunk_seconds
        in_window = [s for s in ordered if window_start <= s.start < window_end]
        if in_window:
            chunks.append(
                Chunk(
                    index=index,
                    start_seconds=window_start,
                    end_seconds=min(window_end, total),
                    segments=in_window,
                )
            )
            index += 1
        if window_end >= total:
            break
        window_start += step

    return chunks


__all__ = ["Chunk", "chunk_transcript"]
