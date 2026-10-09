"""Configuration: secrets from .env, everything else from config.yaml.

Validated once at startup so a misconfigured VPS fails immediately with a
readable message instead of halfway through a pipeline run.
"""

from __future__ import annotations

import functools
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"

PositiveInt = Annotated[int, Field(gt=0)]
NonNegInt = Annotated[int, Field(ge=0)]


class ProxyProvider(StrEnum):
    WEBSHARE = "webshare"
    GENERIC = "generic"
    NONE = "none"


# --------------------------------------------------------------------------
# .env  (secrets + per-host behaviour)
# --------------------------------------------------------------------------


class Settings(BaseSettings):
    """Secrets and host-specific settings. Never log an instance of this."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # Without this, a ValidationError repr includes the offending input
        # dict — i.e. the secrets — and startup errors go straight to journald.
        hide_input_in_errors=True,
    )

    anthropic_api_key: str
    anthropic_model: str = "claude-haiku-4-5"

    youtube_api_key: str

    telegram_bot_token: str
    telegram_chat_id: str

    proxy_provider: ProxyProvider = ProxyProvider.NONE
    webshare_proxy_username: str = ""
    webshare_proxy_password: str = ""
    proxy_url: str = ""

    coingecko_api_key: str = ""

    display_tz: str = "UTC"
    healthcheck_url: str = ""

    whisper_fallback: bool = False
    whisper_api_key: str = ""
    whisper_base_url: str = "https://api.groq.com/openai/v1"
    whisper_model: str = "whisper-large-v3-turbo"

    data_dir: Path = REPO_ROOT / "data"

    @field_validator("display_tz")
    @classmethod
    def _tz_must_exist(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"DISPLAY_TZ={v!r} is not a valid IANA timezone (e.g. 'Africa/Nairobi', 'UTC')"
            ) from exc
        return v

    @model_validator(mode="after")
    def _proxy_credentials_present(self) -> Settings:
        if self.proxy_provider is ProxyProvider.WEBSHARE and not (
            self.webshare_proxy_username and self.webshare_proxy_password
        ):
            raise ValueError(
                "PROXY_PROVIDER=webshare requires WEBSHARE_PROXY_USERNAME and "
                "WEBSHARE_PROXY_PASSWORD. Use the 'Residential' plan credentials "
                "from https://dashboard.webshare.io/proxy/settings"
            )
        if self.proxy_provider is ProxyProvider.GENERIC and not self.proxy_url:
            raise ValueError("PROXY_PROVIDER=generic requires PROXY_URL")
        return self

    @model_validator(mode="after")
    def _whisper_credentials_present(self) -> Settings:
        if self.whisper_fallback and not self.whisper_api_key:
            raise ValueError("WHISPER_FALLBACK=true requires WHISPER_API_KEY")
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.display_tz)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ycs.db"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "ycs.lock"


# --------------------------------------------------------------------------
# config.yaml  (behaviour, thresholds, channels)
# --------------------------------------------------------------------------


class ChannelConfig(BaseModel):
    handle: str
    default_host: str = "Host"
    enabled: bool = True

    @field_validator("handle")
    @classmethod
    def _handle_shape(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("@"):
            raise ValueError(f"channel handle must start with '@': {v!r}")
        return v


class DiscoveryConfig(BaseModel):
    backfill_days: PositiveInt = 30
    min_duration_seconds: NonNegInt = 180
    livestream_settle_hours: NonNegInt = 2
    max_videos_per_run: PositiveInt = 25


class TranscriptsConfig(BaseModel):
    languages: list[str] = ["en", "en-US", "en-GB"]
    provider_order: list[str] = ["youtube_transcript_api", "yt_dlp"]
    webshare_retries_when_blocked: NonNegInt = 2
    webshare_ip_locations: list[str] = []
    ytdlp_subtitle_format: str = "json3"
    ytdlp_po_token_provider_url: str = "http://127.0.0.1:4416"
    blocked_message_patterns: list[str] = []
    unavailable_message_patterns: list[str] = []
    no_captions_message_patterns: list[str] = []

    @field_validator("languages")
    @classmethod
    def _at_least_one_language(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("transcripts.languages must list at least one language code")
        return v


class ModelPrice(BaseModel):
    input: float = Field(ge=0)
    output: float = Field(ge=0)


class ExtractionConfig(BaseModel):
    prompt_version: str = "v1"
    chunk_minutes: PositiveInt = 20
    chunk_overlap_seconds: NonNegInt = 60
    max_output_tokens: PositiveInt = 8000
    model_prices_usd_per_mtok: dict[str, ModelPrice] = {}

    @model_validator(mode="after")
    def _overlap_fits_in_chunk(self) -> ExtractionConfig:
        if self.chunk_overlap_seconds >= self.chunk_minutes * 60:
            raise ValueError(
                "extraction.chunk_overlap_seconds must be smaller than the chunk length "
                f"({self.chunk_minutes} min = {self.chunk_minutes * 60}s)"
            )
        return self


class VerificationConfig(BaseModel):
    fuzzy_threshold: int = Field(default=85, ge=0, le=100)
    quote_window_seconds: NonNegInt = 90
    min_quote_chars: NonNegInt = 12


class EnrichmentConfig(BaseModel):
    coin_list_size: PositiveInt = 1000
    coin_list_refresh_hours: PositiveInt = 24
    entry_price_at_call_timestamp: bool = True


class CrosscheckConfig(BaseModel):
    window_days: PositiveInt = 5
    recompute_nightly: bool = True


class ScoringConfig(BaseModel):
    default_timeframe_days: PositiveInt = 30
    hit_threshold_pct: float = Field(default=5.0, gt=0)
    ohlc_days_buckets: list[PositiveInt] = [1, 7, 14, 30, 90, 180, 365]


class ScorecardConfig(BaseModel):
    min_scored_for_rank: PositiveInt = 10


class AlertsConfig(BaseModel):
    max_message_chars: PositiveInt = 4096
    send_rate_per_second: PositiveInt = 1
    outbox_retry_ladder_minutes: list[NonNegInt] = [1, 5, 15, 60, 240]
    outbox_max_attempts: PositiveInt = 6


class ReliabilityConfig(BaseModel):
    transcript_retry_ladder_minutes: list[NonNegInt] = [15, 60, 240, 720, 1440]
    http_timeout_seconds: PositiveInt = 30
    http_max_attempts: PositiveInt = 4
    stage_batch_size: PositiveInt = 10

    @field_validator("transcript_retry_ladder_minutes")
    @classmethod
    def _ladder_not_empty(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError(
                "reliability.transcript_retry_ladder_minutes must have at least one rung; "
                "it defines how many times a transcript is retried before giving up"
            )
        return v


class HealthConfig(BaseModel):
    transcript_failure_alert_hours: PositiveInt = 6
    youtube_quota_daily_units: PositiveInt = 10000
    youtube_quota_warn_pct: int = Field(default=80, ge=0, le=100)
    backup_retention_days: PositiveInt = 14


class AppConfig(BaseModel):
    """Parsed config.yaml. Rejects unknown keys so typos surface at startup."""

    model_config = {"extra": "forbid", "hide_input_in_errors": True}

    channels: list[ChannelConfig]
    discovery: DiscoveryConfig = DiscoveryConfig()
    transcripts: TranscriptsConfig = TranscriptsConfig()
    extraction: ExtractionConfig = ExtractionConfig()
    verification: VerificationConfig = VerificationConfig()
    enrichment: EnrichmentConfig = EnrichmentConfig()
    crosscheck: CrosscheckConfig = CrosscheckConfig()
    scoring: ScoringConfig = ScoringConfig()
    scorecard: ScorecardConfig = ScorecardConfig()
    alerts: AlertsConfig = AlertsConfig()
    reliability: ReliabilityConfig = ReliabilityConfig()
    health: HealthConfig = HealthConfig()

    @field_validator("channels")
    @classmethod
    def _channels_sane(cls, v: list[ChannelConfig]) -> list[ChannelConfig]:
        if not v:
            raise ValueError("config.yaml must define at least one channel")
        handles = [c.handle.lower() for c in v]
        dupes = {h for h in handles if handles.count(h) > 1}
        if dupes:
            raise ValueError(f"duplicate channel handles in config.yaml: {sorted(dupes)}")
        return v

    @property
    def enabled_channels(self) -> list[ChannelConfig]:
        return [c for c in self.channels if c.enabled]


class Config(BaseModel):
    """Everything the application needs, validated."""

    model_config = {"arbitrary_types_allowed": True, "hide_input_in_errors": True}

    settings: Settings
    app: AppConfig

    def price_for_model(self, model: str) -> ModelPrice | None:
        """Token prices for cost accounting. A missing price is not fatal: token
        counts are still recorded and cost is reported as unknown."""
        return self.app.extraction.model_prices_usd_per_mtok.get(model)


def format_config_error(exc: Exception) -> str:
    """Render a configuration failure without echoing any input values.

    `hide_input_in_errors` covers the pydantic repr; this additionally flattens
    the error list into one actionable line per problem and runs the result
    through the log redactor as a belt-and-braces measure.
    """
    from pydantic import ValidationError

    from .logging import redact

    if isinstance(exc, ValidationError):
        lines = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "<root>"
            lines.append(f"  - {loc}: {err['msg']}")
        return redact("Configuration invalid:\n" + "\n".join(lines))
    return redact(f"Configuration invalid: {exc}")


def load_app_config(path: Path | None = None) -> AppConfig:
    cfg_path = path or DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(f"config.yaml not found at {cfg_path}")
    raw: Any = yaml.safe_load(cfg_path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{cfg_path} must contain a YAML mapping at the top level")
    return AppConfig.model_validate(raw)


@functools.lru_cache(maxsize=1)
def get_config() -> Config:
    """Load and validate configuration once per process."""
    settings = Settings()  # type: ignore[call-arg]  # values come from .env
    app = load_app_config()
    cfg = Config(settings=settings, app=app)
    cfg.settings.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def describe_config(cfg: Config) -> dict[str, object]:
    """A redacted summary safe to log or print. Secrets never appear."""
    s = cfg.settings
    return {
        "model": s.anthropic_model,
        "display_tz": s.display_tz,
        "proxy_provider": s.proxy_provider.value,
        "whisper_fallback": s.whisper_fallback,
        "healthcheck_configured": bool(s.healthcheck_url),
        "coingecko_key_configured": bool(s.coingecko_api_key),
        "data_dir": str(s.data_dir),
        "db_path": str(s.db_path),
        "channels": [c.handle for c in cfg.app.enabled_channels],
        "prompt_version": cfg.app.extraction.prompt_version,
    }


__all__ = [
    "AppConfig",
    "ChannelConfig",
    "Config",
    "ProxyProvider",
    "Settings",
    "describe_config",
    "format_config_error",
    "get_config",
    "load_app_config",
]
