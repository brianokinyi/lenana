"""Shared HTTP conventions: every outbound request gets a timeout and a
jittered retry policy.

A missing timeout is the classic way a oneshot systemd unit turns into a
process that hangs until the next timer tick overlaps it, so `timeout` is not
optional anywhere in this codebase.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .config import Config, get_config
from .logging import get_logger

log = get_logger("http")

T = TypeVar("T")

#: Status codes worth another attempt. 429 is included because every API we
#: talk to rate-limits, and 408/5xx are transient by definition.
RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def is_retryable(exc: BaseException) -> bool:
    """Transport errors and retryable status codes only.

    A 400 or 404 means the request itself is wrong; retrying it wastes quota
    and delays the real error reaching the logs.
    """
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRY_STATUS
    return False


def retry_http(
    attempts: int | None = None,
    initial: float = 1.0,
    maximum: float = 30.0,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator applying the standard retry policy.

    Jitter matters because four channels are polled in the same tick; without
    it, a rate-limited retry would re-collide on exactly the same schedule.
    """
    n = attempts if attempts is not None else get_config().app.reliability.http_max_attempts

    def log_retry(state: Any) -> None:
        log.warning(
            "retrying http call",
            extra={
                "attempt": state.attempt_number,
                "fn": getattr(state.fn, "__name__", "?"),
                "error": str(state.outcome.exception()) if state.outcome else None,
            },
        )

    return retry(
        stop=stop_after_attempt(n),
        wait=wait_exponential_jitter(initial=initial, max=maximum),
        retry=retry_if_exception(is_retryable),
        before_sleep=log_retry,
        reraise=True,
    )


def build_client(
    cfg: Config | None = None,
    *,
    base_url: str = "",
    headers: dict[str, str] | None = None,
    use_proxy: bool = False,
) -> httpx.Client:
    """An httpx client with an explicit timeout.

    `use_proxy` is opt-in per client: transcript fetching goes through the
    residential proxy, but Telegram and CoinGecko must not — routing those
    through a rotating residential IP would burn proxy bandwidth and make
    Telegram's rate limiting unpredictable.
    """
    cfg = cfg or get_config()
    timeout = httpx.Timeout(cfg.app.reliability.http_timeout_seconds)

    proxy: str | None = None
    if use_proxy:
        proxy = proxy_url_for(cfg)

    return httpx.Client(
        base_url=base_url,
        headers=headers or {},
        timeout=timeout,
        follow_redirects=True,
        proxy=proxy,
    )


def proxy_url_for(cfg: Config | None = None) -> str | None:
    """The outbound proxy URL, or None when proxying is disabled.

    Never log the return value — it embeds credentials. `logging.redact`
    scrubs it if it leaks into an exception message.
    """
    from .config import ProxyProvider

    cfg = cfg or get_config()
    s = cfg.settings
    if s.proxy_provider is ProxyProvider.GENERIC:
        return s.proxy_url or None
    if s.proxy_provider is ProxyProvider.WEBSHARE:
        return f"http://{s.webshare_proxy_username}:{s.webshare_proxy_password}@p.webshare.io:80"
    return None


__all__ = ["RETRY_STATUS", "build_client", "is_retryable", "proxy_url_for", "retry_http"]
