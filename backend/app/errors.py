"""Application error hierarchy.

Every error raised deliberately by this application derives from :class:`AppError`.
Vendor SDK exceptions are wrapped at the ``providers`` boundary so that no
third-party exception type escapes into the pipeline (see TRD TR-085).
"""

from __future__ import annotations


class AppError(Exception):
    """Base class for all application errors."""


class ConfigError(AppError):
    """Raised when configuration is missing or invalid.

    This is a startup-time failure: the process should exit rather than serve
    traffic it cannot handle.
    """


class DeckError(AppError):
    """Raised when a deck file is missing, malformed, or fails validation."""


class ProtocolError(AppError):
    """Raised when a WebSocket message violates the protocol.

    Args:
        code: Stable error code sent to the client, e.g. ``"bad_message"``.
        message: Human-readable explanation.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ProviderError(AppError):
    """Raised when an STT, LLM, or TTS provider fails.

    Args:
        provider: Provider name, e.g. ``"groq_llm"``.
        message: Human-readable explanation.
        retryable: Whether retrying the same request could succeed.
        retry_after: Seconds the upstream asked us to wait, when supplied.
    """

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after
