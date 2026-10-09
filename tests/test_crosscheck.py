"""Cross-check window edges and divergence classification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from yt_crypto_signals.config import Config
from yt_crypto_signals.enums import (
    Confidence,
    Direction,
    Divergence,
    VerifyStatus,
    VideoState,
)
from yt_crypto_signals.models import Call, Channel, Crosscheck, ExtractionRun, Video
from yt_crypto_signals.pipeline.crosscheck import crosscheck

BASE = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def setup_channel(session: Session, handle: str) -> Channel:
    channel = Channel(handle=handle, name=handle.lstrip("@"))
    session.add(channel)
    session.commit()
    session.refresh(channel)
    return channel


def add_call(
    session: Session,
    channel: Channel,
    video_id: str,
    direction: Direction,
    entry_at: datetime,
    coin: str = "solana",
    state: VideoState = VideoState.ENRICHED,
    is_sponsored: bool = False,
) -> Call:
    assert channel.id is not None
    video = Video(video_id=video_id, channel_pk=channel.id, published_at=entry_at, state=state)
    session.add(video)
    session.commit()
    session.refresh(video)
    assert video.id is not None

    run = ExtractionRun(video_pk=video.id, prompt_version="v1", model="test")
    session.add(run)
    session.commit()
    session.refresh(run)
    assert run.id is not None

    call = Call(
        video_pk=video.id,
        extraction_run_pk=run.id,
        token_symbol="SOL",
        direction=direction,
        confidence=Confidence.MEDIUM,
        speaker="Host",
        quote="q",
        timestamp_seconds=0,
        verify_status=VerifyStatus.VERIFIED,
        coingecko_id=coin,
        entry_price_at=entry_at,
        entry_price_usd=100.0,
        is_sponsored=is_sponsored,
    )
    session.add(call)
    session.commit()
    session.refresh(call)
    return call


def crosscheck_for(session: Session, call: Call) -> Crosscheck | None:
    return session.exec(select(Crosscheck).where(Crosscheck.call_pk == call.id)).first()


def test_single_channel_call_is_solo(session: Session, config: Config) -> None:
    a = setup_channel(session, "@A")
    call = add_call(session, a, "vid00000001", Direction.UP, BASE)
    crosscheck(session, config)
    row = crosscheck_for(session, call)
    assert row is not None
    assert row.divergence is Divergence.SOLO
    assert (row.bull_count, row.bear_count) == (1, 0)


def test_agreeing_channels_are_aligned(session: Session, config: Config) -> None:
    a, b = setup_channel(session, "@A"), setup_channel(session, "@B")
    add_call(session, b, "vid00000002", Direction.UP, BASE, state=VideoState.DONE)
    call = add_call(session, a, "vid00000001", Direction.UP, BASE)
    crosscheck(session, config)
    row = crosscheck_for(session, call)
    assert row is not None
    assert row.divergence is Divergence.ALIGNED
    assert (row.bull_count, row.bear_count) == (2, 0)


def test_disagreeing_channels_are_split(session: Session, config: Config) -> None:
    a, b = setup_channel(session, "@A"), setup_channel(session, "@B")
    add_call(session, b, "vid00000002", Direction.DOWN, BASE, state=VideoState.DONE)
    call = add_call(session, a, "vid00000001", Direction.UP, BASE)
    crosscheck(session, config)
    row = crosscheck_for(session, call)
    assert row is not None
    assert row.divergence is Divergence.SPLIT
    assert (row.bull_count, row.bear_count) == (1, 1)


def test_peer_just_inside_the_window_counts(session: Session, config: Config) -> None:
    config.app.crosscheck.window_days = 5
    a, b = setup_channel(session, "@A"), setup_channel(session, "@B")
    add_call(
        session,
        b,
        "vid00000002",
        Direction.UP,
        BASE - timedelta(days=5) + timedelta(minutes=1),
        state=VideoState.DONE,
    )
    call = add_call(session, a, "vid00000001", Direction.UP, BASE)
    crosscheck(session, config)
    row = crosscheck_for(session, call)
    assert row is not None and row.bull_count == 2


def test_peer_just_outside_the_window_is_excluded(session: Session, config: Config) -> None:
    config.app.crosscheck.window_days = 5
    a, b = setup_channel(session, "@A"), setup_channel(session, "@B")
    add_call(
        session,
        b,
        "vid00000002",
        Direction.UP,
        BASE - timedelta(days=5, minutes=1),
        state=VideoState.DONE,
    )
    call = add_call(session, a, "vid00000001", Direction.UP, BASE)
    crosscheck(session, config)
    row = crosscheck_for(session, call)
    assert row is not None
    assert row.divergence is Divergence.SOLO
    assert row.bull_count == 1


def test_window_is_symmetric(session: Session, config: Config) -> None:
    """A peer call *after* ours still counts; the window is ±N days."""
    config.app.crosscheck.window_days = 5
    a, b = setup_channel(session, "@A"), setup_channel(session, "@B")
    add_call(
        session, b, "vid00000002", Direction.UP, BASE + timedelta(days=4), state=VideoState.DONE
    )
    call = add_call(session, a, "vid00000001", Direction.UP, BASE)
    crosscheck(session, config)
    row = crosscheck_for(session, call)
    assert row is not None and row.bull_count == 2


def test_same_channel_is_not_its_own_peer(session: Session, config: Config) -> None:
    a = setup_channel(session, "@A")
    add_call(session, a, "vid00000002", Direction.UP, BASE, state=VideoState.DONE)
    call = add_call(session, a, "vid00000001", Direction.UP, BASE)
    crosscheck(session, config)
    row = crosscheck_for(session, call)
    assert row is not None
    assert row.divergence is Divergence.SOLO


def test_different_coin_is_not_a_peer(session: Session, config: Config) -> None:
    a, b = setup_channel(session, "@A"), setup_channel(session, "@B")
    add_call(session, b, "vid00000002", Direction.UP, BASE, coin="ethereum", state=VideoState.DONE)
    call = add_call(session, a, "vid00000001", Direction.UP, BASE)
    crosscheck(session, config)
    row = crosscheck_for(session, call)
    assert row is not None and row.divergence is Divergence.SOLO


def test_sponsored_peer_calls_are_excluded(session: Session, config: Config) -> None:
    """A paid promo is not independent corroboration."""
    a, b = setup_channel(session, "@A"), setup_channel(session, "@B")
    add_call(
        session,
        b,
        "vid00000002",
        Direction.UP,
        BASE,
        state=VideoState.DONE,
        is_sponsored=True,
    )
    call = add_call(session, a, "vid00000001", Direction.UP, BASE)
    crosscheck(session, config)
    row = crosscheck_for(session, call)
    assert row is not None and row.divergence is Divergence.SOLO


def test_crosscheck_advances_the_video(session: Session, config: Config) -> None:
    a = setup_channel(session, "@A")
    call = add_call(session, a, "vid00000001", Direction.UP, BASE)
    crosscheck(session, config)
    video = session.get(Video, call.video_pk)
    assert video is not None and video.state is VideoState.CROSSCHECKED
