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

KOKORO: Final = "kokoro"
"""On-device Kokoro-82M synthesis (TR-083)."""

NONE: Final = "none"
"""Value of ``LLM_FALLBACK_PROVIDER`` that leaves the fallback switched off."""

OLLAMA: Final = "ollama"
"""A model served locally by Ollama (TR-084)."""

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
        llm=_build_llm_with_fallback(settings),
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
        from .groq_stt import GroqWhisperSTT  # noqa: PLC0415 - deferred like the others

        key = settings.groq_api_key
        # `_check_groq_credentials` has already run, so a key is present.
        assert key is not None  # noqa: S101
        return GroqWhisperSTT(
            api_key=key.get_secret_value(),
            base_url=settings.groq_base_url,
            model=settings.groq_stt_model,
        )
    if name == "local":
        raise ConfigError(_not_implemented("STT_PROVIDER", name, "FasterWhisperSTT (TR-084)", "M3"))
    raise ConfigError(_unknown("STT_PROVIDER", name, STT_VALUES))


def _build_llm_with_fallback(settings: Settings) -> LLMProvider:
    """Build the model provider, wrapped in a fallback when one is configured.

    Args:
        settings: Loaded application settings.

    Returns:
        The primary provider, or a :class:`~app.providers.fallback.FallbackLLM`
        wrapping it, when ``LLM_FALLBACK_PROVIDER`` names a second one.

    Raises:
        ConfigError: If either provider cannot be built, or if the fallback is
            the same provider as the primary, which would retry a rate limit
            against the quota that just refused it.
    """
    primary = _build_llm(settings)
    name = settings.llm_fallback_provider
    if name == NONE:
        return primary
    if name == settings.llm_provider:
        msg = (
            f"LLM_FALLBACK_PROVIDER={name} is the same as LLM_PROVIDER. A rate limit would be "
            f"retried against the quota that just refused it. Set it to a different provider, "
            f"or to {NONE} to disable the fallback."
        )
        raise ConfigError(msg)

    from .fallback import FallbackLLM  # noqa: PLC0415 - deferred like the others

    fallback = _build_llm(settings.model_copy(update={"llm_provider": name}))
    return FallbackLLM(
        primary=primary,
        fallback=fallback,
        primary_model=_model_name(settings, settings.llm_provider),
        fallback_model=_model_name(settings, name),
    )


def _model_name(settings: Settings, provider: str) -> str:
    """Name the model a provider will use, for the message the listener sees.

    Args:
        settings: Loaded application settings.
        provider: Which provider slot to describe.

    Returns:
        The model identifier, or the provider's own name when it has no model.
    """
    if provider == GROQ:
        return settings.groq_llm_model
    if provider == OLLAMA:
        return settings.ollama_model
    return provider


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
        from tests.fakes import FakeLLM, deck_router  # noqa: PLC0415

        # Routed rather than fixed: `LLM_PROVIDER=fake` exists so the end-to-end suite can drive a
        # real browser against a real server and still assert exact slides and exact words.
        return FakeLLM(router=deck_router)
    if name == OLLAMA:
        from .ollama_llm import OllamaLLM  # noqa: PLC0415 - deferred like the others

        return OllamaLLM(
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )
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
    if name == KOKORO:
        # Deferred import: this module pulls in onnxruntime, which is heavy and
        # pointless for a process running the fakes.
        from .kokoro_tts import KokoroTTS  # noqa: PLC0415

        return KokoroTTS(
            models_dir=settings.kokoro_models_dir,
            voice=settings.kokoro_voice,
            speed=settings.kokoro_speed,
            download=settings.kokoro_download,
        )
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
