"""Speech-to-text with Whisper on Groq (TR-030, TR-081, TR-085).

One request per finished utterance, not a continuous stream. That choice is made
in the browser, where the voice detector decides when a turn has ended, and it
has two consequences worth stating plainly. Transcription cannot begin until the
speaker stops, which costs latency. In exchange the free tier's request budget
stretches a long way, and interruption is detected locally with no round trip at
all -- which is the part a listener actually feels.

The upload is a WAV built in memory. Whisper needs a container to know the sample
rate, and writing a 44-byte header is cheaper in every sense than encoding to a
compressed format on the way out.
"""

from __future__ import annotations

import io
import struct
import time
from typing import Any, Final

import httpx

from ..errors import ProviderError
from ..logging_setup import get_logger
from .base import Transcript

logger = get_logger(__name__)

PROVIDER_NAME: Final = "groq_stt"
"""Value of :attr:`GroqWhisperSTT.name` and of ``ProviderError.provider``."""

TRANSCRIPTIONS_PATH: Final = "/audio/transcriptions"
"""Path appended to ``GROQ_BASE_URL`` to reach the transcription endpoint."""

WAV_HEADER_BYTES: Final = 44
"""Size of the canonical RIFF/WAVE header this module writes."""

REQUEST_TIMEOUT_S: Final = 20.0
"""Ceiling on one transcription request.

Below the session's own 20 s turn watchdog would be ideal, but an utterance may
legitimately take a few seconds; the watchdog remains the outer guarantee.
"""

RETRY_DELAY_S: Final = 0.5
"""Pause before the single retry (TR-030)."""

MAX_ATTEMPTS: Final = 2
"""One attempt plus one retry. More would push a slow turn past the watchdog."""

MIN_UTTERANCE_BYTES: Final = 3_200
"""Shortest upload worth sending: 100 ms of 16 kHz PCM16.

Anything shorter is a click or a truncated frame. Whisper answers such input with
a confident hallucination rather than silence, so it is cheaper and more honest
to refuse it here.
"""


def pcm16_to_wav(pcm16: bytes, sample_rate: int) -> bytes:
    """Wrap raw samples in a minimal RIFF/WAVE container.

    Args:
        pcm16: Little-endian 16-bit mono samples.
        sample_rate: Sample rate of ``pcm16`` in hertz.

    Returns:
        A complete single-channel 16-bit WAV file.
    """
    byte_rate = sample_rate * 2
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm16),
        b"WAVE",
        b"fmt ",
        16,  # PCM format chunk size
        1,  # PCM, uncompressed
        1,  # mono
        sample_rate,
        byte_rate,
        2,  # block align: one 16-bit sample
        16,  # bits per sample
        b"data",
        len(pcm16),
    )
    return header + pcm16


class GroqWhisperSTT:
    """Transcribes a finished utterance with Whisper on Groq.

    Attributes:
        name: Provider name reported by the health probe.
    """

    name = PROVIDER_NAME

    def __init__(self, *, api_key: str, base_url: str, model: str) -> None:
        """Create a provider bound to one account and model.

        Args:
            api_key: Groq credential.
            base_url: OpenAI-compatible base URL.
            model: Whisper model identifier.
        """
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=REQUEST_TIMEOUT_S,
        )

    async def transcribe(self, pcm16: bytes, sample_rate: int = 16_000) -> Transcript:
        """Transcribe one complete utterance.

        Args:
            pcm16: Little-endian 16-bit mono samples.
            sample_rate: Sample rate of ``pcm16`` in hertz.

        Returns:
            The transcript, whose text is empty when nothing intelligible was
            said. An empty result is not an error: the session simply returns to
            listening, which is the right response to a cough.

        Raises:
            ProviderError: If the request fails after one retry.
        """
        if len(pcm16) < MIN_UTTERANCE_BYTES:
            logger.info("stt.too_short", bytes=len(pcm16))
            return Transcript(text="", latency_ms=0)

        wav = pcm16_to_wav(pcm16, sample_rate)
        started = time.perf_counter()
        payload = await self._post_with_retry(wav)
        latency_ms = round((time.perf_counter() - started) * 1000)

        text = str(payload.get("text", "")).strip()
        logger.info("stt.done", ms=latency_ms, chars=len(text), bytes=len(pcm16))
        return Transcript(
            text=text,
            latency_ms=latency_ms,
            language=payload.get("language"),
        )

    async def aclose(self) -> None:
        """Close the HTTP client and its connection pool."""
        await self._client.aclose()

    # -- internals ---------------------------------------------------------- #

    async def _post_with_retry(self, wav: bytes) -> dict[str, Any]:
        """Send the audio, retrying once on a retryable failure (TR-030).

        Args:
            wav: The WAV file to upload.

        Returns:
            The decoded JSON response.

        Raises:
            ProviderError: If both attempts fail.
        """
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return await self._post(wav)
            except ProviderError as exc:
                if not exc.retryable or attempt == MAX_ATTEMPTS:
                    raise
                logger.warning("stt.retrying", reason=exc.message, attempt=attempt)
                await self._sleep(RETRY_DELAY_S)
        msg = "unreachable: the loop above always returns or raises"
        raise AssertionError(msg)  # pragma: no cover

    @staticmethod
    async def _sleep(seconds: float) -> None:
        """Wait, in a form a test can patch.

        Args:
            seconds: How long to wait.
        """
        import asyncio  # noqa: PLC0415 - local so tests can patch this method alone

        await asyncio.sleep(seconds)

    async def _post(self, wav: bytes) -> dict[str, Any]:
        """Send one transcription request.

        Args:
            wav: The WAV file to upload.

        Returns:
            The decoded JSON response.

        Raises:
            ProviderError: On any HTTP or transport failure, with ``retryable``
                set for the cases worth trying again.
        """
        files = {"file": ("utterance.wav", io.BytesIO(wav), "audio/wav")}
        data = {
            "model": self._model,
            # Fixing the language stops Whisper drifting into another one on a
            # noisy or very short clip, which it otherwise does confidently.
            "language": "en",
            "temperature": "0",
            "response_format": "verbose_json",
        }
        try:
            response = await self._client.post(TRANSCRIPTIONS_PATH, files=files, data=data)
        except httpx.TimeoutException as exc:
            msg = f"transcription timed out after {REQUEST_TIMEOUT_S:.0f}s"
            raise ProviderError(PROVIDER_NAME, msg, retryable=True) from exc
        except httpx.HTTPError as exc:
            msg = f"transcription request failed: {exc}"
            raise ProviderError(PROVIDER_NAME, msg, retryable=True) from exc

        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            retry_after = _retry_after_seconds(response)
            msg = f"rate limited by {self._model}"
            raise ProviderError(PROVIDER_NAME, msg, retryable=True, retry_after=retry_after)
        if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
            msg = f"HTTP {response.status_code} from {self._model}"
            raise ProviderError(PROVIDER_NAME, msg, retryable=True)
        if response.status_code >= httpx.codes.BAD_REQUEST:
            msg = f"HTTP {response.status_code} from {self._model}: {response.text[:200]}"
            raise ProviderError(PROVIDER_NAME, msg)

        try:
            decoded: dict[str, Any] = response.json()
        except ValueError as exc:
            msg = "transcription response was not JSON"
            raise ProviderError(PROVIDER_NAME, msg) from exc
        return decoded


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Read the ``retry-after`` header, when the server sent a usable one.

    Args:
        response: The rate-limited response.

    Returns:
        Seconds to wait, or ``None`` when the header is absent or unparseable.
    """
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
