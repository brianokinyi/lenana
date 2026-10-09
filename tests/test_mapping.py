"""Ticker to CoinGecko id mapping, including symbol collisions."""

from __future__ import annotations

from sqlmodel import Session

from yt_crypto_signals.models import Coin
from yt_crypto_signals.pipeline.enrich import target_is_plausible
from yt_crypto_signals.prices.coingecko import resolve_symbol


def add_coin(session: Session, coin_id: str, symbol: str, market_cap: float | None) -> None:
    session.add(Coin(coingecko_id=coin_id, symbol=symbol, name=coin_id, market_cap=market_cap))
    session.commit()


def test_unique_symbol_maps(session: Session) -> None:
    add_coin(session, "solana", "SOL", 90_000_000_000)
    assert resolve_symbol(session, "SOL") == ("solana", "mapped")


def test_unknown_symbol_is_unmapped(session: Session) -> None:
    assert resolve_symbol(session, "NOTACOIN") == (None, "unmapped")


def test_clear_market_cap_leader_wins(session: Session) -> None:
    """Dozens of tokens squat popular tickers; the real one is orders of
    magnitude larger."""
    add_coin(session, "solana", "SOL", 90_000_000_000)
    add_coin(session, "sol-scam-token", "SOL", 120_000)
    coin_id, status = resolve_symbol(session, "SOL")
    assert coin_id == "solana"
    assert status == "mapped"


def test_close_competitors_are_flagged_ambiguous(session: Session) -> None:
    """Within 5x, the ticker genuinely is ambiguous and a human should look."""
    add_coin(session, "coin-a", "GRT", 400_000_000)
    add_coin(session, "coin-b", "GRT", 300_000_000)
    coin_id, status = resolve_symbol(session, "GRT")
    assert status == "ambiguous"
    assert coin_id == "coin-a", "the best candidate is still returned so enrichment proceeds"


def test_exactly_five_times_is_mapped(session: Session) -> None:
    add_coin(session, "big", "X", 500_000)
    add_coin(session, "small", "X", 100_000)
    assert resolve_symbol(session, "X") == ("big", "mapped")


def test_just_under_five_times_is_ambiguous(session: Session) -> None:
    add_coin(session, "big", "X", 499_999)
    add_coin(session, "small", "X", 100_000)
    assert resolve_symbol(session, "X")[1] == "ambiguous"


def test_symbol_is_normalised_before_lookup(session: Session) -> None:
    add_coin(session, "solana", "SOL", 90_000_000_000)
    for spelling in ("sol", " SOL ", "$SOL", "$sol"):
        assert resolve_symbol(session, spelling) == ("solana", "mapped")


def test_null_market_caps_do_not_crash(session: Session) -> None:
    add_coin(session, "unknown-a", "ZZZ", None)
    add_coin(session, "unknown-b", "ZZZ", None)
    coin_id, status = resolve_symbol(session, "ZZZ")
    assert coin_id is not None
    assert status == "ambiguous"


# --------------------------------------------------------------------------
# target plausibility (caption artefacts)
# --------------------------------------------------------------------------


def test_plausible_targets_are_kept() -> None:
    assert target_is_plausible(300.0, 180.0)
    assert target_is_plausible(150_000.0, 100_000.0)
    assert target_is_plausible(0.12, 0.09)


def test_mangled_number_is_rejected() -> None:
    """The real case this guard caught: captions wrote "$900,000" for a token
    trading near $500, and the quote matched the transcript perfectly."""
    assert not target_is_plausible(900_000.0, 500.0)


def test_doge_two_billion_is_rejected() -> None:
    assert not target_is_plausible(2_000_000_000.0, 0.0873)


def test_boundary_ratios() -> None:
    assert target_is_plausible(100.0, 2.0)  # exactly 50x
    assert not target_is_plausible(100.0, 1.99)  # just over 50x
    assert target_is_plausible(2.0, 100.0)  # exactly 1/50
    assert not target_is_plausible(1.99, 100.0)


def test_non_positive_inputs_are_implausible() -> None:
    assert not target_is_plausible(0.0, 100.0)
    assert not target_is_plausible(100.0, 0.0)
    assert not target_is_plausible(-5.0, 100.0)
