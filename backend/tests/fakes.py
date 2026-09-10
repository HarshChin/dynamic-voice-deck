"""Fake STT, LLM, and TTS providers (TRD §12.2).

These are the only providers the unit and end-to-end suites use: they need no
network, no API key, and no model weights, and they are deterministic, so a
latency or ordering assertion fails for a real reason rather than because a
remote service was slow. Each one satisfies the matching ``runtime_checkable``
protocol in :mod:`app.providers.base`, which ``test_fakes_satisfy_protocols``
asserts, so the pipeline cannot tell them apart from the real thing.

They live in the test package on purpose. ``app.providers.registry`` imports
this module lazily, and only when ``*_PROVIDER=fake`` is selected, so a
production process never imports test code (CLAUDE.md §3.5).

Every fake records what it was called with, because most pipeline assertions
are about *what the pipeline sent* -- the prompt, the tool list, the sentence
order -- rather than about the answer that came back.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from app.providers.base import (
    LLMDone,
    LLMEvent,
    Message,
    TokenDelta,
    ToolSpec,
    Transcript,
)

FAKE_STT_NAME: Final = "fake_stt"
FAKE_LLM_NAME: Final = "fake_llm"
FAKE_TTS_NAME: Final = "fake_tts"

DEFAULT_TRANSCRIPT: Final = "What is this deck about?"
"""Transcript returned for audio the caller did not script."""

DEFAULT_ANSWER: Final = "This deck is about a voice-first slide presenter."
"""Content of the single token in the default LLM script."""

FAKE_SAMPLE_RATE: Final = 24_000
"""Matches Kokoro's output rate (TR-083), so downstream framing math is real."""

BYTES_PER_CHAR: Final = 64
"""Synthesised bytes per character: ~1.3 ms of speech per character at 24 kHz."""

CHUNK_BYTES: Final = 4_800
"""Chunk size the real TTS provider uses: 100 ms of PCM16 at 24 kHz (TR-083)."""

BYTES_PER_SAMPLE: Final = 2
"""PCM16: chunk sizes must stay even or a frame splits a sample in half."""


def default_llm_script() -> list[LLMEvent]:
    """Build the script :class:`FakeLLM` uses when the caller supplies none.

    A fresh list of fresh events is built per call so that one test mutating an
    event cannot affect another (the fakes hold no module-level state).

    Returns:
        One token followed by a normal completion.
    """
    return [TokenDelta(text=DEFAULT_ANSWER), LLMDone(finish_reason="stop")]


@dataclass(slots=True)
class STTCall:
    """One recorded call to :meth:`FakeSTT.transcribe`.

    Attributes:
        pcm16: The audio the pipeline submitted.
        sample_rate: Sample rate it claimed for that audio.
    """

    pcm16: bytes
    sample_rate: int


@dataclass(slots=True)
class LLMCall:
    """One recorded call to :meth:`FakeLLM.stream`.

    Attributes:
        messages: History as sent, so a test can assert on the system prompt.
        tools: Tools offered, so a test can assert the schema reached the model.
    """

    messages: list[Message]
    tools: list[ToolSpec]
    tool_choice: str = "auto"
    """Whether the request permitted a tool call.

    Recorded because the two-step turn declares the tools on both requests and
    only forbids calling them on the second; asserting on the tools list alone
    can no longer tell the two apart.
    """


@dataclass(slots=True)
class TTSCall:
    """One recorded call to :meth:`FakeTTS.synthesize`.

    Attributes:
        text: The sentence submitted.
        voice: Voice requested, or ``None`` for the configured default.
    """

    text: str
    voice: str | None


class FakeSTT:
    """Speech-to-text that replays scripted transcripts.

    Args:
        scripted: Exact audio bytes mapped to the transcript to return for them.
        default_text: Transcript for audio that is not in ``scripted``.
        latency_ms: Value reported in :attr:`Transcript.latency_ms`.
        delay_s: Wall-clock delay before returning, for tests that need the
            transcription step to be interruptible.

    Attributes:
        name: Provider name reported to the health probe.
        calls: Every call, in order.
    """

    def __init__(
        self,
        scripted: Mapping[bytes, str] | None = None,
        *,
        default_text: str = DEFAULT_TRANSCRIPT,
        latency_ms: int = 0,
        delay_s: float = 0.0,
    ) -> None:
        self.name = FAKE_STT_NAME
        self._scripted = dict(scripted or {})
        self._default_text = default_text
        self._latency_ms = latency_ms
        self._delay_s = delay_s
        self.calls: list[STTCall] = []

    async def transcribe(self, pcm16: bytes, sample_rate: int = 16_000) -> Transcript:
        """Return the scripted transcript for this audio.

        Args:
            pcm16: Little-endian 16-bit mono samples.
            sample_rate: Sample rate of ``pcm16`` in hertz.

        Returns:
            The scripted transcript, or ``default_text`` for unknown audio.
        """
        self.calls.append(STTCall(pcm16=pcm16, sample_rate=sample_rate))
        await asyncio.sleep(self._delay_s)
        return Transcript(
            text=self._scripted.get(pcm16, self._default_text),
            latency_ms=self._latency_ms,
            language="en",
        )


class FakeLLM:
    """Language model that replays a scripted event stream.

    Args:
        script: Events to yield, in order. Scripts normally end with an
            :class:`~app.providers.base.LLMDone`, as a real stream does.
        delay_s: Delay before each event. The provider awaits even when this is
            zero, so the stream always has a cancellation point between events
            and a barge-in test can land reliably mid-flight.

    Attributes:
        name: Provider name reported to the health probe.
        calls: Every call, recording the messages and tools it was given.
        cancelled: How many streams were cancelled before finishing, which is
            how a barge-in test proves generation actually stopped.
    """

    def __init__(
        self,
        script: Sequence[LLMEvent] | None = None,
        *,
        delay_s: float = 0.0,
    ) -> None:
        self.name = FAKE_LLM_NAME
        self._script: list[LLMEvent] = list(script) if script is not None else default_llm_script()
        self._delay_s = delay_s
        self.calls: list[LLMCall] = []
        self.cancelled = 0

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        tool_choice: str = "auto",
    ) -> AsyncIterator[LLMEvent]:
        """Replay the script.

        Args:
            messages: Conversation history, recorded then ignored.
            tools: Tools offered, recorded then ignored.

        Yields:
            The scripted events, one per ``delay_s``.

        Raises:
            asyncio.CancelledError: Re-raised after recording the cancellation,
                so callers see the same behaviour as a real provider.
        """
        self.calls.append(
            LLMCall(messages=list(messages), tools=list(tools), tool_choice=tool_choice)
        )
        try:
            for event in self._script:
                await asyncio.sleep(self._delay_s)
                yield event
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


class FakeTTS:
    """Text-to-speech that emits deterministic bytes proportional to the text.

    The audio is not sound: it is the SHA-256 digest of the sentence repeated to
    the required length. That keeps it deterministic across runs and machines,
    keeps different sentences distinguishable in a byte-level assertion, and
    keeps the length -- the thing playback scheduling actually depends on -- a
    simple function of the input.

    Args:
        bytes_per_char: Bytes of PCM16 produced per character of text.
        delay_s: Delay before each chunk, so a test can interrupt playback.
        chunk_bytes: Size of each yielded chunk; must be even for PCM16.
        sample_rate: Rate reported to the pipeline.

    Attributes:
        name: Provider name reported to the health probe.
        sample_rate: Sample rate of the bytes yielded.
        calls: Every call, in order.
        warm_ups: How many times :meth:`warm_up` was awaited.
    """

    def __init__(
        self,
        *,
        bytes_per_char: int = BYTES_PER_CHAR,
        delay_s: float = 0.0,
        chunk_bytes: int = CHUNK_BYTES,
        sample_rate: int = FAKE_SAMPLE_RATE,
    ) -> None:
        if chunk_bytes % BYTES_PER_SAMPLE:
            msg = f"chunk_bytes must be even for PCM16, got {chunk_bytes}"
            raise ValueError(msg)
        self.name = FAKE_TTS_NAME
        self.sample_rate = sample_rate
        self._bytes_per_char = bytes_per_char
        self._delay_s = delay_s
        self._chunk_bytes = chunk_bytes
        self.calls: list[TTSCall] = []
        self.warm_ups = 0

    async def synthesize(self, text: str, voice: str | None = None) -> AsyncIterator[bytes]:
        """Yield deterministic PCM16 for one sentence.

        Args:
            text: The sentence to "speak".
            voice: Voice identifier, recorded then ignored.

        Yields:
            Chunks of at most ``chunk_bytes``; the last one may be shorter.
        """
        self.calls.append(TTSCall(text=text, voice=voice))
        audio = _deterministic_pcm16(text, self._bytes_per_char)
        for start in range(0, len(audio), self._chunk_bytes):
            await asyncio.sleep(self._delay_s)
            yield audio[start : start + self._chunk_bytes]

    async def warm_up(self) -> None:
        """Record a warm-up. There are no weights to load."""
        self.warm_ups += 1


def _deterministic_pcm16(text: str, bytes_per_char: int) -> bytes:
    """Build reproducible PCM16 whose length is proportional to the text.

    Args:
        text: The sentence being synthesised.
        bytes_per_char: Bytes to emit per character.

    Returns:
        An even number of bytes, unique to ``text`` and stable across runs.
    """
    total = len(text) * bytes_per_char
    total += total % BYTES_PER_SAMPLE
    if not total:
        return b""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    repeats = -(-total // len(digest))
    return (digest * repeats)[:total]
