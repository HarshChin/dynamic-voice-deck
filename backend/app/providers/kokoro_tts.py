"""Kokoro-82M text-to-speech (TR-083, TR-086).

Runs on this machine rather than behind an API, so synthesis costs no tokens and
no network round trip. Weights are downloaded once on first use and cached under
``backend/models/`` (TR-012); they are large and are never committed.

Measured on an Apple Silicon laptop, warm, voice ``af_heart``:

===================================  =====  ==========  =========
text                                 chars  synth (ms)  audio (s)
===================================  =====  ==========  =========
"Two layers, actually."                 21         259       1.34
one ordinary sentence                   53         440       2.75
three sentences                        203       1677      12.61
===================================  =====  ==========  =========

Two things follow, and they shape the whole pipeline. Synthesis is roughly three
times faster than real time, so it keeps ahead of playback once started. And its
cost scales with the length of the text handed to it, which is why the
:class:`~app.pipeline.chunker.SentenceChunker` splits early (TR-042) and why the
system prompt asks the agent to open with a short sentence (TR-086): a
twenty-character opener is audible in about 260 ms, while waiting for a whole
answer would cost the better part of two seconds.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Final

import httpx
import numpy as np

from ..errors import ProviderError
from ..logging_setup import get_logger

logger = get_logger(__name__)

PROVIDER_NAME: Final = "kokoro"
"""Value of :attr:`KokoroTTS.name` and of ``ProviderError.provider``."""

SAMPLE_RATE: Final = 24_000
"""Kokoro's output rate in hertz, confirmed against the model itself."""

FRAME_SAMPLES: Final = 2_400
"""Samples per wire frame: 100 ms at :data:`SAMPLE_RATE` (TR-141).

100 ms balances two costs. Smaller frames mean more WebSocket messages and more
scheduling work in the browser; larger frames mean the playback queue has a
coarser unit to flush on when the user interrupts.
"""

FRAME_BYTES: Final = FRAME_SAMPLES * 2
"""Bytes of PCM16 in a full frame."""

WARM_UP_TEXT: Final = "Ready."
"""Throwaway phrase synthesised at startup so the first real turn is not slow."""

RELEASE_URL: Final = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
)
"""Where the weights come from when they are not already on disk."""

MODEL_FILE: Final = "kokoro-v1.0.onnx"
VOICES_FILE: Final = "voices-v1.0.bin"

EXPECTED_SHA256: Final[dict[str, str]] = {
    MODEL_FILE: "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    VOICES_FILE: "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}
"""Digests of the files this code was written and measured against.

Checked after download so a truncated or substituted file fails loudly at
startup rather than as unintelligible audio much later.
"""

DOWNLOAD_TIMEOUT_S: Final = 600.0
"""The model file is over 300 MB; a default HTTP timeout would abort it."""


def float32_to_pcm16(samples: np.ndarray[Any, Any]) -> bytes:
    """Convert Kokoro's float samples to the wire format.

    Values are clipped before scaling. Kokoro occasionally returns a sample just
    past 1.0, and letting that wrap around in the integer conversion produces an
    audible click at full scale.

    Args:
        samples: Float32 samples, nominally in ``[-1.0, 1.0]``.

    Returns:
        Little-endian signed 16-bit samples.
    """
    clipped = np.clip(samples, -1.0, 1.0)
    raw: bytes = (clipped * 32767.0).astype("<i2").tobytes()
    return raw


class KokoroTTS:
    """Speech synthesis with Kokoro-82M, on this machine.

    Attributes:
        name: Provider name reported by the health probe.
        sample_rate: Output rate in hertz.
    """

    name = PROVIDER_NAME
    sample_rate = SAMPLE_RATE

    def __init__(
        self,
        *,
        models_dir: Path,
        voice: str = "af_heart",
        speed: float = 1.0,
        download: bool = True,
    ) -> None:
        """Create a provider bound to a weights directory.

        Nothing is loaded here. Loading and the first synthesis happen in
        :meth:`warm_up`, which the application lifespan runs at startup so the
        cost is paid before anyone is waiting (TR-013).

        Args:
            models_dir: Directory holding, or to receive, the weight files.
            voice: Default voice identifier.
            speed: Speaking rate multiplier.
            download: Whether to fetch missing weights. Tests set this to
                ``False`` so a missing file fails immediately instead of pulling
                300 MB.
        """
        self._models_dir = models_dir
        self._voice = voice
        self._speed = speed
        self._download = download
        self._kokoro: Any | None = None
        self._lock = asyncio.Lock()

    @property
    def model_path(self) -> Path:
        """Path to the ONNX model file."""
        return self._models_dir / MODEL_FILE

    @property
    def voices_path(self) -> Path:
        """Path to the voice embeddings file."""
        return self._models_dir / VOICES_FILE

    @property
    def is_loaded(self) -> bool:
        """Whether the model is in memory and ready to synthesise."""
        return self._kokoro is not None

    async def warm_up(self) -> None:
        """Ensure the weights exist, load the model, and synthesise once.

        Raises:
            ProviderError: If the weights cannot be obtained or the model cannot
                be loaded.
        """
        async with self._lock:
            if self._kokoro is not None:
                return
            await self._ensure_weights()
            self._kokoro = await asyncio.to_thread(self._load)
            await asyncio.to_thread(self._synthesize_blocking, WARM_UP_TEXT, self._voice)
            logger.info(
                "tts.ready", provider=self.name, voice=self._voice, sample_rate=self.sample_rate
            )

    async def synthesize(self, text: str, voice: str | None = None) -> AsyncIterator[bytes]:
        """Synthesise one sentence and yield it as wire frames.

        Args:
            text: The sentence to speak.
            voice: Voice identifier, or ``None`` for the configured default.

        Yields:
            Frames of little-endian 16-bit mono PCM at :attr:`sample_rate`, each
            holding at most :data:`FRAME_SAMPLES` samples.

        Raises:
            ProviderError: If the model is not loaded or synthesis fails.
        """
        spoken = text.strip()
        if not spoken:
            return
        if self._kokoro is None:
            msg = "warm_up() must run before synthesis"
            raise ProviderError(PROVIDER_NAME, msg)

        # Synthesis is CPU-bound and blocking, so it runs in a worker thread; the
        # event loop stays free to keep streaming model tokens and to notice an
        # interrupt. Cancelling this coroutine stops the frames at the next
        # yield: the thread finishes its current sentence, which is a few hundred
        # milliseconds of wasted work rather than a stuck turn.
        try:
            samples = await asyncio.to_thread(
                self._synthesize_blocking, spoken, voice or self._voice
            )
        except ProviderError:
            raise
        except Exception as exc:
            msg = f"synthesis failed: {exc}"
            raise ProviderError(PROVIDER_NAME, msg) from exc

        pcm = float32_to_pcm16(samples)
        for start in range(0, len(pcm), FRAME_BYTES):
            yield pcm[start : start + FRAME_BYTES]

    async def aclose(self) -> None:
        """Release the loaded model."""
        self._kokoro = None

    # -- internals ---------------------------------------------------------- #

    def _load(self) -> Any:
        """Load the ONNX model. Blocking; called in a worker thread.

        Returns:
            The Kokoro instance.

        Raises:
            ProviderError: If the library or the weights cannot be loaded.
        """
        try:
            from kokoro_onnx import Kokoro  # noqa: PLC0415 - heavy import, deferred to startup

            return Kokoro(str(self.model_path), str(self.voices_path))
        except Exception as exc:
            msg = f"could not load Kokoro from {self._models_dir}: {exc}"
            raise ProviderError(PROVIDER_NAME, msg) from exc

    def _synthesize_blocking(self, text: str, voice: str) -> np.ndarray[Any, Any]:
        """Run synthesis. Blocking; called in a worker thread.

        Args:
            text: Sentence to speak.
            voice: Voice identifier.

        Returns:
            Float32 samples at :attr:`sample_rate`.

        Raises:
            ProviderError: If the model is not loaded or the rate is unexpected.
        """
        if self._kokoro is None:  # pragma: no cover - guarded by the caller
            msg = "model is not loaded"
            raise ProviderError(PROVIDER_NAME, msg)
        result = self._kokoro.create(text, voice=voice, speed=self._speed, lang="en-us")
        samples: np.ndarray[Any, Any] = result[0]
        rate = int(result[1])
        if rate != self.sample_rate:
            # The client's AudioContext is built for this rate, so a mismatch
            # would play back at the wrong pitch rather than fail outright.
            msg = f"expected {self.sample_rate} Hz from Kokoro, got {rate}"
            raise ProviderError(PROVIDER_NAME, msg)
        return samples

    async def _ensure_weights(self) -> None:
        """Download any missing weight file and verify its digest.

        Raises:
            ProviderError: If a file is missing and cannot be downloaded, or if a
                downloaded file does not match its expected digest.
        """
        missing = [name for name in EXPECTED_SHA256 if not (self._models_dir / name).exists()]
        if not missing:
            return
        if not self._download:
            msg = f"missing weight files in {self._models_dir}: {', '.join(missing)}"
            raise ProviderError(PROVIDER_NAME, msg)

        self._models_dir.mkdir(parents=True, exist_ok=True)
        for name in missing:
            await self._download_file(name)

    async def _download_file(self, name: str) -> None:
        """Fetch one weight file and check it before putting it in place.

        The download lands on a temporary path and is renamed only once its
        digest matches, so an interrupted download can never be mistaken for a
        complete one on the next start.

        Args:
            name: File name to fetch.

        Raises:
            ProviderError: If the download fails or the digest does not match.
        """
        url = f"{RELEASE_URL}/{name}"
        target = self._models_dir / name
        partial = target.with_suffix(target.suffix + ".part")
        logger.info("tts.downloading", file=name, url=url)

        digest = hashlib.sha256()
        try:
            async with (
                httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_S, follow_redirects=True) as client,
                client.stream("GET", url) as response,
            ):
                response.raise_for_status()
                with partial.open("wb") as handle:
                    async for block in response.aiter_bytes(1 << 20):
                        handle.write(block)
                        digest.update(block)
        except httpx.HTTPError as exc:
            partial.unlink(missing_ok=True)
            msg = f"could not download {name}: {exc}"
            raise ProviderError(PROVIDER_NAME, msg) from exc

        actual = digest.hexdigest()
        expected = EXPECTED_SHA256[name]
        if actual != expected:
            partial.unlink(missing_ok=True)
            msg = f"{name} digest mismatch: expected {expected}, got {actual}"
            raise ProviderError(PROVIDER_NAME, msg)

        partial.replace(target)
        logger.info("tts.downloaded", file=name, bytes=target.stat().st_size)
