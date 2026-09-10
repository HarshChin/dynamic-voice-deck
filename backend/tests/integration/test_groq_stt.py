"""Transcription against the real Whisper endpoint (PRD F4, TR-030).

These are the only tests that prove the provider's request is one Groq actually accepts. Everything
about the request is exercised offline in ``tests/test_groq_stt.py`` against a mock transport, which
catches a malformed multipart body or a dropped header; what it cannot catch is the endpoint
disagreeing with our reading of its contract. That needs one real call, so there is one.

The utterances were synthesised by the project's own Kokoro voice and resampled to 16 kHz, the rate
and shape the browser uploads. Using our own synthesis rather than a recording of a person keeps the
fixtures reproducible and small, and it exercises exactly the audio path the product produces.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from app.config import get_settings
from app.pipeline.turn import is_filler
from app.providers.groq_stt import GroqWhisperSTT

pytestmark = pytest.mark.integration

SAMPLE_RATE = 16_000
"""The rate the browser uploads at, and the rate the fixtures are stored at."""


def build(api_key: str) -> GroqWhisperSTT:
    """Build the provider exactly as the registry does, so the test covers the shipped wiring.

    Args:
        api_key: The real credential.

    Returns:
        A provider pointed at Groq.
    """
    settings = get_settings()
    return GroqWhisperSTT(
        api_key=api_key,
        base_url=settings.groq_base_url,
        model=settings.groq_stt_model,
    )


async def test_a_spoken_question_comes_back_as_words(
    groq_api_key: str, utterance: Callable[[str], bytes]
) -> None:
    """TC-INT-001: a real utterance transcribes to text naming what it asked about."""
    provider = build(groq_api_key)

    result = await provider.transcribe(
        utterance("how_do_you_handle_interruptions.wav"), SAMPLE_RATE
    )
    await provider.aclose()

    assert "interrupt" in result.text.lower(), result.text
    assert result.latency_ms > 0


async def test_two_seconds_of_silence_produce_nothing_to_answer(
    groq_api_key: str, utterance: Callable[[str], bytes]
) -> None:
    """TC-INT-002: silence transcribes to nothing, or to filler the pipeline already drops.

    Whisper does not return an empty string for silence as often as one would hope; it returns
    whatever it hallucinates, most commonly "Thank you." or a subtitle credit. That is exactly why
    the denylist exists, so the assertion accepts either outcome. A transcript that is neither
    would start a turn from silence, which is the failure this guards against.
    """
    provider = build(groq_api_key)

    result = await provider.transcribe(utterance("silence_2s.wav"), SAMPLE_RATE)
    await provider.aclose()

    assert is_filler(result.text), result.text
