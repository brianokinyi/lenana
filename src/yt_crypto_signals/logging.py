"""Structured JSON logging to stdout, which systemd captures into journald.

The redaction filter is the important part: proxy URLs carry credentials in
their userinfo component, and an exception from `requests` or `yt-dlp` will
happily include the full URL in its message. Redaction happens at the logging
boundary so no individual call site has to remember.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from typing import Any

#: Substituted for anything that looks like a secret.
MASK = "***"

#: user:pass@host in any URL -> ***@host
_USERINFO_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
#: Long opaque tokens: Anthropic, Telegram bot tokens, Google keys, CoinGecko.
_TOKEN_RES = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_\-]{30,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),
    re.compile(r"\bCG-[A-Za-z0-9]{10,}\b"),
)

#: Env vars whose values must never appear in a log line, whatever their shape.
_SECRET_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "YOUTUBE_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "WEBSHARE_PROXY_USERNAME",
    "WEBSHARE_PROXY_PASSWORD",
    "PROXY_URL",
    "COINGECKO_API_KEY",
    "WHISPER_API_KEY",
    "HEALTHCHECK_URL",
)


def redact(text: str) -> str:
    """Remove credentials from a string before it reaches a log sink."""
    if not text:
        return text
    out = _USERINFO_RE.sub(lambda m: f"{m.group('scheme')}{MASK}@", text)
    for pattern in _TOKEN_RES:
        out = pattern.sub(MASK, out)
    # Catch-all: literal values from the environment, which covers secrets whose
    # shape the patterns above do not anticipate.
    for var in _SECRET_ENV_VARS:
        value = os.environ.get(var)
        if value and len(value) >= 6:
            out = out.replace(value, MASK)
    return out


class RedactingJsonFormatter(logging.Formatter):
    """One JSON object per line, with every string value redacted."""

    _RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
        "message",
        "asctime",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        # Anything passed via logger.info("...", extra={...}) rides along.
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = redact(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = redact(record.stack_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    """Readable output for interactive CLI use. Also redacted."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, UTC).strftime("%H:%M:%S")
        base = f"{ts} {record.levelname:<7} {redact(record.getMessage())}"
        if record.exc_info:
            base += "\n" + redact(self.formatException(record.exc_info))
        return base


def setup_logging(level: str = "INFO", json_output: bool | None = None) -> None:
    """Install the root handler.

    Defaults to JSON when stdout is not a TTY — i.e. JSON under systemd,
    human-readable when a person runs the CLI.
    """
    if json_output is None:
        json_output = not sys.stdout.isatty()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingJsonFormatter() if json_output else HumanFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # These are chatty at INFO and tell us nothing we do not already log.
    for noisy in (
        "httpx",
        "httpcore",
        "urllib3",
        "googleapiclient.discovery_cache",
        "alembic.runtime.plugins",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ycs.{name}")


__all__ = ["HumanFormatter", "RedactingJsonFormatter", "get_logger", "redact", "setup_logging"]
