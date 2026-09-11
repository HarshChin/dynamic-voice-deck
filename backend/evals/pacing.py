"""Space model calls out so the free tier never has to refuse one.

Groq's per-minute limit counts the tokens of a *refused* request against the same
window the request was refused for -- observed as ``Used 5406`` one second after a
run with zero successful calls was stopped. So reacting to a 429 by retrying on the
advertised ``retry-after`` re-fills the window and loops forever. The only pacing
that works against that limiter is proactive: know the ceiling, and never ask for
more than fits.

At roughly 2,800 input tokens a call and a 7,000-token minute, two calls a minute
is the ceiling, so 31 seconds between calls is the default spacing a run asks for.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from app.providers.base import LLMEvent, LLMProvider, Message, ToolChoice, ToolSpec


class Pacer:
    """Hand out start times at least ``min_interval_s`` apart, process-wide.

    One instance is shared by every provider that draws on the same account, so
    the subject model and the judge cannot between them exceed the account's
    per-minute allowance.

    Args:
        min_interval_s: Seconds between consecutive calls.
    """

    def __init__(self, min_interval_s: float) -> None:
        self._interval = min_interval_s
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def wait(self) -> None:
        """Block until the next call may start, and reserve that slot."""
        async with self._lock:
            now = time.monotonic()
            delay = self._next_allowed - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_allowed = max(now, self._next_allowed) + self._interval


class PacedLLM:
    """A provider that waits its turn before every call.

    Args:
        inner: The provider doing the work.
        pacer: The shared pacer to wait on.

    Attributes:
        name: The inner provider's name; pacing is not a property of the model.
    """

    def __init__(self, inner: LLMProvider, pacer: Pacer) -> None:
        self._inner = inner
        self._pacer = pacer
        self.name = inner.name

    async def aclose(self) -> None:
        """Close the inner provider, if it can be closed."""
        closer = getattr(self._inner, "aclose", None)
        if closer is not None:
            await closer()

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        tool_choice: ToolChoice = "auto",
    ) -> AsyncIterator[LLMEvent]:
        """Wait for a slot, then stream from the inner provider.

        Args:
            messages: Conversation history.
            tools: Tools the model may call.
            tool_choice: Whether a call is permitted.

        Yields:
            The inner provider's events, unchanged.
        """
        await self._pacer.wait()
        async for event in self._inner.stream(messages, tools, tool_choice):
            yield event
