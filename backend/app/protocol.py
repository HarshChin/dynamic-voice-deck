"""WebSocket message contract (TRD §6).

Text frames carry JSON objects with a ``type`` discriminator; binary frames
carry audio. This module is the single source of truth for the backend half of
that contract, mirrored in ``frontend/src/protocol.ts``. A parity test asserts
the two sets of ``type`` strings stay equal (TR-143), so changes must land in
both files together.

Audio framing (TR-141): every server audio frame is an 8-byte little-endian
header of ``sentence_id`` and ``seq`` followed by PCM16 samples.
"""

from __future__ import annotations

import struct
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

from .decks.models import Deck

PROTOCOL_VERSION = 1
"""Incremented when a change would break an older client."""

AUDIO_HEADER = struct.Struct("<II")
"""Binary layout of an audio frame header: ``sentence_id`` then ``seq``."""

AUDIO_HEADER_BYTES = AUDIO_HEADER.size
"""Size of the audio frame header in bytes (8)."""

MAX_TEXT_INPUT_CHARS = 500
"""Longest typed question accepted, matching the frontend's input limit."""


class SessionState(StrEnum):
    """States of a session's turn-taking machine (TRD §7)."""

    IDLE = "idle"
    CONNECTING = "connecting"
    LISTENING = "listening"
    HEARING = "hearing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"
    ERROR = "error"


class ErrorCode(StrEnum):
    """Stable error codes sent to the client (TRD §6.2)."""

    BAD_MESSAGE = "bad_message"
    UNEXPECTED_BINARY = "unexpected_binary"
    STT_FAILED = "stt_failed"
    LLM_FAILED = "llm_failed"
    TTS_FAILED = "tts_failed"
    TURN_TIMEOUT = "turn_timeout"
    INTERNAL_ERROR = "internal_error"
    DECK_NOT_FOUND = "deck_not_found"
    RATE_LIMITED = "rate_limited"


class SessionMode(StrEnum):
    """Whether the agent waits for questions or walks the deck unprompted."""

    QA = "qa"
    PRESENT = "present"


class ToolSource(StrEnum):
    """Origin of a navigation decision.

    ``LLM`` means the model called the tool. ``FALLBACK`` means the model
    answered about another slide without calling it and the keyword matcher
    inferred the target (TR-062). The distinction is surfaced in the UI so the
    two paths can be told apart while demonstrating the system.
    """

    LLM = "llm"
    FALLBACK = "fallback"


# --------------------------------------------------------------------------- #
# Client -> Server
# --------------------------------------------------------------------------- #


class SessionStartMsg(BaseModel):
    """Open a session against a deck. Must be the first message."""

    type: Literal["session.start"] = "session.start"
    deck_id: str
    mode: SessionMode = SessionMode.QA
    client_ts: float | None = None


class SpeechStartMsg(BaseModel):
    """Voice activity began. Doubles as an interrupt while the agent speaks."""

    type: Literal["speech.start"] = "speech.start"
    client_ts: float | None = None


class SpeechEndMsg(BaseModel):
    """An utterance finished; exactly one binary frame follows (TR-140)."""

    type: Literal["speech.end"] = "speech.end"
    duration_ms: int = Field(ge=0)
    client_ts: float | None = None


class InterruptMsg(BaseModel):
    """Barge-in, carrying how much of the answer the user actually heard.

    ``last_completed_sentence_id`` is ``None`` when playback had not started, in
    which case nothing of the answer was heard (TR-051).
    """

    type: Literal["interrupt"] = "interrupt"
    last_completed_sentence_id: int | None = Field(default=None, ge=0)
    client_ts: float | None = None


class InterruptCancelMsg(BaseModel):
    """The speech onset that caused an interrupt turned out to be a misfire."""

    type: Literal["interrupt.cancel"] = "interrupt.cancel"


class PlaybackProgressMsg(BaseModel):
    """A sentence finished playing. Drives the return to ``LISTENING``."""

    type: Literal["playback.progress"] = "playback.progress"
    turn_id: int = Field(ge=0)
    sentence_id: int = Field(ge=0)


class SlideChangedMsg(BaseModel):
    """The user navigated manually; the agent's context follows the screen."""

    type: Literal["slide.changed"] = "slide.changed"
    index: int = Field(ge=1)
    source: Literal["user"] = "user"


class TextInputMsg(BaseModel):
    """A typed question, bypassing speech-to-text (F13)."""

    type: Literal["text.input"] = "text.input"
    text: str = Field(min_length=1, max_length=MAX_TEXT_INPUT_CHARS)


class ControlAction(StrEnum):
    """Session-level commands issued from the UI."""

    START_PRESENTATION = "start_presentation"
    PAUSE = "pause"
    RESUME = "resume"
    MUTE = "mute"
    UNMUTE = "unmute"


class ControlMsg(BaseModel):
    """Change presentation mode or muting."""

    type: Literal["control"] = "control"
    action: ControlAction


ClientMessage = Annotated[
    SessionStartMsg
    | SpeechStartMsg
    | SpeechEndMsg
    | InterruptMsg
    | InterruptCancelMsg
    | PlaybackProgressMsg
    | SlideChangedMsg
    | TextInputMsg
    | ControlMsg,
    Field(discriminator="type"),
]
"""Any message the client may send."""

CLIENT_MESSAGE_ADAPTER: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)
"""Validator for inbound frames; raises ``ValidationError`` on anything else."""


# --------------------------------------------------------------------------- #
# Server -> Client
# --------------------------------------------------------------------------- #


class ProviderNames(BaseModel):
    """Which provider implementation is serving each stage."""

    stt: str
    llm: str
    tts: str


class SessionReadyMsg(BaseModel):
    """Session accepted; carries the deck so the client can render it."""

    type: Literal["session.ready"] = "session.ready"
    session_id: str
    protocol_version: int = PROTOCOL_VERSION
    deck: Deck
    providers: ProviderNames


class StateMsg(BaseModel):
    """A turn-taking state transition (TR-020)."""

    type: Literal["state"] = "state"
    value: SessionState
    turn_id: int = Field(ge=0)
    server_ts: float


class TranscriptUserMsg(BaseModel):
    """What the user was heard to say."""

    type: Literal["transcript.user"] = "transcript.user"
    turn_id: int = Field(ge=0)
    text: str
    final: bool = True


class TranscriptAgentMsg(BaseModel):
    """One spoken sentence, sent as its first audio leaves (TR-033)."""

    type: Literal["transcript.agent"] = "transcript.agent"
    turn_id: int = Field(ge=0)
    sentence_id: int = Field(ge=0)
    text: str


class ToolCallMsg(BaseModel):
    """A navigation decision, and whether the model or the fallback made it."""

    type: Literal["tool.call"] = "tool.call"
    turn_id: int = Field(ge=0)
    name: str
    args: dict[str, Any]
    source: ToolSource


class SlideGotoMsg(BaseModel):
    """Move the deck. ``reason`` is shown in the event log.

    Carries ``turn_id`` so the client can apply the same stale-turn guard it
    applies to every other per-turn message (TR-131). Without it the one message
    that changes what the audience actually sees would be the only one exempt,
    and a navigation from a turn the user already interrupted could still move
    the deck.
    """

    type: Literal["slide.goto"] = "slide.goto"
    turn_id: int = Field(ge=0)
    index: int = Field(ge=1)
    highlight: int | None = Field(default=None, ge=0)
    reason: str


class AgentCancelledMsg(BaseModel):
    """The turn was cut short; history was truncated at this sentence."""

    type: Literal["agent.cancelled"] = "agent.cancelled"
    turn_id: int = Field(ge=0)
    truncated_at_sentence_id: int | None = Field(default=None, ge=0)


class MetricsMsg(BaseModel):
    """Per-stage latencies for one turn, in milliseconds (TR-163)."""

    type: Literal["metrics"] = "metrics"
    turn_id: int = Field(ge=0)
    stt_ms: int | None = None
    llm_ttft_ms: int | None = None
    llm_total_ms: int | None = None
    tts_ttfb_ms: int | None = None
    sentences: int = Field(default=0, ge=0)


class ErrorMsg(BaseModel):
    """A failure the client should surface. ``recoverable`` keeps the session."""

    type: Literal["error"] = "error"
    code: ErrorCode
    message: str
    recoverable: bool = True
    retry_after_s: float | None = Field(default=None, ge=0)
    """Seconds the upstream asked us to wait, when it said (TR-171).

    A number rather than a sentence, because the client counts it down. Only a
    rate limit sets it; every other failure leaves it ``None``.
    """


ServerMessage = Annotated[
    SessionReadyMsg
    | StateMsg
    | TranscriptUserMsg
    | TranscriptAgentMsg
    | ToolCallMsg
    | SlideGotoMsg
    | AgentCancelledMsg
    | MetricsMsg
    | ErrorMsg,
    Field(discriminator="type"),
]
"""Any message the server may send."""


CLIENT_MESSAGE_TYPES: frozenset[str] = frozenset(
    {
        "session.start",
        "speech.start",
        "speech.end",
        "interrupt",
        "interrupt.cancel",
        "playback.progress",
        "slide.changed",
        "text.input",
        "control",
    }
)
"""Every client message type, for the cross-language parity test (TR-143)."""

SERVER_MESSAGE_TYPES: frozenset[str] = frozenset(
    {
        "session.ready",
        "state",
        "transcript.user",
        "transcript.agent",
        "tool.call",
        "slide.goto",
        "agent.cancelled",
        "metrics",
        "error",
    }
)
"""Every server message type, for the cross-language parity test (TR-143)."""


def encode_audio_frame(sentence_id: int, seq: int, pcm16: bytes) -> bytes:
    """Frame a chunk of audio for the wire.

    Args:
        sentence_id: Which sentence of the current turn this audio belongs to.
        seq: Position of this chunk within the sentence, from zero.
        pcm16: Little-endian 16-bit mono samples.

    Returns:
        The header followed by the samples.
    """
    return AUDIO_HEADER.pack(sentence_id, seq) + pcm16


def decode_audio_frame(frame: bytes) -> tuple[int, int, bytes]:
    """Split a wire frame back into its header fields and samples.

    Args:
        frame: A complete binary frame.

    Returns:
        The sentence id, the sequence number, and the samples.

    Raises:
        ValueError: If the frame is too short to contain a header.
    """
    if len(frame) < AUDIO_HEADER_BYTES:
        msg = f"audio frame must be at least {AUDIO_HEADER_BYTES} bytes, got {len(frame)}"
        raise ValueError(msg)
    sentence_id, seq = AUDIO_HEADER.unpack_from(frame, 0)
    return sentence_id, seq, frame[AUDIO_HEADER_BYTES:]
