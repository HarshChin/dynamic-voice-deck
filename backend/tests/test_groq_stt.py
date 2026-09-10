"""Speech-to-text provider (TR-030, TR-081, TR-085)."""

from __future__ import annotations

import struct
import wave
from io import BytesIO
from typing import Any

import httpx
import pytest
from app.errors import ProviderError
from app.providers.groq_stt import (
    MAX_ATTEMPTS,
    MIN_UTTERANCE_BYTES,
    PROVIDER_NAME,
    GroqWhisperSTT,
    pcm16_to_wav,
)

SAMPLE_RATE = 16_000
"""Rate the browser sends, and the only one this provider is asked for."""


def speech_like(seconds: float = 1.0) -> bytes:
    """Build PCM16 long enough to be accepted as an utterance.

    Args:
        seconds: Duration of the audio.

    Returns:
        Little-endian 16-bit mono samples.
    """
    count = int(SAMPLE_RATE * seconds)
    return struct.pack(f"<{count}h", *([1000] * count))


def build(handler: Any, **kwargs: Any) -> GroqWhisperSTT:
    """Build a provider whose transport is a stub.

    Args:
        handler: Called with each request; returns the response.
        **kwargs: Overrides for the constructor.

    Returns:
        A provider that never touches the network.
    """
    provider = GroqWhisperSTT(
        api_key=kwargs.pop("api_key", "test-key"),
        base_url=kwargs.pop("base_url", "https://api.example.test/v1"),
        model=kwargs.pop("model", "whisper-large-v3-turbo"),
    )
    # Only the transport is swapped; the base URL and auth header stay as the
    # provider built them, so the request under test is the real one.
    provider._client = httpx.AsyncClient(
        base_url=str(provider._client.base_url),
        headers=provider._client.headers,
        transport=httpx.MockTransport(handler),
    )
    return provider


# --------------------------------------------------------------------------- #
# The WAV container (TR-030)
# --------------------------------------------------------------------------- #


def test_the_wav_header_describes_the_audio_it_wraps() -> None:
    """TC-BE-260: Whisper needs a container to know the rate; a wrong one mis-pitches it."""
    pcm = speech_like(0.5)

    wav = pcm16_to_wav(pcm, SAMPLE_RATE)

    with wave.open(BytesIO(wav)) as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == SAMPLE_RATE
        assert handle.getnframes() == len(pcm) // 2
        assert handle.readframes(handle.getnframes()) == pcm


def test_an_empty_recording_still_produces_a_valid_container() -> None:
    """TC-BE-261: a zero-length body must not produce a file that fails to parse."""
    wav = pcm16_to_wav(b"", SAMPLE_RATE)

    with wave.open(BytesIO(wav)) as handle:
        assert handle.getnframes() == 0


# --------------------------------------------------------------------------- #
# Refusing what is not worth sending
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("size", [0, 2, MIN_UTTERANCE_BYTES - 2])
async def test_audio_too_short_to_be_speech_is_never_uploaded(size: int) -> None:
    """TC-BE-262: Whisper answers a click with a confident hallucination, not silence."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"text": "Thank you."})

    provider = build(handler)

    transcript = await provider.transcribe(b"\x00" * size)

    assert transcript.text == ""
    assert calls == 0, "a clip this short must not cost a request"


# --------------------------------------------------------------------------- #
# The request and its result
# --------------------------------------------------------------------------- #


async def test_a_successful_transcription_returns_the_text_and_its_cost() -> None:
    """TC-BE-263: the transcript and the milliseconds it took both reach the caller."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "  How do you handle interruptions?  "})

    provider = build(handler)

    transcript = await provider.transcribe(speech_like())

    assert transcript.text == "How do you handle interruptions?"
    assert transcript.latency_ms >= 0
    assert seen["url"].endswith("/audio/transcriptions")
    assert seen["auth"] == "Bearer test-key"
    # The upload is a real WAV, and the language is pinned so a short clip cannot
    # send the model drifting into another one.
    assert b"RIFF" in seen["body"]
    assert b'name="language"' in seen["body"]


async def test_a_transcript_of_only_whitespace_comes_back_empty() -> None:
    """TC-BE-264: silence is not an error; the session simply returns to listening."""
    provider = build(lambda request: httpx.Response(200, json={"text": "   "}))

    transcript = await provider.transcribe(speech_like())

    assert transcript.text == ""


# --------------------------------------------------------------------------- #
# Failure (TR-030, TR-085)
# --------------------------------------------------------------------------- #


async def test_a_rate_limit_is_retried_once_and_then_reported(monkeypatch: Any) -> None:
    """TC-BE-265: one retry, then a retryable error carrying the wait the server asked for."""
    attempts = 0
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, headers={"retry-after": "3"}, json={"error": {}})

    async def no_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(GroqWhisperSTT, "_sleep", staticmethod(no_sleep))
    provider = build(handler)

    with pytest.raises(ProviderError) as raised:
        await provider.transcribe(speech_like())

    assert attempts == MAX_ATTEMPTS, "one attempt plus one retry, and no more"
    assert len(slept) == MAX_ATTEMPTS - 1
    assert raised.value.provider == PROVIDER_NAME
    assert raised.value.retryable is True
    assert raised.value.retry_after == 3


async def test_a_transient_failure_that_clears_is_not_reported(monkeypatch: Any) -> None:
    """TC-BE-266: the retry exists to be useful, not only to delay the error."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"error": {}})
        return httpx.Response(200, json={"text": "Second time lucky."})

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(GroqWhisperSTT, "_sleep", staticmethod(no_sleep))
    provider = build(handler)

    transcript = await provider.transcribe(speech_like())

    assert transcript.text == "Second time lucky."
    assert attempts == 2


async def test_a_rejected_request_is_not_retried() -> None:
    """TC-BE-267: a 400 will fail identically the second time, so retrying only adds delay."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, text="bad audio")

    provider = build(handler)

    with pytest.raises(ProviderError) as raised:
        await provider.transcribe(speech_like())

    assert attempts == 1
    assert raised.value.retryable is False


async def test_a_transport_failure_becomes_a_provider_error(monkeypatch: Any) -> None:
    """TC-BE-268: TR-085 -- no httpx exception may escape the provider boundary."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(GroqWhisperSTT, "_sleep", staticmethod(no_sleep))
    provider = build(handler)

    with pytest.raises(ProviderError) as raised:
        await provider.transcribe(speech_like())

    assert raised.value.provider == PROVIDER_NAME
    assert raised.value.retryable is True


async def test_a_response_that_is_not_json_is_reported_clearly() -> None:
    """TC-BE-269: an HTML error page must not surface as a JSON decoding traceback."""
    provider = build(lambda request: httpx.Response(200, text="<html>gateway</html>"))

    with pytest.raises(ProviderError) as raised:
        await provider.transcribe(speech_like())

    assert "not JSON" in raised.value.message


async def test_closing_releases_the_connection_pool() -> None:
    """TC-BE-270: TR-013 -- the provider owns a client and must be able to give it back."""
    provider = build(lambda request: httpx.Response(200, json={"text": "hello"}))

    await provider.aclose()

    assert provider._client.is_closed
