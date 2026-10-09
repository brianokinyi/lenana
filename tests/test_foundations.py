"""Checkpoint 1 coverage: config validation, helpers, redaction, locking,
and the database constraints the later stages depend on."""

from __future__ import annotations

import multiprocessing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from lenana.config import AppConfig, ChannelConfig, ProxyProvider, Settings
from lenana.enums import FailureKind, OutboxKind, VideoState
from lenana.locking import AlreadyRunning, exclusive_lock
from lenana.logging import redact
from lenana.models import Channel, Outbox, Video
from lenana.utils import (
    backoff_delay,
    extract_video_id,
    fmt_duration,
    fmt_price,
    normalize_for_match,
    parse_iso8601_duration,
    parse_rfc3339,
    pct_change,
)

# --------------------------------------------------------------------------
# utils
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("PT1H23M45S", 5025),
        ("PT45S", 45),
        ("PT3M", 180),
        ("P1DT2H", 93600),
        ("P0D", 0),  # YouTube returns this for a still-running livestream
        ("", None),
        ("garbage", None),
    ],
)
def test_parse_iso8601_duration(value: str, expected: int | None) -> None:
    assert parse_iso8601_duration(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://www.youtube.com/watch?v=n3g_L9QI-9k", "n3g_L9QI-9k"),
        ("https://youtu.be/n3g_L9QI-9k", "n3g_L9QI-9k"),
        ("https://www.youtube.com/shorts/n3g_L9QI-9k", "n3g_L9QI-9k"),
        ("https://www.youtube.com/live/n3g_L9QI-9k", "n3g_L9QI-9k"),
        ("https://www.youtube.com/watch?v=n3g_L9QI-9k&t=42s", "n3g_L9QI-9k"),
        ("n3g_L9QI-9k", "n3g_L9QI-9k"),
        ("not a video", None),
        ("", None),
    ],
)
def test_extract_video_id(value: str, expected: str | None) -> None:
    assert extract_video_id(value) == expected


def test_parse_rfc3339_returns_aware_utc() -> None:
    dt = parse_rfc3339("2026-10-09T14:32:05Z")
    assert dt == datetime(2026, 10, 9, 14, 32, 5, tzinfo=UTC)
    assert dt is not None and dt.tzinfo is not None, "stored datetimes must be aware UTC"


def test_parse_rfc3339_converts_offset_to_utc() -> None:
    assert parse_rfc3339("2026-10-09T17:32:05+03:00") == datetime(
        2026, 10, 9, 14, 32, 5, tzinfo=UTC
    )


def test_fmt_duration_and_price() -> None:
    assert fmt_duration(5025) == "1:23:45"
    assert fmt_duration(125) == "2:05"
    assert fmt_duration(None) == "unknown"
    assert fmt_price(112345.6) == "$112,346"
    assert fmt_price(1.5) == "$1.50"
    assert fmt_price(0.0421) == "$0.0421"
    assert fmt_price(0.00000123) == "$0.00000123"
    assert fmt_price(None) == "none given"


def test_pct_change_guards_bad_base() -> None:
    assert pct_change(100, 110) == pytest.approx(10.0)
    assert pct_change(100, 90) == pytest.approx(-10.0)
    assert pct_change(0, 50) is None


def test_normalize_for_match_strips_caption_noise() -> None:
    # Claude reproduces punctuation and casing the auto-captions never had.
    assert normalize_for_match("SOL is going to $300, easily!") == normalize_for_match(
        "sol is going to 300 easily"
    )


def test_backoff_ladder_exhausts() -> None:
    ladder = [15, 60, 240]
    assert backoff_delay(ladder, 1) == timedelta(minutes=15)
    assert backoff_delay(ladder, 3) == timedelta(minutes=240)
    # Past the last rung -> no further retry, caller marks permanent_failed.
    assert backoff_delay(ladder, 4) is None


def test_failure_kind_retry_policy() -> None:
    assert FailureKind.BLOCKED.is_retryable
    assert FailureKind.TRANSIENT.is_retryable
    # Retrying these would only burn proxy bandwidth and quota.
    assert not FailureKind.NO_CAPTIONS.is_retryable
    assert not FailureKind.UNAVAILABLE.is_retryable


# --------------------------------------------------------------------------
# logging redaction
# --------------------------------------------------------------------------


def test_redact_strips_proxy_credentials() -> None:
    out = redact("proxy failed: http://user123:secretpw@p.webshare.io:80 refused")
    assert "secretpw" not in out
    assert "user123" not in out
    assert "p.webshare.io" in out, "the host is useful for debugging and is not a secret"


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-abcdef123456789",
        "123456789:AAEhBOweik6ad9r_Qq4d8ZcvFk2s3kLmNoP",
        "AIzaSyB1234567890abcdefghijklmno",
        "CG-abcdefgh12345678",
    ],
)
def test_redact_strips_api_tokens(secret: str) -> None:
    assert secret not in redact(f"request failed with key {secret} attached")


def test_redact_uses_environment_as_catch_all(monkeypatch: pytest.MonkeyPatch) -> None:
    # Covers secrets whose shape the regexes do not anticipate.
    monkeypatch.setenv("WEBSHARE_PROXY_PASSWORD", "oddly-shaped-pw")
    assert "oddly-shaped-pw" not in redact("auth rejected for oddly-shaped-pw")


# --------------------------------------------------------------------------
# config validation
# --------------------------------------------------------------------------


def _base_settings_kwargs() -> dict[str, object]:
    return {
        "anthropic_api_key": "sk-ant-test",
        "youtube_api_key": "AIzaTEST",
        "telegram_bot_token": "1:TEST",
        "telegram_chat_id": "-100",
        "_env_file": None,
    }


def test_webshare_requires_credentials() -> None:
    with pytest.raises(ValidationError, match="WEBSHARE_PROXY_USERNAME"):
        Settings(**_base_settings_kwargs(), proxy_provider=ProxyProvider.WEBSHARE)  # type: ignore[arg-type]


def test_generic_proxy_requires_url() -> None:
    with pytest.raises(ValidationError, match="PROXY_URL"):
        Settings(**_base_settings_kwargs(), proxy_provider=ProxyProvider.GENERIC)  # type: ignore[arg-type]


def test_whisper_fallback_requires_key() -> None:
    with pytest.raises(ValidationError, match="WHISPER_API_KEY"):
        Settings(**_base_settings_kwargs(), whisper_fallback=True)  # type: ignore[arg-type]


def test_bad_timezone_rejected() -> None:
    with pytest.raises(ValidationError, match="not a valid IANA timezone"):
        Settings(**_base_settings_kwargs(), display_tz="Mars/Olympus")  # type: ignore[arg-type]


def test_settings_errors_never_echo_input() -> None:
    """A startup failure goes to journald, so it must not carry secrets."""
    with pytest.raises(ValidationError) as exc:
        Settings(**_base_settings_kwargs(), proxy_provider=ProxyProvider.WEBSHARE)  # type: ignore[arg-type]
    assert "sk-ant-test" not in str(exc.value)


def test_duplicate_channel_handles_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate channel handles"):
        AppConfig(
            channels=[ChannelConfig(handle="@A"), ChannelConfig(handle="@a")],
        )


def test_channel_handle_must_start_with_at() -> None:
    with pytest.raises(ValidationError, match="must start with"):
        ChannelConfig(handle="CryptoBanterGroup")


def test_unknown_config_key_rejected() -> None:
    # Catches a typo in config.yaml at startup rather than silently ignoring it.
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"channels": [{"handle": "@A"}], "discovry": {"backfill_days": 10}}
        )


def test_chunk_overlap_must_fit_in_chunk() -> None:
    with pytest.raises(ValidationError, match="chunk_overlap_seconds"):
        AppConfig.model_validate(
            {
                "channels": [{"handle": "@A"}],
                "extraction": {"chunk_minutes": 1, "chunk_overlap_seconds": 60},
            }
        )


def test_empty_retry_ladder_rejected() -> None:
    with pytest.raises(ValidationError, match="at least one rung"):
        AppConfig.model_validate(
            {"channels": [{"handle": "@A"}], "reliability": {"transcript_retry_ladder_minutes": []}}
        )


def test_shipped_config_yaml_is_valid() -> None:
    """The committed config.yaml must always load."""
    from lenana.config import load_app_config

    cfg = load_app_config()
    assert len(cfg.enabled_channels) == 4
    assert cfg.extraction.chunk_minutes == 20


# --------------------------------------------------------------------------
# locking
# --------------------------------------------------------------------------


def _child_tries_lock(path: str, result: object) -> None:  # pragma: no cover - subprocess
    try:
        with exclusive_lock(Path(path)):
            result.value = 1  # type: ignore[attr-defined]
    except AlreadyRunning:
        result.value = 2  # type: ignore[attr-defined]


def test_second_process_cannot_take_the_lock(tmp_path: Path) -> None:
    """A manual CLI run must not overlap a timer run."""
    lock_path = tmp_path / "lenana.lock"
    ctx = multiprocessing.get_context("spawn")
    flag = ctx.Value("i", 0)
    with exclusive_lock(lock_path):
        proc = ctx.Process(target=_child_tries_lock, args=(str(lock_path), flag))
        proc.start()
        proc.join(timeout=20)
    assert flag.value == 2, "the second process should have been refused the lock"


def test_lock_is_reusable_after_release(tmp_path: Path) -> None:
    lock_path = tmp_path / "lenana.lock"
    with exclusive_lock(lock_path):
        pass
    with exclusive_lock(lock_path):
        pass  # no exception = released cleanly


# --------------------------------------------------------------------------
# schema guarantees the pipeline relies on
# --------------------------------------------------------------------------


def test_outbox_dedupe_key_is_unique(session: Session) -> None:
    """Duplicate-alert suppression is a database constraint, not app logic."""
    key = "video_alert:abc123:1:0"
    session.add(Outbox(dedupe_key=key, kind=OutboxKind.VIDEO_ALERT, chat_id="-100", body="x"))
    session.commit()
    session.add(Outbox(dedupe_key=key, kind=OutboxKind.VIDEO_ALERT, chat_id="-100", body="y"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_video_id_is_unique(session: Session) -> None:
    channel = Channel(handle="@A")
    session.add(channel)
    session.commit()
    assert channel.id is not None

    for _ in range(2):
        session.add(
            Video(
                video_id="dupVideoId1",
                channel_pk=channel.id,
                published_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
    with pytest.raises(IntegrityError):
        session.commit()


def test_new_video_starts_in_discovered(session: Session) -> None:
    channel = Channel(handle="@A")
    session.add(channel)
    session.commit()
    assert channel.id is not None

    video = Video(
        video_id="vid00000001", channel_pk=channel.id, published_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    session.add(video)
    session.commit()
    session.refresh(video)
    assert video.state is VideoState.DISCOVERED
    assert video.attempts == 0
    assert video.url == "https://www.youtube.com/watch?v=vid00000001"
    assert video.url_at(125) == "https://www.youtube.com/watch?v=vid00000001&t=125s"


def test_foreign_keys_are_enforced(session: Session) -> None:
    """PRAGMA foreign_keys is off by default in SQLite; our schema needs it on."""
    session.add(
        Video(
            video_id="orphan00001", channel_pk=9999, published_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
