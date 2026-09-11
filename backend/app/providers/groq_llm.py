"""Groq's hosted chat completions (TR-082).

Groq speaks the OpenAI protocol, so everything hard about streaming it --
reassembling tool-call arguments, the three SSE line terminators, releasing the
connection the instant a turn is cancelled -- lives in
:mod:`app.providers.openai_compat` and is shared with the local provider. What
is Groq-specific is exactly one thing: it needs a credential, and running
without one is a configuration mistake worth failing loudly at startup rather
than discovering on the user's first question.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from ..errors import ConfigError
from .openai_compat import (
    CONNECT_TIMEOUT_S,
    POOL_TIMEOUT_S,
    READ_TIMEOUT_S,
    WRITE_TIMEOUT_S,
    OpenAICompatibleLLM,
)

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    import httpx

__all__ = [
    "CONNECT_TIMEOUT_S",
    "POOL_TIMEOUT_S",
    "PROVIDER_NAME",
    "READ_TIMEOUT_S",
    "WRITE_TIMEOUT_S",
    "GroqLLM",
]

PROVIDER_NAME: Final = "groq_llm"
"""Name reported to the health probe, and the key the error mapper uses."""


class GroqLLM(OpenAICompatibleLLM):
    """The hosted model provider, which is the default (TR-082).

    Args:
        api_key: Groq API key. Blank or missing is a configuration failure.
        model: Model identifier, from ``GROQ_LLM_MODEL``.
        base_url: OpenAI-compatible base URL, from ``GROQ_BASE_URL``.
        temperature: Sampling temperature, from ``LLM_TEMPERATURE``.
        max_tokens: Response cap, from ``LLM_MAX_TOKENS``.
        client: Pre-built HTTP client, injected by tests.

    Raises:
        ConfigError: If ``api_key`` is missing or blank.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        temperature: float,
        max_tokens: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            msg = (
                "GroqLLM needs a Groq API key. Set GROQ_API_KEY in .env "
                "(see .env.example) or select LLM_PROVIDER=fake."
            )
            raise ConfigError(msg)
        super().__init__(
            name=PROVIDER_NAME,
            model=model,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            api_key=api_key,
            client=client,
        )
