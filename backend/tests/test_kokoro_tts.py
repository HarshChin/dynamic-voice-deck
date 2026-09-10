"""On-device speech synthesis (TR-012, TR-083, TR-085).

The model weights are 340 MB and are not committed, so tests that need real
synthesis are skipped when they are absent rather than downloading them. What is
always exercised is everything around the model: framing, conversion, the digest
check, and the error boundary.
"""

from __future__ import annotations

import gc
import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from app.errors import ProviderError
from app.providers.kokoro_tts import (
    EXPECTED_SHA256,
    FRAME_BYTES,
    FRAME_SAMPLES,
    MODEL_FILE,
    PROVIDER_NAME,
    SAMPLE_RATE,
    VOICES_FILE,
    KokoroTTS,
    float32_to_pcm16,
)

WEIGHTS = Path(__file__).resolve().parents[1] / "models"
"""Where the weights live once downloaded."""

needs_weights = pytest.mark.skipif(
    not (WEIGHTS / MODEL_FILE).exists(),
    reason=f"Kokoro weights absent; run the app once to download them into {WEIGHTS}",
)


class StubKokoro:
    """Stands in for the model, returning a fixed tone at a chosen rate."""

    def __init__(self, samples: int = FRAME_SAMPLES * 3, rate: int = SAMPLE_RATE) -> None:
        self.samples = samples
        self.rate = rate
        self.calls: list[tuple[str, str, float]] = []

    def create(self, text: str, voice: str, speed: float, lang: str) -> tuple[Any, int]:
        """Return deterministic audio and record how it was asked for."""
        self.calls.append((text, voice, speed))
        return np.linspace(-0.5, 0.5, self.samples, dtype=np.float32), self.rate


def loaded(stub: StubKokoro | None = None, **kwargs: Any) -> KokoroTTS:
    """Build a provider with the model already in place.

    Args:
        stub: The stand-in model, or a default one.
        **kwargs: Constructor overrides.

    Returns:
        A provider ready to synthesise without touching disk or the network.
    """
    provider = KokoroTTS(
        models_dir=kwargs.pop("models_dir", WEIGHTS),
        voice=kwargs.pop("voice", "af_heart"),
        speed=kwargs.pop("speed", 1.0),
        download=kwargs.pop("download", False),
    )
    provider._kokoro = stub if stub is not None else StubKokoro()
    return provider


# --------------------------------------------------------------------------- #
# Sample conversion
# --------------------------------------------------------------------------- #


def test_samples_are_clipped_before_scaling() -> None:
    """TC-BE-271: an out-of-range sample must not wrap around into a full-scale click.

    Kokoro occasionally returns a value just past 1.0. Letting that wrap in the
    integer conversion produces the loudest possible sample, which is audible.
    """
    samples = np.array([1.4, -1.4, 0.0], dtype=np.float32)

    pcm = float32_to_pcm16(samples)

    values = np.frombuffer(pcm, dtype="<i2")
    assert values.tolist() == [32767, -32767, 0]


def test_conversion_produces_two_bytes_per_sample() -> None:
    """TC-BE-272: the wire format is PCM16; a stray byte would desynchronise playback."""
    pcm = float32_to_pcm16(np.zeros(1000, dtype=np.float32))

    assert len(pcm) == 2000


# --------------------------------------------------------------------------- #
# Framing (TR-141)
# --------------------------------------------------------------------------- #


async def test_audio_is_yielded_in_wire_sized_frames() -> None:
    """TC-BE-273: 100 ms frames, with only the last one allowed to be short."""
    provider = loaded(StubKokoro(samples=FRAME_SAMPLES * 2 + 100))

    frames = [frame async for frame in provider.synthesize("Two layers, actually.")]

    assert len(frames) == 3
    assert [len(frame) for frame in frames[:-1]] == [FRAME_BYTES, FRAME_BYTES]
    assert 0 < len(frames[-1]) < FRAME_BYTES
    assert all(len(frame) % 2 == 0 for frame in frames), "whole samples only"


async def test_blank_text_produces_no_audio_at_all() -> None:
    """TC-BE-274: an empty segment must not cost a synthesis or emit a frame."""
    stub = StubKokoro()
    provider = loaded(stub)

    frames = [frame async for frame in provider.synthesize("   ")]

    assert frames == []
    assert stub.calls == []


async def test_the_configured_voice_and_speed_reach_the_model() -> None:
    """TC-BE-275: a deck may name its own voice, and it must actually be used."""
    stub = StubKokoro()
    provider = loaded(stub, voice="af_heart", speed=1.25)

    [frame async for frame in provider.synthesize("Hello.", "am_michael")]

    assert stub.calls == [("Hello.", "am_michael", 1.25)]


# --------------------------------------------------------------------------- #
# Failure (TR-085)
# --------------------------------------------------------------------------- #


async def test_synthesising_before_warm_up_is_refused_clearly() -> None:
    """TC-BE-276: a missing model must say so, not raise an attribute error."""
    provider = KokoroTTS(models_dir=WEIGHTS, download=False)

    with pytest.raises(ProviderError) as raised:
        [frame async for frame in provider.synthesize("Hello.")]

    assert raised.value.provider == PROVIDER_NAME
    assert "warm_up" in raised.value.message


async def test_an_unexpected_sample_rate_is_refused() -> None:
    """TC-BE-277: the client's audio clock is fixed, so a mismatch would play at the wrong pitch."""
    provider = loaded(StubKokoro(rate=22_050))

    with pytest.raises(ProviderError) as raised:
        [frame async for frame in provider.synthesize("Hello.")]

    assert "24000" in raised.value.message


async def test_a_model_failure_does_not_escape_as_a_vendor_error() -> None:
    """TC-BE-278: TR-085 -- nothing from the library may reach the pipeline."""

    class Broken(StubKokoro):
        def create(self, text: str, voice: str, speed: float, lang: str) -> tuple[Any, int]:
            raise RuntimeError("onnxruntime exploded")

    provider = loaded(Broken())

    with pytest.raises(ProviderError) as raised:
        [frame async for frame in provider.synthesize("Hello.")]

    assert raised.value.provider == PROVIDER_NAME
    assert isinstance(raised.value.__cause__, RuntimeError)


async def test_missing_weights_with_downloads_off_says_which_files() -> None:
    """TC-BE-279: TR-012 -- an operator needs to know what to fetch and where."""
    provider = KokoroTTS(models_dir=Path("/nonexistent/models"), download=False)

    with pytest.raises(ProviderError) as raised:
        await provider.warm_up()

    assert MODEL_FILE in raised.value.message
    assert VOICES_FILE in raised.value.message


# --------------------------------------------------------------------------- #
# The real model, when it is present
# --------------------------------------------------------------------------- #


@needs_weights
def test_the_shipped_weights_match_the_digests_this_code_was_written_against() -> None:
    """TC-BE-280: TR-012 -- a truncated download must fail loudly, not sound wrong."""
    for name, expected in EXPECTED_SHA256.items():
        digest = hashlib.sha256((WEIGHTS / name).read_bytes()).hexdigest()
        assert digest == expected, name


@needs_weights
@pytest.mark.slow
@pytest.mark.integration
async def test_real_synthesis_produces_audible_speech() -> None:
    """TC-BE-075: TR-083 -- the model returns 24 kHz audio of a plausible length.

    Marked ``integration`` and therefore out of the default run, for the same reason the Groq tests
    are: it loads a real model. Doing that costs seconds, needs weights the repository does not
    carry, and leaves an ONNX Runtime session whose native destructor aborts the process during
    interpreter shutdown -- after the suite has already reported success, which on macOS means a
    crash dialog for a green run. Everything about this provider that can be tested without the
    model is tested above against a stub; this one asserts the model itself still sounds like
    speech, which is what ``make test-integration`` is for.
    """
    provider = KokoroTTS(models_dir=WEIGHTS, download=False)
    await provider.warm_up()

    frames = [frame async for frame in provider.synthesize("Two layers, actually.")]

    pcm = b"".join(frames)
    seconds = len(pcm) / 2 / SAMPLE_RATE
    assert 0.5 < seconds < 4.0, f"a five-word sentence should not last {seconds:.1f}s"

    samples = np.frombuffer(pcm, dtype="<i2")
    assert np.abs(samples).max() > 3000, "silence is not speech"

    await provider.aclose()
    # Destroy the ONNX session while the interpreter is still healthy rather than at shutdown,
    # where its native destructor races the runtime's own teardown.
    gc.collect()
