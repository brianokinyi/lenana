"""Telegram rendering: HTML escaping and the 4096-character limit."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from yt_crypto_signals.enums import Confidence, Direction, Divergence
from yt_crypto_signals.notify.format import (
    TELEGRAM_MAX_CHARS,
    AlertCall,
    AlertCrosscheck,
    VideoAlert,
    escape_html,
    render_backfill_summary,
    render_video_alert,
    split_message,
)

TZ = ZoneInfo("Africa/Nairobi")


def make_call(**overrides: object) -> AlertCall:
    base: dict[str, object] = {
        "token_symbol": "SOL",
        "direction": Direction.UP,
        "price_target": 300.0,
        "target_currency": "USD",
        "timeframe_text": "end of year",
        "confidence": Confidence.HIGH,
        "speaker": "Ran Neuner",
        "quote": "i think sol is going to three hundred",
        "timestamp_seconds": 625,
        "is_conditional": False,
    }
    base.update(overrides)
    return AlertCall(**base)  # type: ignore[arg-type]


def make_alert(**overrides: object) -> VideoAlert:
    base: dict[str, object] = {
        "channel_name": "Crypto Banter",
        "title": "The Altcoin Dump Is Here",
        "published_at": datetime(2026, 10, 9, 9, 0, tzinfo=UTC),
        "duration_seconds": 2442,
        "video_url": "https://www.youtube.com/watch?v=abc12345678",
        "calls": [make_call()],
    }
    base.update(overrides)
    return VideoAlert(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# escaping
# --------------------------------------------------------------------------


def test_escape_html_handles_all_three_characters() -> None:
    assert escape_html("a & b < c > d") == "a &amp; b &lt; c &gt; d"


def test_escape_html_does_not_double_escape_ampersand() -> None:
    """`&` must be replaced first, or the escapes for `<` and `>` get escaped."""
    assert escape_html("<b>") == "&lt;b&gt;"
    assert escape_html("&lt;") == "&amp;lt;"


def test_quote_with_markup_is_escaped_in_the_alert() -> None:
    body = render_video_alert(
        make_alert(calls=[make_call(quote="<script>alert(1)</script> & more")]), TZ
    )
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


def test_title_with_markup_is_escaped() -> None:
    body = render_video_alert(make_alert(title="BTC <b>MOON</b> & beyond"), TZ)
    assert "<b>MOON</b>" not in body
    assert "&lt;b&gt;MOON&lt;/b&gt;" in body
    assert "&amp; beyond" in body


def test_channel_name_is_escaped() -> None:
    body = render_video_alert(make_alert(channel_name="A & B <Crypto>"), TZ)
    assert "A &amp; B &lt;Crypto&gt;" in body


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def test_alert_contains_the_required_elements() -> None:
    body = render_video_alert(make_alert(), TZ)
    assert "📡" in body and "Crypto Banter" in body
    assert "🟢" in body and "SOL" in body and "UP" in body
    assert "$300.00" in body
    assert "end of year" in body
    assert "Ran Neuner" in body
    assert "https://www.youtube.com/watch?v=abc12345678" in body


def test_timestamp_links_deep_into_the_video() -> None:
    body = render_video_alert(make_alert(), TZ)
    assert 'href="https://www.youtube.com/watch?v=abc12345678&t=625s"' in body
    assert "[10:25]" in body


def test_down_call_uses_the_red_marker() -> None:
    body = render_video_alert(make_alert(calls=[make_call(direction=Direction.DOWN)]), TZ)
    assert "🔴" in body and "DOWN" in body


def test_conditional_call_gets_the_diamond_marker() -> None:
    body = render_video_alert(make_alert(calls=[make_call(is_conditional=True)]), TZ)
    assert "🔸" in body


def test_missing_target_says_none_given() -> None:
    body = render_video_alert(
        make_alert(calls=[make_call(price_target=None, target_currency=None)]), TZ
    )
    assert "none given" in body


def test_published_date_is_in_the_display_timezone() -> None:
    # 09:00 UTC is 12:00 in Nairobi (UTC+3).
    body = render_video_alert(make_alert(), TZ)
    assert "2026-10-09 12:00" in body


def test_track_record_hidden_below_minimum_sample() -> None:
    body = render_video_alert(make_alert(hit_rate_pct=80.0, scored_count=4), TZ)
    assert "not enough data" in body
    assert "80%" not in body


def test_track_record_shown_at_minimum_sample() -> None:
    body = render_video_alert(make_alert(hit_rate_pct=61.9, scored_count=12), TZ)
    assert "62% (n=12)" in body


def test_crosscheck_split_is_flagged() -> None:
    body = render_video_alert(
        make_alert(
            crosschecks=[
                AlertCrosscheck(
                    token_symbol="SOL",
                    bull_count=2,
                    bear_count=1,
                    channels=["Lark Davis", "threadguy"],
                    divergence=Divergence.SPLIT,
                )
            ]
        ),
        TZ,
    )
    assert "🔀" in body
    assert "2 🟢 / 1 🔴" in body
    assert "⚠️ split" in body


def test_crosscheck_aligned_is_not_flagged() -> None:
    body = render_video_alert(
        make_alert(
            crosschecks=[
                AlertCrosscheck(
                    token_symbol="SOL",
                    bull_count=3,
                    bear_count=0,
                    channels=["Lark Davis"],
                    divergence=Divergence.ALIGNED,
                )
            ]
        ),
        TZ,
    )
    assert "⚠️" not in body


def test_backfill_summary_mentions_counts() -> None:
    body = render_backfill_summary(42, 137, 30)
    assert "42 videos" in body and "137 verified calls" in body and "30 days" in body


# --------------------------------------------------------------------------
# splitting
# --------------------------------------------------------------------------


def test_short_message_is_not_split() -> None:
    assert split_message("hello") == ["hello"]


def test_split_respects_the_limit() -> None:
    text = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
    parts = split_message(text)
    assert len(parts) > 1
    assert all(len(p) <= TELEGRAM_MAX_CHARS for p in parts)


def test_split_never_breaks_mid_line() -> None:
    """Telegram rejects a message with an unclosed tag, so cuts must land on
    line boundaries where our tags are balanced."""
    lines = [f"<b>line {i}</b> " + "y" * 60 for i in range(300)]
    parts = split_message("\n".join(lines))
    rejoined = "\n".join(parts).split("\n")
    assert rejoined == lines
    for part in parts:
        assert part.count("<b>") == part.count("</b>")


def test_split_preserves_all_content() -> None:
    text = "\n".join(str(i) * 50 for i in range(400))
    assert "\n".join(split_message(text)) == text


def test_single_overlong_line_is_hard_wrapped() -> None:
    """Cannot happen with our own formatting, but an unbounded field must not
    be able to wedge the outbox."""
    parts = split_message("z" * 10000, limit=4096)
    assert len(parts) == 3
    assert all(len(p) <= 4096 for p in parts)
    assert "".join(parts) == "z" * 10000


def test_many_calls_split_into_several_messages() -> None:
    alert = make_alert(calls=[make_call(quote="q " * 100) for _ in range(40)])
    parts = split_message(render_video_alert(alert, TZ))
    assert len(parts) > 1
    assert all(len(p) <= TELEGRAM_MAX_CHARS for p in parts)
