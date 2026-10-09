"""Provider 3: audio download + external Whisper transcription.

**Off by default** (`WHISPER_FALLBACK=false`). It is the only provider that
downloads media, which costs real bandwidth through a residential proxy and
real money per audio-hour, so it is opt-in.

Targets an OpenAI-compatible `/audio/transcriptions` endpoint. The default
`WHISPER_BASE_URL` points at Groq (`whisper-large-v3-turbo`), the cheapest
option per audio-hour; switching to OpenAI or another compatible host is a
base-URL change, not a code change.

Implementation is deferred until the first two providers prove insufficient in
practice. The guard below is the contract: enabling the flag without wiring
the rest produces an explicit, classified failure rather than a crash.
"""

from __future__ import annotations

from ...enums import TranscriptProvider
from .base import FetchedTranscriptData, ProviderUnavailable, TranscriptSource


class WhisperSource(TranscriptSource):
    name = TranscriptProvider.WHISPER

    def is_available(self) -> bool:
        return bool(self.cfg.settings.whisper_fallback and self.cfg.settings.whisper_api_key)

    def fetch(self, video_id: str) -> FetchedTranscriptData:
        if not self.is_available():
            raise ProviderUnavailable(
                "whisper fallback is disabled (set WHISPER_FALLBACK=true and WHISPER_API_KEY)",
                provider=self.name,
            )
        raise ProviderUnavailable(
            "whisper transcription is not implemented yet; it is scheduled for after the "
            "caption-based providers are shown to be insufficient",
            provider=self.name,
        )


__all__ = ["WhisperSource"]
