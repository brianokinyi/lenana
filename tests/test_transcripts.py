"""Transcript provider failure classification and caption parsing.

All external calls are mocked: the point is that each provider's distinct
error surface collapses to the right one of four classifications, since that
decision drives whether a video is retried or abandoned.
"""

from __future__ import annotations

import pytest
from youtube_transcript_api._errors import (
    AgeRestricted,
    InvalidVideoId,
    IpBlocked,
    NoTranscriptFound,
    PoTokenRequired,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeDataUnparsable,
    YouTubeRequestFailed,
)

from yt_crypto_signals.config import Config, ProxyProvider
from yt_crypto_signals.enums import FailureKind, TranscriptProvider
from yt_crypto_signals.sources.transcripts.base import (
    FetchedTranscriptData,
    TranscriptFailure,
    TranscriptSegment,
    classify_message,
)
from yt_crypto_signals.sources.transcripts.registry import fetch_transcript
from yt_crypto_signals.sources.transcripts.whisper import WhisperSource
from yt_crypto_signals.sources.transcripts.ytapi import (
    build_proxy_config,
    classify_exception,
)
from yt_crypto_signals.sources.transcripts.ytdlp import parse_json3, parse_vtt

VIDEO_ID = "n3g_L9QI-9k"


# --------------------------------------------------------------------------
# youtube-transcript-api: exception type -> classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (IpBlocked(VIDEO_ID), FailureKind.BLOCKED),
        (RequestBlocked(VIDEO_ID), FailureKind.BLOCKED),
        # A PO token is required: yt-dlp with bgutil may still succeed, so this
        # must stay retryable rather than terminal.
        (PoTokenRequired(VIDEO_ID), FailureKind.BLOCKED),
        (TranscriptsDisabled(VIDEO_ID), FailureKind.NO_CAPTIONS),
        (NoTranscriptFound(VIDEO_ID, ["en"], {}), FailureKind.NO_CAPTIONS),
        (AgeRestricted(VIDEO_ID), FailureKind.UNAVAILABLE),
        (InvalidVideoId(VIDEO_ID), FailureKind.UNAVAILABLE),
        (VideoUnavailable(VIDEO_ID), FailureKind.UNAVAILABLE),
        (VideoUnplayable(VIDEO_ID, "reason", []), FailureKind.UNAVAILABLE),
        (YouTubeRequestFailed(VIDEO_ID, Exception("500")), FailureKind.TRANSIENT),
        (YouTubeDataUnparsable(VIDEO_ID), FailureKind.TRANSIENT),
    ],
)
def test_library_exceptions_are_classified(exc: Exception, expected: FailureKind) -> None:
    assert classify_exception(exc) is expected


def test_ip_blocked_subclass_order_matters() -> None:
    """IpBlocked subclasses RequestBlocked, so the mapping must be checked
    most-specific-first."""
    assert issubclass(IpBlocked, RequestBlocked)
    assert classify_exception(IpBlocked(VIDEO_ID)) is FailureKind.BLOCKED


def test_unknown_exception_is_transient() -> None:
    assert classify_exception(RuntimeError("something odd")) is FailureKind.TRANSIENT


def test_only_blocked_and_transient_are_retried() -> None:
    assert FailureKind.BLOCKED.is_retryable
    assert FailureKind.TRANSIENT.is_retryable
    assert not FailureKind.NO_CAPTIONS.is_retryable
    assert not FailureKind.UNAVAILABLE.is_retryable


# --------------------------------------------------------------------------
# yt-dlp: message text -> classification (no exception type to match on)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: [youtube] abc: Sign in to confirm you're not a bot",
        # YouTube uses a curly apostrophe in the real message, which is exactly
        # why the patterns match on "not a bot" rather than the whole phrase.
        "ERROR: [youtube] abc: Sign in to confirm you’re not a bot",  # noqa: RUF001
        "HTTP Error 429: Too Many Requests",
        "The uploader has not made this video available in your country",
    ],
)
def test_bot_detection_messages_are_blocked(message: str, config: Config) -> None:
    assert classify_message(message, config) is FailureKind.BLOCKED


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: [youtube] abc: Video unavailable",
        "ERROR: [youtube] abc: Private video. Sign in if you've been granted access",
        "This video has been removed by the uploader",
        "Join this channel to get access to members-only content",
    ],
)
def test_unavailable_messages_are_classified(message: str, config: Config) -> None:
    assert classify_message(message, config) is FailureKind.UNAVAILABLE


def test_no_caption_messages_are_classified(config: Config) -> None:
    assert classify_message("There are no subtitles for the requested languages", config) is (
        FailureKind.NO_CAPTIONS
    )


def test_unrecognised_message_is_transient(config: Config) -> None:
    assert classify_message("connection reset by peer", config) is FailureKind.TRANSIENT


def test_specific_classifications_win_over_blocked(config: Config) -> None:
    """A blocked response can also mention 'unavailable'; the more specific
    conclusion about the video should win."""
    message = "Video unavailable. Sign in to confirm you're not a bot"
    assert classify_message(message, config) is FailureKind.UNAVAILABLE


def test_patterns_come_from_config(config: Config) -> None:
    config.app.transcripts.blocked_message_patterns = ["brand new wording"]
    assert classify_message("some brand new wording here", config) is FailureKind.BLOCKED
    assert classify_message("sign in to confirm", config) is FailureKind.TRANSIENT


# --------------------------------------------------------------------------
# caption parsing
# --------------------------------------------------------------------------


def test_parse_json3_extracts_timed_segments() -> None:
    payload = """
    {"events": [
      {"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "hello "}, {"utf8": "world"}]},
      {"tStartMs": 2000, "dDurationMs": 1500, "segs": [{"utf8": "sol to 300"}]}
    ]}
    """
    segments = parse_json3(payload)
    assert [(s.start, s.text) for s in segments] == [(0.0, "hello world"), (2.0, "sol to 300")]
    assert segments[0].duration == 2.0


def test_parse_json3_skips_non_text_events() -> None:
    """Window and style directives carry no `segs` and must be ignored."""
    payload = """
    {"events": [
      {"tStartMs": 0, "wWinId": 1},
      {"tStartMs": 100, "dDurationMs": 10, "segs": [{"utf8": "\\n"}]},
      {"tStartMs": 500, "dDurationMs": 1000, "segs": [{"utf8": "real text"}]}
    ]}
    """
    segments = parse_json3(payload)
    assert [s.text for s in segments] == ["real text"]


def test_parse_json3_rejects_malformed_json() -> None:
    import json

    with pytest.raises(json.JSONDecodeError):
        parse_json3("{not json")


def test_parse_vtt_extracts_cues() -> None:
    payload = """WEBVTT

00:00:00.000 --> 00:00:02.000
hello world

00:00:02.000 --> 00:00:04.500
sol to 300
"""
    segments = parse_vtt(payload)
    assert [(s.start, s.text) for s in segments] == [(0.0, "hello world"), (2.0, "sol to 300")]
    assert segments[1].duration == 2.5


def test_parse_vtt_collapses_rolling_duplicates() -> None:
    """Auto-generated VTT repeats each line as a rolling caption; keeping the
    repeats would multiply extraction cost."""
    payload = """WEBVTT

00:00:00.000 --> 00:00:02.000
first line

00:00:02.000 --> 00:00:04.000
first line

00:00:04.000 --> 00:00:06.000
second line
"""
    assert [s.text for s in parse_vtt(payload)] == ["first line", "second line"]


def test_parse_vtt_strips_inline_tags() -> None:
    payload = """WEBVTT

00:00:00.000 --> 00:00:02.000
<c.colorE5E5E5>tagged text</c>
"""
    assert parse_vtt(payload)[0].text == "tagged text"


# --------------------------------------------------------------------------
# proxy configuration
# --------------------------------------------------------------------------


def test_no_proxy_configured_returns_none(config: Config) -> None:
    config.settings.proxy_provider = ProxyProvider.NONE
    assert build_proxy_config(config) is None


def test_webshare_proxy_is_built_with_pinned_retries(config: Config) -> None:
    config.settings.proxy_provider = ProxyProvider.WEBSHARE
    config.settings.webshare_proxy_username = "user"
    config.settings.webshare_proxy_password = "pass"
    config.app.transcripts.webshare_retries_when_blocked = 2
    proxy = build_proxy_config(config)
    assert proxy is not None
    # Pinned low so it does not multiply with our own retry ladder.
    assert proxy.retries_when_blocked == 2


def test_generic_proxy_is_built(config: Config) -> None:
    config.settings.proxy_provider = ProxyProvider.GENERIC
    config.settings.proxy_url = "http://127.0.0.1:8888"
    proxy = build_proxy_config(config)
    assert proxy is not None
    assert "127.0.0.1:8888" in str(proxy.to_requests_dict())


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------


class StubSource:
    def __init__(self, name: TranscriptProvider, failure: TranscriptFailure | None = None) -> None:
        self.name = name
        self.failure = failure
        self.calls = 0

    def is_available(self) -> bool:
        return True

    def fetch(self, video_id: str) -> FetchedTranscriptData:
        self.calls += 1
        if self.failure:
            raise self.failure
        return FetchedTranscriptData(
            video_id=video_id,
            provider=self.name,
            segments=[TranscriptSegment(start=0.0, duration=1.0, text="ok")],
        )


def test_first_success_short_circuits(config: Config) -> None:
    first = StubSource(TranscriptProvider.YOUTUBE_TRANSCRIPT_API)
    second = StubSource(TranscriptProvider.YT_DLP)
    outcome = fetch_transcript(VIDEO_ID, config, sources=[first, second])  # type: ignore[arg-type]
    assert outcome.ok
    assert second.calls == 0, "the fallback must not run after a success"


def test_falls_through_to_the_next_provider(config: Config) -> None:
    blocked = StubSource(
        TranscriptProvider.YOUTUBE_TRANSCRIPT_API,
        TranscriptFailure(FailureKind.BLOCKED, "blocked"),
    )
    working = StubSource(TranscriptProvider.YT_DLP)
    outcome = fetch_transcript(VIDEO_ID, config, sources=[blocked, working])  # type: ignore[arg-type]
    assert outcome.ok
    assert working.calls == 1
    assert len(outcome.attempts) == 2
    assert outcome.attempts[0].kind is FailureKind.BLOCKED


def test_most_informative_classification_wins(config: Config) -> None:
    """A definite answer about the video beats an inability to reach it, so the
    video is skipped rather than retried pointlessly."""
    transient = StubSource(
        TranscriptProvider.YOUTUBE_TRANSCRIPT_API,
        TranscriptFailure(FailureKind.TRANSIENT, "timeout"),
    )
    no_captions = StubSource(
        TranscriptProvider.YT_DLP, TranscriptFailure(FailureKind.NO_CAPTIONS, "none")
    )
    outcome = fetch_transcript(VIDEO_ID, config, sources=[transient, no_captions])  # type: ignore[arg-type]
    assert not outcome.ok
    assert outcome.failure is not None
    assert outcome.failure.kind is FailureKind.NO_CAPTIONS


def test_unavailable_outranks_no_captions(config: Config) -> None:
    a = StubSource(
        TranscriptProvider.YOUTUBE_TRANSCRIPT_API,
        TranscriptFailure(FailureKind.NO_CAPTIONS, "none"),
    )
    b = StubSource(TranscriptProvider.YT_DLP, TranscriptFailure(FailureKind.UNAVAILABLE, "private"))
    outcome = fetch_transcript(VIDEO_ID, config, sources=[a, b])  # type: ignore[arg-type]
    assert outcome.failure is not None
    assert outcome.failure.kind is FailureKind.UNAVAILABLE


def test_no_providers_yields_a_transient_failure(config: Config) -> None:
    outcome = fetch_transcript(VIDEO_ID, config, sources=[])
    assert not outcome.ok
    assert outcome.failure is not None
    assert outcome.failure.kind is FailureKind.TRANSIENT


def test_whisper_is_unavailable_when_disabled(config: Config) -> None:
    config.settings.whisper_fallback = False
    source = WhisperSource(config)
    assert not source.is_available()
    with pytest.raises(TranscriptFailure) as exc:
        source.fetch(VIDEO_ID)
    assert exc.value.kind is FailureKind.TRANSIENT
