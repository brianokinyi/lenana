"""Shared fixtures.

Every test runs against a throwaway SQLite file and a fully synthetic config,
so the suite never touches the real database or reads the developer's .env.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlmodel import Session

from yt_crypto_signals import db as db_module
from yt_crypto_signals.config import AppConfig, ChannelConfig, Config, Settings


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's real .env and secrets out of every test."""
    for var in list(os.environ):
        if var.startswith(
            ("ANTHROPIC_", "YOUTUBE_", "TELEGRAM_", "WEBSHARE_", "PROXY_", "COINGECKO_", "WHISPER_")
        ):
            monkeypatch.delenv(var, raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        anthropic_api_key="sk-ant-test",
        youtube_api_key="AIzaTESTKEY",
        telegram_bot_token="111111:TESTTOKEN",
        telegram_chat_id="-100123",
        display_tz="UTC",
        data_dir=tmp_path,
        _env_file=None,  # type: ignore[call-arg]
    )


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig(channels=[ChannelConfig(handle="@TestChannel", default_host="Test Host")])


@pytest.fixture
def config(settings: Settings, app_config: AppConfig) -> Config:
    return Config(settings=settings, app=app_config)


@pytest.fixture
def engine(config: Config) -> Iterator[Engine]:
    """A fresh database per test, created from the models."""
    eng = db_module.build_engine(config.settings.db_path)
    db_module.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as s:
        yield s
