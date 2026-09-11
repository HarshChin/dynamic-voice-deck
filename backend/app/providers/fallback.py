"""Answer on a second model when the first one cannot (TR-085).

The hosted model is the only stage of this pipeline with a token budget, and the
free tier's is genuinely reachable: a handful of questions in quick succession
exhausts the per-minute ceiling, and a day's development exhausts the daily one.
Until now that meant the agent went quiet and said so. This makes it answer
instead, more slowly, on a model running on the same machine.

The wrapper is deliberately thin and deliberately narrow. It switches for a rate
limit and nothing else: a malformed request or a genuine upstream fault would
fail on the second model too, and retrying it would only double the wait before
the same error reached the user.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Final

from ..errors import ProviderError
from ..logging_setup import get_logger
from .base import LLMEvent, LLMProvider, Message, ProviderSwitched, ToolChoice, ToolSpec

logger = get_logger(__name__)

PROVIDER_NAME: Final = "fallback"
"""Name reported to the health probe; the wrapped providers report their own."""

RATE_LIMIT_REASON: Final = "the hosted model is out of free-tier capacity"
"""What the listener is told, in words rather than in a status code."""


class FallbackLLM:
    """Try one model, then another, for the one failure a second model can fix.

    Args:
        primary: The model asked first; normally the hosted one.
        fallback: The model asked when the primary is rate limited.
        primary_model: Identifier of the primary, for the message the user sees.
        fallback_model: Identifier of the fallback, likewise.

    Attributes:
        name: Provider name reported to the health probe.
    """

    def __init__(
        self,
        *,
        primary: LLMProvider,
        fallback: LLMProvider,
        primary_model: str,
        fallback_model: str,
    ) -> None:
        self.name = PROVIDER_NAME
        self._primary = primary
        self._fallback = fallback
        self._primary_model = primary_model
        self._fallback_model = fallback_model

    async def aclose(self) -> None:
        """Close both wrapped providers."""
        for provider in (self._primary, self._fallback):
            closer = getattr(provider, "aclose", None)
            if closer is not None:
                await closer()

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        tool_choice: ToolChoice = "auto",
    ) -> AsyncIterator[LLMEvent]:
        """Stream one response, from whichever model can produce it.

        Args:
            messages: Conversation history.
            tools: Tools the model may call.
            tool_choice: Whether a call is permitted.

        Yields:
            The primary's events, or -- after a
            :class:`~app.providers.base.ProviderSwitched` -- the fallback's.

        Raises:
            ProviderError: If the primary failed for any reason other than a
                rate limit, if it failed after it had already started speaking,
                or if the fallback failed too.
        """
        emitted = False
        try:
            async for event in self._primary.stream(messages, tools, tool_choice):
                emitted = True
                yield event
            return
        except ProviderError as exc:
            if emitted:
                # Half an answer has already been spoken. Starting again on
                # another model would repeat it, which is worse than stopping.
                logger.warning("llm.fallback_too_late", error=str(exc))
                raise
            if not _is_rate_limit(exc):
                raise
            switch = ProviderSwitched(
                from_model=self._primary_model,
                to_model=self._fallback_model,
                reason=RATE_LIMIT_REASON,
                retry_after_s=exc.retry_after,
            )

        logger.info(
            "llm.fallback",
            from_model=switch.from_model,
            to_model=switch.to_model,
            retry_after_s=switch.retry_after_s,
        )
        yield switch
        async for event in self._fallback.stream(messages, tools, tool_choice):
            yield event


def _is_rate_limit(exc: ProviderError) -> bool:
    """Report whether a failure is the one a second model can answer through.

    Args:
        exc: The failure raised by the primary.

    Returns:
        ``True`` for a rate limit, which is the only failure worth retrying
        elsewhere: the request was fine and the quota was not.
    """
    return exc.retryable and exc.retry_after is not None
