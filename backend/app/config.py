"""Application configuration.

Settings are read from environment variables and from the ``.env`` file at the
repository root (one level above ``backend/``). Secrets are masked in every
representation so that logging a ``Settings`` object cannot leak an API key
(TRD TR-180).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
"""Absolute path to the repository root, which is where ``.env`` lives."""

BACKEND_ROOT = Path(__file__).resolve().parents[1]
"""Absolute path to the ``backend/`` directory."""

MODELS_DIR = BACKEND_ROOT / "models"
"""Directory holding downloaded model weights; git-ignored."""

SttProviderName = Literal["groq", "local", "fake"]
LlmProviderName = Literal["groq", "ollama", "fake"]
TtsProviderName = Literal["kokoro", "fake"]


class Settings(BaseSettings):
    """Runtime configuration for the backend.

    Attributes are populated from environment variables of the same name,
    case-insensitively, with values in ``.env`` taking lower precedence than
    real environment variables.
    """

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Provider selection -------------------------------------------------
    stt_provider: SttProviderName = "groq"
    llm_provider: LlmProviderName = "groq"
    tts_provider: TtsProviderName = "kokoro"

    # --- Credentials --------------------------------------------------------
    groq_api_key: SecretStr | None = None

    # --- Model identifiers --------------------------------------------------
    groq_stt_model: str = "whisper-large-v3-turbo"
    groq_llm_model: str = "qwen/qwen3.8-27b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "llama3.1:8b"

    # --- Generation tuning --------------------------------------------------
    llm_temperature: float = Field(default=0.4, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(default=350, gt=0, le=4096)
    kokoro_voice: str = "af_heart"
    kokoro_speed: float = Field(default=1.0, gt=0.0, le=3.0)

    # --- Server -------------------------------------------------------------
    backend_host: str = "0.0.0.0"  # noqa: S104 - local development server
    backend_port: int = Field(default=8000, gt=0, lt=65536)
    cors_origins: list[str] = Field(
        default=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    )

    # --- Observability ------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = False

    # --- Pipeline tuning ----------------------------------------------------
    turn_timeout_s: float = Field(default=20.0, gt=0.0)
    max_history_turns: int = Field(default=20, gt=0)
    max_json_message_bytes: int = Field(default=16_384, gt=0)
    max_utterance_bytes: int = Field(default=2_097_152, gt=0)

    @field_validator("groq_api_key", mode="after")
    @classmethod
    def _empty_secret_is_no_secret(cls, value: SecretStr | None) -> SecretStr | None:
        """Treat a blank credential as absent rather than as a set-but-empty one.

        ``cp .env.example .env`` leaves ``GROQ_API_KEY=`` in the file, which
        pydantic reads as ``SecretStr("")``. Without this normalisation an
        ``is None`` guard would pass and the failure would surface much later as
        an opaque ``401`` from the provider, rather than as the actionable
        startup error TR-176 requires.

        Args:
            value: The parsed secret, possibly empty.

        Returns:
            ``None`` when the secret is absent or blank, otherwise the secret.
        """
        if value is None or not value.get_secret_value().strip():
            return None
        return value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        """Normalise and validate the logging level name.

        Args:
            value: Level name in any case, e.g. ``"debug"``.

        Returns:
            The upper-case level name.

        Raises:
            ValueError: If the name is not a standard logging level.
        """
        normalised = value.upper()
        # NOTSET is deliberately excluded. On the *root* logger it means level 0,
        # which emits everything -- strictly more verbose than DEBUG, the opposite
        # of what the name suggests to most readers. Allowing it would also switch
        # on the DEBUG-level transcript logging that TR-181 gates, by accident.
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if normalised not in allowed:
            msg = f"log_level must be one of {sorted(allowed)}, got {value!r}"
            raise ValueError(msg)
        return normalised

    def masked_dump(self) -> dict[str, object]:
        """Return settings as a dictionary with secrets replaced by a placeholder.

        Returns:
            A mapping safe to log or serialise.
        """
        data = self.model_dump()
        for key, value in list(data.items()):
            if isinstance(getattr(self, key, None), SecretStr):
                data[key] = "***" if value else None
        return data

    def __repr__(self) -> str:
        """Return a representation with secrets masked."""
        return f"{type(self).__name__}({self.masked_dump()!r})"

    __str__ = __repr__


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Returns:
        The cached :class:`Settings` instance.
    """
    return Settings()
