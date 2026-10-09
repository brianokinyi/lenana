"""Telegram Bot API transport. One concern only: putting a message on the wire.

Queueing, retrying and deduplication belong to `outbox.py`. This module knows
how to send one message and how to describe what happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import Config
from ..httpc import build_client
from ..logging import get_logger

log = get_logger("telegram")

API_BASE = "https://api.telegram.org"


@dataclass
class ChatRef:
    chat_id: int
    chat_type: str
    name: str


@dataclass
class Identity:
    ok: bool
    bot_id: int | None = None
    username: str | None = None
    error: str | None = None


@dataclass
class SendResult:
    ok: bool
    status_code: int | None = None
    error: str | None = None
    #: Seconds Telegram asked us to wait, from a 429 `retry_after`.
    retry_after: int | None = None
    message_id: int | None = None

    @property
    def is_retryable(self) -> bool:
        """429 and 5xx are worth another attempt; 400 means the message itself
        is malformed and retrying it forever would wedge the outbox."""
        if self.retry_after is not None:
            return True
        if self.status_code is None:
            return True  # transport failure
        return self.status_code >= 500


class TelegramClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._token = cfg.settings.telegram_bot_token
        self.default_chat_id = cfg.settings.telegram_chat_id

    def _url(self, method: str) -> str:
        return f"{API_BASE}/bot{self._token}/{method}"

    def send_message(
        self, text: str, chat_id: str | None = None, disable_preview: bool = True
    ) -> SendResult:
        """Send one HTML message.

        Not proxied: routing Telegram through a rotating residential IP would
        waste proxy bandwidth and make its rate limiting unpredictable.
        """
        payload = {
            "chat_id": chat_id or self.default_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": disable_preview},
        }
        try:
            with build_client(self.cfg, use_proxy=False) as client:
                response = client.post(self._url("sendMessage"), json=payload)
        except httpx.HTTPError as exc:
            log.warning("telegram transport error", extra={"error": str(exc)})
            return SendResult(ok=False, error=str(exc))

        if response.status_code == 200:
            body = response.json()
            return SendResult(
                ok=True,
                status_code=200,
                message_id=(body.get("result") or {}).get("message_id"),
            )

        retry_after: int | None = None
        description = response.text
        try:
            body = response.json()
            description = body.get("description") or description
            retry_after = (body.get("parameters") or {}).get("retry_after")
        except ValueError:
            pass

        log.warning(
            "telegram send failed",
            extra={
                "status": response.status_code,
                "error": description[:300],
                "retry_after": retry_after,
            },
        )
        return SendResult(
            ok=False,
            status_code=response.status_code,
            error=description[:500],
            retry_after=retry_after,
        )

    def get_me(self) -> Identity:
        """Verify the bot token without sending anything."""
        try:
            with build_client(self.cfg, use_proxy=False) as client:
                response = client.get(self._url("getMe"))
        except httpx.HTTPError as exc:
            return Identity(ok=False, error=str(exc))
        if response.status_code != 200:
            return Identity(ok=False, error=response.text[:500])
        result = response.json().get("result") or {}
        return Identity(ok=True, bot_id=result.get("id"), username=result.get("username"))

    def send_document(self, path: Path, caption: str, chat_id: str | None = None) -> SendResult:
        """Upload a report; errors use the same outbox retry policy as messages."""
        try:
            with path.open("rb") as document, build_client(self.cfg) as client:
                response = client.post(
                    self._url("sendDocument"),
                    data={"chat_id": chat_id or self.default_chat_id, "caption": caption[:1024]},
                    files={
                        "document": (
                            path.name,
                            document,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
                    },
                )
        except OSError as exc:
            return SendResult(ok=False, status_code=400, error=f"Report unavailable: {exc}")
        except httpx.HTTPError as exc:
            return SendResult(ok=False, error=str(exc))
        try:
            body = response.json()
        except ValueError:
            return SendResult(
                ok=False,
                status_code=response.status_code,
                error="Telegram returned a non-JSON document response",
            )
        if response.status_code == 200 and body.get("ok"):
            return SendResult(
                ok=True, status_code=200, message_id=(body.get("result") or {}).get("message_id")
            )
        return SendResult(
            ok=False,
            status_code=response.status_code,
            error=str(body.get("description", "Document upload failed"))[:500],
            retry_after=(body.get("parameters") or {}).get("retry_after"),
        )

    def discover_chats(self) -> list[ChatRef]:
        """Chats that have interacted with the bot, via getUpdates.

        The chat id is the one piece of Telegram setup that cannot be read
        from a token: someone has to message the bot first. This turns
        "find your chat id" from a documentation step into a command.
        """
        try:
            with build_client(self.cfg, use_proxy=False) as client:
                response = client.get(self._url("getUpdates"))
        except httpx.HTTPError as exc:
            log.warning("getUpdates failed", extra={"error": str(exc)})
            return []
        if response.status_code != 200:
            return []

        found: dict[int, ChatRef] = {}
        for update in response.json().get("result") or []:
            for key in ("message", "edited_message", "channel_post", "my_chat_member"):
                chat = (update.get(key) or {}).get("chat")
                if not chat:
                    continue
                found[chat["id"]] = ChatRef(
                    chat_id=chat["id"],
                    chat_type=chat.get("type", "?"),
                    name=chat.get("title") or chat.get("username") or chat.get("first_name") or "",
                )
        return sorted(found.values(), key=lambda c: c.chat_id)


__all__ = ["ChatRef", "Identity", "SendResult", "TelegramClient"]
