"""Answering on a second model when the first is rate limited (TR-085)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from app.errors import ProviderError
from app.providers.base import (
    LLMDone,
    LLMEvent,
    LLMProvider,
    Message,
    ProviderSwitched,
    TokenDelta,
    ToolSpec,
)
from app.providers.fallback import FallbackLLM

from .fakes import FakeLLM


def sentence_script(*sentences: str) -> list[LLMEvent]:
    """Build a stream that speaks the given sentences and stops.

    Args:
        *sentences: What the model should say, in order.

    Returns:
        One token per sentence, then a normal completion.
    """
    return [*(TokenDelta(text=sentence) for sentence in sentences), LLMDone(finish_reason="stop")]


class BrokenLLM:
    """A provider that fails, optionally after saying something first."""

    def __init__(self, error: ProviderError, *, before: list[LLMEvent] | None = None) -> None:
        self.name = "broken"
        self._error = error
        self._before = before or []
        self.calls = 0

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        tool_choice: str = "auto",
    ) -> AsyncIterator[LLMEvent]:
        """Yield anything scripted, then fail."""
        self.calls += 1
        for event in self._before:
            yield event
        raise self._error


def rate_limited(seconds: float = 12.0) -> ProviderError:
    """Build the failure a free tier produces.

    Args:
        seconds: What the upstream said to wait.

    Returns:
        A retryable provider error carrying a retry-after.
    """
    return ProviderError("groq_llm", "HTTP 429", retryable=True, retry_after=seconds)


def build(primary: Any, fallback: Any) -> FallbackLLM:
    """Compose two providers.

    Args:
        primary: The one asked first.
        fallback: The one asked after a rate limit.

    Returns:
        The wrapper.
    """
    return FallbackLLM(
        primary=primary,
        fallback=fallback,
        primary_model="qwen/qwen3.8-27b",
        fallback_model="qwen2.5:7b",
    )


async def drain(provider: LLMProvider) -> list[LLMEvent]:
    """Collect a whole stream.

    Args:
        provider: The provider to read.

    Returns:
        Every event it yielded.
    """
    return [event async for event in provider.stream([], [], "auto")]


async def test_the_primary_answers_and_nothing_is_substituted() -> None:
    """TC-BE-312: TR-085 -- the wrapper is invisible when the hosted model works."""
    primary = FakeLLM(sentence_script("Two layers, actually."))
    fallback = FakeLLM(sentence_script("Should never be asked."))

    events = await drain(build(primary, fallback))

    assert not any(isinstance(event, ProviderSwitched) for event in events)
    assert fallback.calls == []
    assert [event.text for event in events if isinstance(event, TokenDelta)] == [
        "Two layers, actually."
    ]


async def test_a_rate_limit_is_answered_by_the_local_model() -> None:
    """TC-BE-313: TR-085 -- the turn is answered rather than abandoned."""
    primary = BrokenLLM(rate_limited(12.0))
    fallback = FakeLLM(sentence_script("Answering locally."))

    events = await drain(build(primary, fallback))

    switch = next(event for event in events if isinstance(event, ProviderSwitched))
    assert switch.from_model == "qwen/qwen3.8-27b"
    assert switch.to_model == "qwen2.5:7b"
    assert switch.retry_after_s == pytest.approx(12.0)
    # The announcement comes before a word of the answer, so the log explains itself.
    assert events.index(switch) == 0
    assert [event.text for event in events if isinstance(event, TokenDelta)] == [
        "Answering locally."
    ]
    assert isinstance(events[-1], LLMDone)


async def test_a_failure_that_is_not_a_rate_limit_is_not_retried() -> None:
    """TC-BE-314: TR-085 -- a malformed request fails the same way on both models.

    Retrying it would only double the wait before the same error reached the
    user, and it would spend local compute proving the point.
    """
    primary = BrokenLLM(ProviderError("groq_llm", "HTTP 400 bad request"))
    fallback = FakeLLM()

    with pytest.raises(ProviderError, match="400"):
        await drain(build(primary, fallback))

    assert fallback.calls == []


async def test_a_rate_limit_with_no_retry_after_is_not_retried() -> None:
    """TC-BE-315: TR-085 -- the retry-after is what distinguishes a quota from a fault."""
    primary = BrokenLLM(ProviderError("groq_llm", "HTTP 503", retryable=True))
    fallback = FakeLLM()

    with pytest.raises(ProviderError):
        await drain(build(primary, fallback))

    assert fallback.calls == []


async def test_a_failure_after_the_answer_started_is_not_retried() -> None:
    """TC-BE-316: TR-085 -- half an answer has been spoken; starting again would repeat it.

    This is the invariant that makes the wrapper safe to put in front of a live
    turn: it may only substitute before the listener has heard anything.
    """
    primary = BrokenLLM(rate_limited(), before=[TokenDelta(text="Two layers, actually.")])
    fallback = FakeLLM(sentence_script("Would repeat the opener."))

    with pytest.raises(ProviderError):
        await drain(build(primary, fallback))

    assert fallback.calls == []


async def test_the_local_model_gets_the_same_conversation_and_tools() -> None:
    """TC-BE-317: TR-085 -- the substitution is of the model, not of the question."""
    primary = BrokenLLM(rate_limited())
    fallback = FakeLLM(sentence_script("Answering locally."))
    messages = [Message(role="user", content="How do you handle interruptions?")]
    tools = [
        ToolSpec(name="go_to_slide", description="Move the deck", parameters={"type": "object"})
    ]

    wrapper = build(primary, fallback)
    [event async for event in wrapper.stream(messages, tools, "auto")]

    assert fallback.calls[0].messages == messages
    assert fallback.calls[0].tools == tools


async def test_closing_the_wrapper_closes_both_providers() -> None:
    """TC-BE-318: a fallback nobody closed is a connection pool nobody closed."""
    closed: list[str] = []

    class Closable(FakeLLM):
        def __init__(self, label: str) -> None:
            super().__init__()
            self._label = label

        async def aclose(self) -> None:
            closed.append(self._label)

    await build(Closable("primary"), Closable("fallback")).aclose()

    assert closed == ["primary", "fallback"]
