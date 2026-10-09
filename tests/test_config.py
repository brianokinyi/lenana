"""Configuration loading, validation and redaction of the shipped config."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from yt_crypto_signals.config import (
    DEFAULT_BLOCKED_PATTERNS,
    AppConfig,
    Config,
    ProxyProvider,
    Settings,
    describe_config,
    format_config_error,
    load_app_config,
)
from yt_crypto_signals.enums import FailureKind
from yt_crypto_signals.sources.transcripts.base import classify_message


def test_shipped_config_loads() -> None:
    cfg = load_app_config()
    assert len(cfg.enabled_channels) == 4
    assert all(c.default_host for c in cfg.channels), "every channel needs a host label"


def test_classification_works_with_shipped_config() -> None:
    """The real regression this guards: empty pattern lists would silently make
    every block 'transient' and retry it forever."""
    app = load_app_config()
    settings = Settings(
        anthropic_api_key="k",
        youtube_api_key="k",
        telegram_bot_token="k",
        telegram_chat_id="1",
        _env_file=None,  # type: ignore[call-arg]
    )
    cfg = Config(settings=settings, app=app)
    assert classify_message("Sign in to confirm you are not a bot", cfg) is FailureKind.BLOCKED
    assert classify_message("Video unavailable", cfg) is FailureKind.UNAVAILABLE


def test_defaults_are_populated_when_config_omits_them() -> None:
    app = AppConfig.model_validate({"channels": [{"handle": "@A"}]})
    assert app.transcripts.blocked_message_patterns == DEFAULT_BLOCKED_PATTERNS
    assert app.transcripts.no_captions_message_patterns


def test_config_can_override_defaults() -> None:
    app = AppConfig.model_validate(
        {"channels": [{"handle": "@A"}], "transcripts": {"blocked_message_patterns": ["xyz"]}}
    )
    assert app.transcripts.blocked_message_patterns == ["xyz"]


def test_describe_config_hides_every_secret(config: Config) -> None:
    config.settings.webshare_proxy_password = "super-secret-pw"
    rendered = str(describe_config(config))
    assert "super-secret-pw" not in rendered
    assert "sk-ant-test" not in rendered
    assert "AIzaTESTKEY" not in rendered
    # Non-secret operational facts should still be visible.
    assert "claude-haiku-4-5" in rendered


def test_format_config_error_is_readable_and_safe() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(
            anthropic_api_key="sk-ant-should-not-appear",
            youtube_api_key="k",
            telegram_bot_token="k",
            telegram_chat_id="1",
            proxy_provider=ProxyProvider.WEBSHARE,
            _env_file=None,  # type: ignore[call-arg]
        )
    rendered = format_config_error(exc.value)
    assert "WEBSHARE_PROXY_USERNAME" in rendered
    assert "sk-ant-should-not-appear" not in rendered


def test_price_lookup_for_configured_model() -> None:
    app = load_app_config()
    price = app.extraction.model_prices_usd_per_mtok.get("claude-haiku-4-5")
    assert price is not None
    assert (price.input, price.output) == (1.00, 5.00)


def test_missing_price_is_not_fatal(config: Config) -> None:
    """Token counts must still be recorded for an unpriced model."""
    assert config.price_for_model("some-unreleased-model") is None
