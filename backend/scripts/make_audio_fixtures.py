"""Generate the recorded utterances the integration tests transcribe.

Run from ``backend/``::

    uv run python scripts/make_audio_fixtures.py

The utterances are synthesised by the project's own Kokoro voice and resampled to 16 kHz, which is
exactly the rate and shape the browser uploads, so a fixture exercises the same path a person does.
Synthesising rather than recording keeps them reproducible, small enough to commit, and free of any
question about whose voice is in the repository.

The resampler here is deliberately the same naive linear interpolation the capture worklet uses.
A better one would produce a cleaner file than the product itself can send, which would make the
fixtures easier to transcribe than reality.
"""

from __future__ import annotations

import array
import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.providers.groq_stt import pcm16_to_wav  # noqa: E402
from app.providers.kokoro_tts import SAMPLE_RATE as KOKORO_RATE  # noqa: E402
from app.providers.kokoro_tts import KokoroTTS  # noqa: E402

OUT = BACKEND / "tests" / "fixtures" / "audio"
"""Where the tests look for them."""

BROWSER_RATE = 16_000
"""What the capture worklet sends, and therefore what the transcriber is given."""

SILENCE_SECONDS = 2
"""Long enough for the recogniser to be asked a real question about nothing."""

LINES = {
    "how_do_you_handle_interruptions.wav": "How do you handle interruptions?",
}
"""One file per question, named after what it asks. Add a line here to add a fixture."""


def resample(pcm16: bytes) -> bytes:
    """Drop mono samples from Kokoro's rate to the browser's by linear interpolation.

    Args:
        pcm16: Little-endian 16-bit mono samples at :data:`KOKORO_RATE`.

    Returns:
        The same audio at :data:`BROWSER_RATE`.
    """
    source = array.array("h")
    source.frombytes(pcm16)
    ratio = KOKORO_RATE / BROWSER_RATE
    out = array.array("h")
    for index in range(int(len(source) / ratio)):
        position = index * ratio
        low = int(position)
        high = min(low + 1, len(source) - 1)
        fraction = position - low
        out.append(int(source[low] * (1 - fraction) + source[high] * fraction))
    return out.tobytes()


async def main() -> None:
    """Write every fixture, reporting what was written."""
    # `print` rather than a logger: this is a developer script whose entire output is a report of
    # what it wrote, and routing that through structlog would bury it in JSON.
    OUT.mkdir(parents=True, exist_ok=True)
    tts = KokoroTTS(models_dir=BACKEND / "models")
    await tts.warm_up()

    for name, line in LINES.items():
        pcm = b"".join([chunk async for chunk in tts.synthesize(line)])
        wav = pcm16_to_wav(resample(pcm), BROWSER_RATE)
        (OUT / name).write_bytes(wav)
        seconds = len(wav) / (BROWSER_RATE * 2)
        print(f"{name}: {len(wav):,} bytes, {seconds:.2f} s  <- {line!r}")

    silence = pcm16_to_wav(b"\x00\x00" * (BROWSER_RATE * SILENCE_SECONDS), BROWSER_RATE)
    (OUT / "silence_2s.wav").write_bytes(silence)
    print(f"silence_2s.wav: {len(silence):,} bytes, {SILENCE_SECONDS:.2f} s")


if __name__ == "__main__":
    asyncio.run(main())
