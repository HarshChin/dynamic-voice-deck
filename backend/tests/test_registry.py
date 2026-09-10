"""Tests for :mod:`app.providers.registry` — selection, credentials, isolation.

The registry is startup code, so its whole job is to fail in a way the reader
can act on: name the variable, name the valid values, name the file that
documents the credential (TR-080, TR-176). These tests assert on the *content*
of those messages rather than only on the exception type, because a
:class:`~app.errors.ConfigError` that says nothing useful is barely better than
a stack trace.

Settings are built with ``model_copy(update=...)``, which deliberately skips
validation. That is the only way to reach the registry's defensive
"unknown value" branch: :class:`~app.config.Settings` types each selection as a
``Literal``, so in a running process pydantic rejects a typo first. The branch
still has to exist, and still has to be right, for anything that builds a
``Settings`` by hand.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Final

import pytest
from app import main
from app.config import BACKEND_ROOT, Settings
from app.errors import ConfigError
from app.pipeline.tools import build_tools
from app.providers.base import (
    LLMDone,
    LLMProvider,
    Message,
    Providers,
    STTProvider,
    TokenDelta,
    TTSProvider,
)
from app.providers.groq_llm import PROVIDER_NAME, GroqLLM
from app.providers.registry import (
    ENV_EXAMPLE,
    GROQ,
    LLM_VALUES,
    STT_VALUES,
    TTS_VALUES,
    build_providers,
)
from pydantic import SecretStr, ValidationError

from tests.fakes import (
    BYTES_PER_CHAR,
    CHUNK_BYTES,
    DEFAULT_TRANSCRIPT,
    FakeLLM,
    FakeSTT,
    FakeTTS,
)

FAKE: Final = "fake"
API_KEY: Final = SecretStr("gsk-test-key-not-real")
UNKNOWN: Final = "bogus"
SLIDE_COUNT: Final = 6
SCRIPTED_AUDIO: Final = b"\x00\x01\x02\x03"
MESSAGES: Final = [Message(role="user", content="What is on slide two?")]

IMPORT_PROBE: Final = """
import json
import sys

from app.config import Settings
from app.providers.registry import build_providers

loaded = lambda: {
    "groq": "app.providers.groq_llm" in sys.modules,
    "fakes": "tests.fakes" in sys.modules,
}
after_import = loaded()
providers = build_providers(
    Settings(_env_file=None, stt_provider="fake", llm_provider="fake", tts_provider="fake")
)
print("PROBE:" + json.dumps({
    "after_import": after_import,
    "after_build": loaded(),
    "llm": providers.names["llm"],
}))
"""
"""Program run in a clean interpreter by TC-BE-159.

A fresh process is the only honest way to ask "what did importing this pull
in?": inside the test session, pytest has already imported ``tests.fakes`` and
every other module the suite touches.
"""


def _configured(settings: Settings, **overrides: object) -> Settings:
    """Return settings with an all-fake stack, then the given overrides.

    Args:
        settings: Isolated base settings from the ``settings`` fixture.
        overrides: Fields to change, applied without validation so that a test
            can supply a value ``Settings`` itself would reject.

    Returns:
        The adjusted settings.
    """
    stack: dict[str, object] = {
        "stt_provider": FAKE,
        "llm_provider": FAKE,
        "tts_provider": FAKE,
    }
    return settings.model_copy(update={**stack, **overrides})


@pytest.mark.parametrize(
    ("field", "variable", "valid"),
    [
        pytest.param("stt_provider", "STT_PROVIDER", STT_VALUES, id="stt"),
        pytest.param("llm_provider", "LLM_PROVIDER", LLM_VALUES, id="llm"),
        pytest.param("tts_provider", "TTS_PROVIDER", TTS_VALUES, id="tts"),
    ],
)
def test_unknown_provider_name_fails_listing_the_valid_values(
    settings: Settings,
    field: str,
    variable: str,
    valid: tuple[str, ...],
) -> None:
    """TC-BE-076: an unrecognised provider name fails fast, listing what is accepted.

    Both gates are checked: pydantic rejects the value when it comes from the
    environment, and the registry rejects it when a ``Settings`` was built
    around validation.
    """
    with pytest.raises(ValidationError) as rejected:
        Settings(**{field: UNKNOWN})
    for value in valid:
        assert repr(value) in str(rejected.value)

    with pytest.raises(ConfigError) as raised:
        build_providers(_configured(settings, **{field: UNKNOWN}))

    assert variable in str(raised.value)
    assert UNKNOWN in str(raised.value)
    for value in valid:
        assert value in str(raised.value)


@pytest.mark.parametrize(
    ("overrides", "expected_variables"),
    [
        pytest.param({"llm_provider": "groq"}, ["LLM_PROVIDER"], id="llm-only"),
        pytest.param({"stt_provider": "groq"}, ["STT_PROVIDER"], id="stt-only"),
        pytest.param(
            {"stt_provider": "groq", "llm_provider": "groq"},
            ["STT_PROVIDER", "LLM_PROVIDER"],
            id="both",
        ),
    ],
)
def test_missing_groq_key_fails_naming_env_example(
    settings: Settings,
    overrides: dict[str, object],
    expected_variables: list[str],
) -> None:
    """TC-BE-077: a groq provider with no GROQ_API_KEY fails, naming `.env.example`."""
    assert settings.groq_api_key is None

    with pytest.raises(ConfigError) as raised:
        build_providers(_configured(settings, **overrides))

    message = str(raised.value)
    assert "GROQ_API_KEY" in message
    assert ENV_EXAMPLE in message
    for variable in expected_variables:
        assert variable in message


@pytest.mark.parametrize(
    ("overrides", "variable", "implementation", "milestone"),
    [
        pytest.param(
            {"stt_provider": "groq", "groq_api_key": API_KEY},
            "STT_PROVIDER",
            "GroqWhisperSTT",
            "M3",
            id="groq-stt",
        ),
        pytest.param(
            {"stt_provider": "local"}, "STT_PROVIDER", "FasterWhisperSTT", "M3", id="local-stt"
        ),
        pytest.param(
            {"llm_provider": "ollama"}, "LLM_PROVIDER", "OllamaLLM", "M4", id="ollama-llm"
        ),
        pytest.param(
            {"tts_provider": "kokoro"}, "TTS_PROVIDER", "KokoroTTS", "M2", id="kokoro-tts"
        ),
    ],
)
def test_providers_from_later_milestones_say_when_they_arrive(
    settings: Settings,
    overrides: dict[str, object],
    variable: str,
    implementation: str,
    milestone: str,
) -> None:
    """TC-BE-158: selecting an unshipped provider names it and its milestone.

    Phase 1 is the text loop, so anyone running the default ``.env`` hits this
    for STT and TTS. The message has to say which milestone adds the real thing
    and how to run today, not merely that something is missing.
    """
    with pytest.raises(ConfigError) as raised:
        build_providers(_configured(settings, **overrides))

    message = str(raised.value)
    assert variable in message
    assert implementation in message
    assert milestone in message
    assert f"{variable}={FAKE}" in message


async def test_selectable_providers_satisfy_their_protocols_and_the_fakes_run(
    settings: Settings,
) -> None:
    """TC-BE-157: every selectable provider satisfies its protocol, and the fakes work.

    The protocols are ``runtime_checkable``, so ``isinstance`` proves the
    attributes exist, and the registry's own return annotations make mypy prove
    the signatures match -- which ``isinstance`` cannot see. Neither check runs
    the doubles, so this also drives one call through each of them: a fake that
    satisfies the protocol but returns nothing usable would fail every pipeline
    test downstream with a confusing message rather than this one.
    """
    fakes = build_providers(_configured(settings))

    assert isinstance(fakes, Providers)
    assert isinstance(fakes.stt, FakeSTT)
    assert isinstance(fakes.llm, FakeLLM)
    assert isinstance(fakes.tts, FakeTTS)
    assert isinstance(fakes.stt, STTProvider)
    assert isinstance(fakes.llm, LLMProvider)
    assert isinstance(fakes.tts, TTSProvider)
    assert fakes.names == {"stt": "fake_stt", "llm": "fake_llm", "tts": "fake_tts"}

    transcript = await fakes.stt.transcribe(SCRIPTED_AUDIO)
    assert transcript.text == DEFAULT_TRANSCRIPT
    assert fakes.stt.calls[0].pcm16 == SCRIPTED_AUDIO

    tools = build_tools(SLIDE_COUNT)
    events = [event async for event in fakes.llm.stream(MESSAGES, tools)]
    assert [type(event) for event in events] == [TokenDelta, LLMDone]
    assert fakes.llm.calls[0].messages == MESSAGES
    assert fakes.llm.calls[0].tools == tools

    chunks = [chunk async for chunk in fakes.tts.synthesize("Ready.")]
    audio = b"".join(chunks)
    assert len(audio) == len("Ready.") * BYTES_PER_CHAR
    assert all(len(chunk) <= CHUNK_BYTES and not len(chunk) % 2 for chunk in chunks)
    assert audio == b"".join([c async for c in FakeTTS().synthesize("Ready.")])
    assert fakes.tts.calls[0].text == "Ready."

    with_groq = build_providers(_configured(settings, llm_provider="groq", groq_api_key=API_KEY))

    assert isinstance(with_groq.llm, GroqLLM)
    assert isinstance(with_groq.llm, LLMProvider)
    assert with_groq.names["llm"] == PROVIDER_NAME
    await with_groq.llm.aclose()


def test_selecting_fake_never_imports_groq_and_production_never_imports_fakes() -> None:
    """TC-BE-159: the registry imports only the implementation it was asked for.

    Importing test code into a production process is what CLAUDE.md §3.5 rules
    out, and the guard is a local import that no in-process assertion can see --
    hence the clean interpreter.
    """
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
        [sys.executable, "-c", IMPORT_PROBE],
        capture_output=True,
        text=True,
        cwd=BACKEND_ROOT,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    # Unconfigured structlog prints to stdout, so find the probe's own line.
    line = next(out for out in completed.stdout.splitlines() if out.startswith("PROBE:"))
    probe = json.loads(line.removeprefix("PROBE:"))

    assert probe["after_import"] == {"groq": False, "fakes": False}
    assert probe["after_build"] == {"groq": False, "fakes": True}
    assert probe["llm"] == "fake_llm"


# --- Teardown ----------------------------------------------------------------
#
# Provider lifetime belongs here rather than with the startup tests: the
# registry is what constructs these instances, and `Providers.aclose` is the
# only seam through which anything gives them back.


async def test_closing_the_providers_releases_the_ones_that_hold_something() -> None:
    """TC-BE-178: aclose closes a provider that has one and steps over those that do not.

    Only the Groq provider owns anything -- an httpx connection pool -- and the
    fakes deliberately expose no ``aclose`` at all, because most providers hold
    nothing to release and a mandatory empty method on each would be ceremony
    rather than safety. Both halves have to work: a leaked pool warns on garbage
    collection and keeps sockets open across a reload, and a teardown that
    insisted on the method would crash shutting down the fake stack every
    end-to-end test runs against.
    """
    llm = GroqLLM(
        api_key=API_KEY.get_secret_value(),
        model="openai/gpt-oss-120b",
        base_url="https://api.groq.test/openai/v1",
        temperature=0.4,
        max_tokens=350,
    )
    providers = Providers(stt=FakeSTT(), llm=llm, tts=FakeTTS())
    assert not hasattr(providers.stt, "aclose")

    await providers.aclose()

    assert llm._client.is_closed is True
    # Idempotent: shutting down twice must not raise on the way out.
    await providers.aclose()


async def test_the_lifespan_closes_the_providers_it_built(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    """TC-BE-179: application shutdown gives back the LLM's connection pool.

    ``GroqLLM.aclose`` existed from the first commit and nothing called it, so
    the pool outlived the process's own shutdown. The lifespan is the only owner
    of the providers, which makes it the only place that can.

    Logging is stubbed out because ``configure_logging`` rewrites process-wide
    handlers and structlog's configuration; this test is about provider
    teardown, and the startup suite owns the logging assertions along with the
    fixture that undoes them.
    """
    monkeypatch.setattr(main, "configure_logging", lambda _settings: None)
    monkeypatch.setenv("STT_PROVIDER", FAKE)
    monkeypatch.setenv("LLM_PROVIDER", GROQ)
    monkeypatch.setenv("TTS_PROVIDER", FAKE)
    monkeypatch.setenv("GROQ_API_KEY", API_KEY.get_secret_value())
    assert settings.groq_api_key is None  # The fixture scrubbed the developer's own key.

    app = main.create_app()
    async with main.lifespan(app):
        providers = app.state.app_state.providers
        assert isinstance(providers.llm, GroqLLM)
        assert providers.llm._client.is_closed is False

    assert providers.llm._client.is_closed is True
