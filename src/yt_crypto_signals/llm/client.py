"""Claude client wrapper: structured extraction plus usage accounting.

Uses `client.messages.parse(output_format=...)`, the SDK's official structured
output helper (anthropic 1.12.1). It generates the JSON schema from the
Pydantic model via `transform_schema`, which sets `additionalProperties:
false`, keeps the full `required` list, and relocates numeric and length
constraints into field descriptions — exactly the schema rules this project
needs, without a hand-rolled generator that would drift from the SDK.

Note on `output_config`: only `format` is sent. `effort` errors on
claude-haiku-4-5, and extraction needs no thinking budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import TypeVar

import anthropic
from anthropic.types import Message
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ..config import Config
from ..logging import get_logger

log = get_logger("llm")

PROMPT_DIR = Path(__file__).parent / "prompts"

T = TypeVar("T", bound=BaseModel)


class ExtractionError(RuntimeError):
    """Claude could not produce a usable structured response."""


@dataclass
class Usage:
    """Token and cost accounting, accumulated across a video's chunks."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    #: None when the model has no configured price; token counts stay valid.
    cost_usd: float | None = None
    requests: int = 0

    def add(self, message: Message, price_input: float | None, price_output: float | None) -> None:
        usage = message.usage
        self.requests += 1
        self.input_tokens += usage.input_tokens or 0
        self.output_tokens += usage.output_tokens or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        if price_input is not None and price_output is not None:
            incremental = (usage.input_tokens or 0) / 1_000_000 * price_input + (
                usage.output_tokens or 0
            ) / 1_000_000 * price_output
            self.cost_usd = (self.cost_usd or 0.0) + incremental

    def merge(self, other: Usage) -> None:
        self.requests += other.requests
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        if other.cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + other.cost_usd

    def as_dict(self) -> dict[str, object]:
        return {
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cost_usd": round(self.cost_usd, 6) if self.cost_usd is not None else None,
        }


@dataclass
class PromptTemplate:
    version: str
    template: Template
    raw: str = field(repr=False, default="")

    def render(self, **values: object) -> str:
        """Render with `$name` placeholders.

        `string.Template` rather than `str.format` so the prompt can contain
        braces (JSON examples, price notation) without escaping them.
        """
        return self.template.safe_substitute(**values)


def load_prompt(version: str) -> PromptTemplate:
    """Load a versioned prompt from disk.

    The version is stored on every extraction run, which is what makes
    `reprocess --prompt-version v2` able to re-run history and compare.
    """
    path = PROMPT_DIR / f"extract_{version}.md"
    if not path.exists():
        available = sorted(p.stem.removeprefix("extract_") for p in PROMPT_DIR.glob("extract_*.md"))
        raise FileNotFoundError(
            f"prompt version {version!r} not found at {path}; available: {available}"
        )
    raw = path.read_text()
    return PromptTemplate(version=version, template=Template(raw), raw=raw)


class ClaudeExtractor:
    """Thin wrapper around one structured-output call."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.model = cfg.settings.anthropic_model
        self.client = anthropic.Anthropic(
            api_key=cfg.settings.anthropic_api_key,
            timeout=float(cfg.app.reliability.http_timeout_seconds * 4),
            max_retries=0,  # tenacity below owns retrying, with jitter
        )
        price = cfg.price_for_model(self.model)
        self.price_input = price.input if price else None
        self.price_output = price.output if price else None
        if price is None:
            log.warning(
                "no price configured for model; cost will be recorded as unknown",
                extra={"model": self.model},
            )

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential_jitter(multiplier=2.0, max=45.0),
        retry=retry_if_exception_type(
            (
                anthropic.RateLimitError,
                anthropic.APIConnectionError,
                anthropic.APITimeoutError,
                anthropic.InternalServerError,
            )
        ),
        reraise=True,
    )
    def _parse(self, prompt: str, output_format: type[T], max_tokens: int) -> tuple[T, Message]:
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=max_tokens,
            output_format=output_format,
            messages=[{"role": "user", "content": prompt}],
        )
        parsed: T | None = None
        for block in response.content:
            if block.type == "text" and getattr(block, "parsed_output", None) is not None:
                candidate = block.parsed_output
                if isinstance(candidate, output_format):
                    parsed = candidate
                break
        if parsed is None:
            raise ExtractionError(
                f"no structured output in response (stop_reason={response.stop_reason})"
            )
        return parsed, response

    def extract(
        self, prompt: str, output_format: type[T], usage: Usage | None = None
    ) -> tuple[T, Usage]:
        """Run one extraction request and account for its tokens."""
        usage = usage if usage is not None else Usage()
        parsed, response = self._parse(
            prompt, output_format, self.cfg.app.extraction.max_output_tokens
        )
        assert isinstance(parsed, output_format)
        if response.stop_reason == "max_tokens":
            # Truncated structured output would be invalid JSON, so parse would
            # already have failed; log it because it means the chunk is too big.
            log.warning("response hit max_tokens", extra={"model": self.model})
        if response.stop_reason == "refusal":
            raise ExtractionError("model refused the request")
        usage.add(response, self.price_input, self.price_output)
        return parsed, usage


__all__ = [
    "ClaudeExtractor",
    "ExtractionError",
    "PromptTemplate",
    "Usage",
    "load_prompt",
]
