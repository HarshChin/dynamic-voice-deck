"""A model running on this machine, through Ollama (TR-084).

Ollama exposes the same ``/chat/completions`` endpoint shape as the hosted
providers, so this is a configuration of
:class:`~app.providers.openai_compat.OpenAICompatibleLLM` rather than a second
implementation of streaming, tool-call reassembly and cancellation.

It exists as a *fallback*, not as a peer. The hosted model answers in well under
a second; a seven-billion-parameter model on a laptop has to read the whole
prompt before it says anything, so it answers in seconds. That is a poor
experience and a far better one than silence, which is what the free tier's
daily ceiling otherwise produces (TR-085).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from .openai_compat import OpenAICompatibleLLM

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    import httpx

PROVIDER_NAME: Final = "ollama"
"""Name reported to the health probe, and the key the error mapper uses."""


class OllamaLLM(OpenAICompatibleLLM):
    """A local model served by Ollama.

    No credential is sent. A local endpoint does not want one, and an empty
    bearer token is worse than no header at all.

    Args:
        model: Model tag, from ``OLLAMA_MODEL``; for example ``qwen2.5:7b``.
        base_url: OpenAI-compatible base URL, from ``OLLAMA_BASE_URL``.
        temperature: Sampling temperature, from ``LLM_TEMPERATURE``.
        max_tokens: Response cap, from ``LLM_MAX_TOKENS``.
        client: Pre-built HTTP client, injected by tests.
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        temperature: float,
        max_tokens: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            name=PROVIDER_NAME,
            model=model,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            client=client,
        )
