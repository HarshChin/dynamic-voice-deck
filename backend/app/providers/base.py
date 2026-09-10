"""Provider interfaces shared by every STT, LLM, and TTS implementation.

Nothing outside this package may import a vendor SDK (CLAUDE.md §3.5). The
pipeline depends only on the protocols declared here, which is what lets the
same session code run against Groq, a local model, or a fake in tests.

Cancellation matters as much as the data: barge-in is implemented by cancelling
the turn's asyncio task (TR-034), so streaming methods must be async iterators
whose underlying HTTP response closes when the consumer is cancelled.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class Transcript(BaseModel):
    """Result of transcribing one utterance.

    Attributes:
        text: The recognised words; empty when only noise was heard.
        latency_ms: Wall-clock time the provider took.
        language: Detected language tag, when the provider reports one.
    """

    text: str
    latency_ms: int = Field(ge=0)
    language: str | None = None


class Message(BaseModel):
    """One entry of conversation history in provider (OpenAI) shape.

    Attributes:
        role: Who produced it.
        content: The text. Empty for an assistant turn that only called tools.
        tool_calls: Calls the assistant made, in provider wire format.
        tool_call_id: For a ``tool`` message, the call it answers.
        name: For a ``tool`` message, the tool's name.
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ToolSpec(BaseModel):
    """A tool offered to the model.

    Attributes:
        name: Function name the model calls.
        description: What it does and when to call it.
        parameters: JSON Schema for the arguments.
    """

    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        """Render in the OpenAI ``tools`` array format.

        Returns:
            The tool as the chat completions API expects it.
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class TokenDelta(BaseModel):
    """A fragment of the assistant's spoken answer."""

    kind: Literal["token"] = "token"
    text: str


class ToolCallDelta(BaseModel):
    """A completed tool call, reassembled from streamed argument fragments.

    Providers stream tool arguments in pieces; implementations accumulate them
    and emit this only once the JSON parses (TR-031).

    Attributes:
        call_id: Provider's identifier, echoed back in the ``tool`` reply.
        name: Tool being called.
        arguments: Parsed arguments.
    """

    kind: Literal["tool_call"] = "tool_call"
    call_id: str
    name: str
    arguments: dict[str, Any]


class LLMDone(BaseModel):
    """End of the model's response.

    Attributes:
        finish_reason: Why generation stopped.
    """

    kind: Literal["done"] = "done"
    finish_reason: Literal["stop", "tool_calls", "length", "cancelled", "error"]


LLMEvent = TokenDelta | ToolCallDelta | LLMDone
"""Anything an :class:`LLMProvider` may yield."""

ToolChoice = Literal["auto", "none"]
"""Whether the model may call a tool on a given request.

``"none"`` must be sent *alongside* the tool schemas rather than by omitting
them. Dropping the ``tools`` key makes the server default the choice to none,
and a model that has just called a tool will sometimes try again -- the request
is then rejected with "Tool choice is none, but model called a tool", which is
exactly how the second step of a turn failed in a live run.
"""


@runtime_checkable
class STTProvider(Protocol):
    """Turns a finished utterance into text."""

    name: str

    async def transcribe(self, pcm16: bytes, sample_rate: int = 16_000) -> Transcript:
        """Transcribe one complete utterance.

        Args:
            pcm16: Little-endian 16-bit mono samples.
            sample_rate: Sample rate of ``pcm16`` in hertz.

        Returns:
            The transcript, with empty text when nothing intelligible was said.

        Raises:
            ProviderError: If the provider fails after its own retry.
        """
        ...


@runtime_checkable
class LLMProvider(Protocol):
    """Streams the assistant's answer and its tool calls."""

    name: str

    def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        tool_choice: ToolChoice = "auto",
    ) -> AsyncIterator[LLMEvent]:
        """Stream a response.

        The returned iterator must close its underlying connection when the
        consuming task is cancelled, so that barge-in stops generation rather
        than merely ignoring it.

        Args:
            messages: Conversation history, oldest first, starting with system.
            tools: Tools to declare. Sent even when calls are forbidden, because
                the history may already contain tool calls and the schemas keep
                the request coherent.
            tool_choice: ``"auto"`` lets the model decide; ``"none"`` forbids a
                call while still declaring the tools.

        Yields:
            Token fragments, completed tool calls, and finally one
            :class:`LLMDone`.

        Raises:
            ProviderError: If the request fails.
        """
        ...


@runtime_checkable
class TTSProvider(Protocol):
    """Turns a sentence into playable audio."""

    name: str
    sample_rate: int

    def synthesize(self, text: str, voice: str | None = None) -> AsyncIterator[bytes]:
        """Synthesise one sentence.

        Args:
            text: The sentence to speak. Callers strip markup first.
            voice: Voice identifier, or ``None`` for the configured default.

        Yields:
            Chunks of little-endian 16-bit mono PCM at :attr:`sample_rate`.

        Raises:
            ProviderError: If synthesis fails.
        """
        ...

    async def warm_up(self) -> None:
        """Load weights and run one throwaway synthesis.

        Called during startup so the first real turn does not pay the cost
        (TR-013).
        """
        ...


class Providers(BaseModel):
    """The three provider implementations selected for this process.

    Attributes:
        stt: Speech-to-text implementation.
        llm: Language-model implementation.
        tts: Text-to-speech implementation.
    """

    model_config = {"arbitrary_types_allowed": True}

    stt: Any
    llm: Any
    tts: Any

    @property
    def names(self) -> dict[str, str]:
        """Return the provider names for the health probe and ``session.ready``.

        Returns:
            A mapping of stage to implementation name.
        """
        return {"stt": self.stt.name, "llm": self.llm.name, "tts": self.tts.name}

    async def aclose(self) -> None:
        """Release every provider that holds a resource of its own.

        Called once from the application lifespan's shutdown, which is the only
        place that owns these instances. Closing is duck-typed rather than part
        of the protocols above because most implementations -- every fake, and
        anything wrapping an in-process model -- hold nothing to release, and a
        mandatory empty ``aclose`` on each would be ceremony, not safety.
        """
        for provider in (self.stt, self.llm, self.tts):
            aclose = getattr(provider, "aclose", None)
            if aclose is not None:
                await aclose()
