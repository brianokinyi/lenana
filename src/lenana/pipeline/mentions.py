"""Extract all coins discussed, including those with no directional prediction."""

from __future__ import annotations

import hashlib
import json
import math
import re

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, col, select

from ..config import Config
from ..llm.chunking import chunk_transcript
from ..llm.client import ClaudeExtractor, Usage
from ..logging import get_logger
from ..models import CoinMention, MentionScan, Transcript, Video
from ..utils import normalize_for_match
from ..verify.quotes import verify_quote, window_text
from .extract import segments_from_row
from .runner import StageResult

log = get_logger("pipeline.mentions")
PROMPT_VERSION = "mentions_v1"
ALIASES = {"XBT": "BTC", "BITCOIN": "BTC", "ETHEREUM": "ETH", "SOLANA": "SOL"}


def normalise_coin(symbol: str) -> str:
    symbol = symbol.strip().lstrip("$").upper()
    return ALIASES.get(symbol, symbol)


class RawMention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token_symbol: str = Field(description="Canonical uppercase ticker, e.g. BTC for Bitcoin.")
    token_name: str | None = Field(description="Full coin name, or null if unknown.")
    quote: str = Field(description="Verbatim transcript excerpt, at least 20 characters.")
    timestamp_seconds: float = Field(description="Bracketed timestamp where the quote starts.")
    is_sponsored: bool = Field(description="True for an advert or sponsored segment.")


class MentionExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mentions: list[RawMention] = Field(description="Every distinct coin discussed in this chunk.")


def fingerprint(transcript: Transcript) -> str:
    payload = json.dumps(transcript.segments, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256((PROMPT_VERSION + payload).encode()).hexdigest()


def checked_mentions(
    items: list[RawMention], transcript: Transcript, cfg: Config
) -> tuple[list[RawMention], int]:
    segments = segments_from_row(transcript)
    unique: dict[str, RawMention] = {}
    rejected = 0
    for item in items:
        symbol = normalise_coin(item.token_symbol)
        names = [symbol, item.token_symbol, item.token_name or ""]
        names.extend(alias for alias, canonical in ALIASES.items() if canonical == symbol)
        quote = normalize_for_match(item.quote)
        window = normalize_for_match(
            window_text(segments, item.timestamp_seconds, cfg.app.verification.quote_window_seconds)
        )
        evidence = any(
            re.search(r"\b" + re.escape(normalize_for_match(name)) + r"\b", quote)
            and re.search(r"\b" + re.escape(normalize_for_match(name)) + r"\b", window)
            for name in names
            if name.strip()
        )
        if (
            not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,19}", symbol)
            or not math.isfinite(item.timestamp_seconds)
            or not evidence
            or not verify_quote(item.quote, item.timestamp_seconds, segments, cfg).ok
        ):
            rejected += 1
            continue
        item = item.model_copy(update={"token_symbol": symbol})
        prior = unique.get(symbol)
        # Prefer an organic discussion to an advert, then earliest evidence.
        if prior is None or (item.is_sponsored, item.timestamp_seconds) < (
            prior.is_sponsored,
            prior.timestamp_seconds,
        ):
            unique[symbol] = item
    return list(unique.values()), rejected


def mentions(session: Session, cfg: Config) -> StageResult:
    result = StageResult(stage="mentions")
    transcripts = session.exec(select(Transcript).order_by(col(Transcript.video_pk))).all()
    pending = [
        t
        for t in transcripts
        if (
            (scan := session.get(MentionScan, t.video_pk)) is None
            or scan.fingerprint != fingerprint(t)
        )
    ][: cfg.app.reliability.stage_batch_size]
    if not pending:
        return result
    extractor = ClaudeExtractor(cfg)
    for transcript in pending:
        video = session.get(Video, transcript.video_pk)
        if video is None:
            continue
        try:
            usage = Usage()
            raw: list[RawMention] = []
            chunks = chunk_transcript(
                segments_from_row(transcript),
                chunk_seconds=cfg.app.extraction.chunk_minutes * 60,
                overlap_seconds=cfg.app.extraction.chunk_overlap_seconds,
            )
            for chunk in chunks:
                prompt = (
                    "Extract every distinct cryptocurrency or token discussed in this transcript, "
                    "including coins mentioned WITHOUT any price target or prediction. "
                    "Return one entry per coin in this chunk with a verbatim evidence quote "
                    "and its exact bracketed starting timestamp. Prefer organic discussion to "
                    "advertisements. Normalize full names to canonical tickers (Bitcoin=BTC, "
                    "Ethereum=ETH, Solana=SOL); do not merge different projects sharing a ticker. "
                    "Do not infer coins from the title, ambiguous ordinary words, exchanges, "
                    "fiat currencies, or generic 'crypto'. Quote at least 20 characters including "
                    "the actual coin name or ticker. Return an empty list if none are present.\n\n"
                    + chunk.to_prompt_text()
                )
                extracted, usage = extractor.extract(prompt, MentionExtraction, usage)
                raw.extend(extracted.mentions)
            verified, rejected = checked_mentions(raw, transcript, cfg)
            for old in session.exec(
                select(CoinMention).where(CoinMention.video_pk == transcript.video_pk)
            ).all():
                session.delete(old)
            session.flush()
            for item in verified:
                session.add(
                    CoinMention(
                        video_pk=transcript.video_pk,
                        token_symbol=item.token_symbol,
                        token_name=item.token_name,
                        quote=item.quote,
                        timestamp_seconds=int(item.timestamp_seconds),
                        is_sponsored=item.is_sponsored,
                    )
                )
            session.merge(
                MentionScan(
                    video_pk=transcript.video_pk,
                    fingerprint=fingerprint(transcript),
                    model=extractor.model,
                    prompt_version=PROMPT_VERSION,
                    cost_usd=usage.cost_usd,
                    rejected=rejected,
                )
            )
            session.commit()
            result.processed += 1
            log.info(
                "mentions scanned",
                extra={
                    "video_id": video.video_id,
                    "coins": len(verified),
                    "rejected": rejected,
                },
            )
        except Exception:
            session.rollback()
            result.failed += 1
            log.exception("mention scan failed", extra={"video_id": video.video_id})
    return result
