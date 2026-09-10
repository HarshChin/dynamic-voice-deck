"""Provider selection: turn configuration into the three implementations (TR-080).

This is the only module that knows which class implements which
``*_PROVIDER`` value, so the pipeline can be handed a
:class:`~app.providers.base.Providers` and never learn a vendor name.

Selection fails loudly and early. A process that cannot build its providers
cannot serve a turn, so every problem -- an unknown name, a credential that is
not set, a provider whose milestone has not landed -- is raised as
:class:`~app.errors.ConfigError` during startup, with a message that names the
environment variable to change. The alternative, discovering it when the first
question arrives, turns a typo into a failed demo.

Phase 1 (milestone M1, the text loop) needs only a real LLM. The STT and TTS
implementations land with M3 and M2; until then those stages must be set to
``fake``, and the errors below say so.

Every implementation is imported inside the branch that selects it, which is
the one place in the backend where a local import earns its keep. Selecting
``fake`` must not import a vendor client, and -- more importantly -- selecting
anything else must not import :mod:`tests.fakes`, because a production process
has no business importing the test package (CLAUDE.md §3.5).
"""

from __future__ import annotations

from typing import Final, get_args

from ..config import LlmProviderName, Settings, SttProviderName, TtsProviderName
from ..errors import ConfigError
from ..logging_setup import get_logger
from .base import LLMProvider, Providers, STTProvider, TTSProvider

logger = get_logger(__name__)

GROQ: Final = "groq"
"""Provider value that needs ``GROQ_API_KEY``."""

FAKE: Final = "fake"
"""Provider value served from :mod:`tests.fakes`."""

ENV_EXAMPLE: Final = ".env.example"
"""File that documents every variable, named in configuration errors (TR-176)."""

STT_VALUES: Final[tuple[str, ...]] = get_args(SttProviderName)
LLM_VALUES: Final[tuple[str, ...]] = get_args(LlmProviderName)
TTS_VALUES: Final[tuple[str, ...]] = get_args(TtsProviderName)
"""Valid values, read from the ``Settings`` literals so the two cannot drift."""


def build_providers(settings: Settings) -> Providers:
    """Build the STT, LLM, and TTS implementations named in the settings.

    Args:
        settings: Loaded application settings.

    Returns:
        The three implementations, ready to be shared by every session.

    Raises:
        ConfigError: If a provider name is unknown, if a selected ``groq``
            provider has no API key, or if the selected implementation has not
            shipped yet.
    """
    # Checked before anything is constructed so that the missing-credential
    # message wins over a "not implemented yet" one: a key that is absent now
    # will still be absent in the milestone that adds the provider.
    _check_groq_credentials(settings)

    providers = Providers(
        stt=_build_stt(settings),
        llm=_build_llm(settings),
        tts=_build_tts(settings),
    )
    logger.info("providers.selected", **providers.names)
    return providers


def _build_stt(settings: Settings) -> STTProvider:
    """Build the speech-to-text provider.

    Args:
        settings: Loaded application settings.

    Returns:
        The selected implementation.

    Raises:
        ConfigError: If the name is unknown or not implemented yet.
    """
    name = settings.stt_provider
    if name == FAKE:
        from tests.fakes import FakeSTT  # noqa: PLC0415

        return FakeSTT()
    if name == GROQ:
        raise ConfigError(_not_implemented("STT_PROVIDER", name, "GroqWhisperSTT (TR-081)", "M3"))
    if name == "local":
        raise ConfigError(_not_implemented("STT_PROVIDER", name, "FasterWhisperSTT (TR-084)", "M3"))
    raise ConfigError(_unknown("STT_PROVIDER", name, STT_VALUES))


def _build_llm(settings: Settings) -> LLMProvider:
    """Build the language-model provider.

    Args:
        settings: Loaded application settings.

    Returns:
        The selected implementation.

    Raises:
        ConfigError: If the name is unknown or not implemented yet.
    """
    name = settings.llm_provider
    if name == GROQ:
        from .groq_llm import GroqLLM  # noqa: PLC0415

        return GroqLLM(
            api_key=_groq_api_key(settings),
            model=settings.groq_llm_model,
            base_url=settings.groq_base_url,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )
    if name == FAKE:
        from tests.fakes import FakeLLM  # noqa: PLC0415

        return FakeLLM()
    if name == "ollama":
        raise ConfigError(_not_implemented("LLM_PROVIDER", name, "OllamaLLM (TR-084)", "M4"))
    raise ConfigError(_unknown("LLM_PROVIDER", name, LLM_VALUES))


def _build_tts(settings: Settings) -> TTSProvider:
    """Build the text-to-speech provider.

    Args:
        settings: Loaded application settings.

    Returns:
        The selected implementation.

    Raises:
        ConfigError: If the name is unknown or not implemented yet.
    """
    name = settings.tts_provider
    if name == FAKE:
        from tests.fakes import FakeTTS  # noqa: PLC0415

        return FakeTTS()
    if name == "kokoro":
        raise ConfigError(_not_implemented("TTS_PROVIDER", name, "KokoroTTS (TR-083)", "M2"))
    raise ConfigError(_unknown("TTS_PROVIDER", name, TTS_VALUES))


def _check_groq_credentials(settings: Settings) -> None:
    """Fail early when a groq provider is selected without a key (TR-176).

    Args:
        settings: Loaded application settings.

    Raises:
        ConfigError: If any selected provider is ``groq`` and ``GROQ_API_KEY``
            is unset or blank.
    """
    stages = [
        stage
        for stage, value in (
            ("STT_PROVIDER", settings.stt_provider),
            ("LLM_PROVIDER", settings.llm_provider),
        )
        if value == GROQ
    ]
    if stages:
        _groq_api_key(settings, selected_by=stages)


def _groq_api_key(settings: Settings, selected_by: list[str] | None = None) -> str:
    """Return the Groq API key, or explain how to set it.

    Args:
        settings: Loaded application settings.
        selected_by: Variables that selected a groq provider, named in the
            message so the reader knows which one to change.

    Returns:
        The secret's plain value, for the ``Authorization`` header.

    Raises:
        ConfigError: If the key is unset or blank.
    """
    if settings.groq_api_key is not None:
        return settings.groq_api_key.get_secret_value()

    stages = selected_by or ["LLM_PROVIDER"]
    variables = " and ".join(stages)
    msg = (
        f"GROQ_API_KEY is not set, but {variables} selects the groq provider. "
        f"Add the key to .env at the repository root (every variable is documented "
        f"in {ENV_EXAMPLE}), or select a provider that needs no credential, "
        f"such as {stages[0]}={FAKE}."
    )
    raise ConfigError(msg)


def _not_implemented(variable: str, value: str, implementation: str, milestone: str) -> str:
    """Build the message for a provider whose milestone has not landed.

    Args:
        variable: Environment variable that selected it.
        value: The selected value.
        implementation: Class and requirement that will implement it.
        milestone: Release-plan milestone that adds it (TRD §15).

    Returns:
        The message for the raised :class:`~app.errors.ConfigError`.
    """
    return (
        f"{variable}={value} selects {implementation}, which is not implemented yet; "
        f"it arrives with milestone {milestone}. Set {variable}={FAKE} to run the "
        f"current milestone."
    )


def _unknown(variable: str, value: str, valid: tuple[str, ...]) -> str:
    """Build the message for a provider name that does not exist.

    Args:
        variable: Environment variable that selected it.
        value: The unrecognised value.
        valid: Every accepted value.

    Returns:
        The message for the raised :class:`~app.errors.ConfigError`.
    """
    options = ", ".join(valid)
    return f"Unknown {variable}={value!r}. Valid values: {options}."
