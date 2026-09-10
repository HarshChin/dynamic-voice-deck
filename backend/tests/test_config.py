"""Tests for :mod:`app.config` — defaults, validation, secret masking, paths."""

from __future__ import annotations

from pathlib import Path

import pytest
from app import config
from app.config import BACKEND_ROOT, MODELS_DIR, REPO_ROOT, Settings, get_settings
from pydantic import ValidationError

FAKE_SECRET = "sk-SHOULD-NEVER-APPEAR"  # noqa: S105 - deliberate stand-in, not a real credential
"""Distinctive stand-in for a real key: easy to grep for in any representation."""


def test_provider_defaults_are_the_declared_values(settings: Settings) -> None:
    """TC-BE-090: provider selection defaults to groq / groq / kokoro, no key."""
    assert settings.stt_provider == "groq"
    assert settings.llm_provider == "groq"
    assert settings.tts_provider == "kokoro"
    assert settings.groq_api_key is None


def test_server_generation_and_pipeline_defaults_are_the_declared_values(
    settings: Settings,
) -> None:
    """TC-BE-091: every non-provider default matches the declaration."""
    assert settings.backend_host == "0.0.0.0"  # noqa: S104 - asserting the declared default
    assert settings.backend_port == 8000
    assert settings.cors_origins == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    assert settings.groq_stt_model == "whisper-large-v3-turbo"
    assert settings.groq_llm_model == "qwen/qwen3.8-27b"
    assert settings.groq_base_url == "https://api.groq.com/openai/v1"
    assert settings.ollama_base_url == "http://localhost:11434/v1"
    assert settings.ollama_model == "llama3.1:8b"

    assert settings.llm_temperature == 0.4
    assert settings.llm_max_tokens == 350
    assert settings.kokoro_voice == "af_heart"
    assert settings.kokoro_speed == 1.0

    assert settings.log_level == "INFO"
    assert settings.log_json is False

    assert settings.turn_timeout_s == 20.0
    assert settings.max_history_turns == 20
    assert settings.max_json_message_bytes == 16_384
    assert settings.max_utterance_bytes == 2_097_152


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("debug", "DEBUG"),
        ("Info", "INFO"),
        ("warning", "WARNING"),
        ("ErRoR", "ERROR"),
        ("critical", "CRITICAL"),
        ("DEBUG", "DEBUG"),
    ],
)
def test_log_level_is_normalised_to_upper_case(
    monkeypatch: pytest.MonkeyPatch,
    given: str,
    expected: str,
) -> None:
    """TC-BE-092: a level in any case is accepted and stored upper-case."""
    monkeypatch.setenv("LOG_LEVEL", given)

    assert Settings().log_level == expected
    assert Settings(log_level=given).log_level == expected


# "notset" is rejected on purpose: on the root logger NOTSET means level 0, which
# emits everything -- more verbose than DEBUG, and enough to switch on the DEBUG
# transcript logging TR-181 gates. See app/config.py::_validate_log_level.
@pytest.mark.parametrize("bad_level", ["verbose", "trace", "WARN", "INF0", "", "notset", "NOTSET"])
def test_unknown_log_level_is_rejected(bad_level: str) -> None:
    """TC-BE-093: a level outside the logging module's names fails validation."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(log_level=bad_level)

    assert "log_level" in str(excinfo.value)


def test_masked_dump_replaces_a_configured_secret_with_stars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-BE-094: a set groq_api_key is dumped as ``"***"``, never in the clear."""
    monkeypatch.setenv("GROQ_API_KEY", FAKE_SECRET)
    settings = Settings()

    assert settings.groq_api_key is not None
    assert settings.groq_api_key.get_secret_value() == FAKE_SECRET

    dumped = settings.masked_dump()

    assert dumped["groq_api_key"] == "***"
    assert FAKE_SECRET not in repr(dumped)
    # Non-secret fields survive the masking untouched.
    assert dumped["stt_provider"] == "groq"
    assert dumped["backend_port"] == 8000


def test_masked_dump_reports_none_when_no_secret_is_configured(settings: Settings) -> None:
    """TC-BE-100: an unset groq_api_key is dumped as ``None``, not ``"***"``."""
    assert settings.groq_api_key is None

    dumped = settings.masked_dump()

    assert "groq_api_key" in dumped
    assert dumped["groq_api_key"] is None


def test_repr_and_str_never_expose_the_raw_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """TC-BE-101: no representation of Settings can leak the key into a log."""
    monkeypatch.setenv("GROQ_API_KEY", FAKE_SECRET)
    settings = Settings()

    assert FAKE_SECRET not in repr(settings)
    assert FAKE_SECRET not in str(settings)
    assert FAKE_SECRET not in f"{settings}"
    assert FAKE_SECRET not in f"{settings!r}"
    assert FAKE_SECRET not in repr(settings.model_dump())
    assert FAKE_SECRET not in repr([settings])
    assert "***" in repr(settings)
    assert repr(settings).startswith("Settings(")


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("backend_port", 0),
        ("backend_port", 70_000),
        ("backend_port", 65_536),
        ("llm_temperature", 5.0),
        ("llm_temperature", -0.1),
        ("llm_max_tokens", 0),
        ("llm_max_tokens", 4_097),
        ("kokoro_speed", 0.0),
        ("kokoro_speed", 3.1),
        ("turn_timeout_s", 0.0),
        ("max_history_turns", 0),
        ("max_json_message_bytes", 0),
        ("max_utterance_bytes", 0),
    ],
)
def test_out_of_range_values_are_rejected(field_name: str, value: float) -> None:
    """TC-BE-102: constrained numeric fields refuse values outside their bounds."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(**{field_name: value})

    assert field_name in str(excinfo.value)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("llm_temperature", 0.0),
        ("llm_temperature", 2.0),
        ("llm_max_tokens", 1),
        ("llm_max_tokens", 4_096),
        ("backend_port", 1),
        ("backend_port", 65_535),
        ("kokoro_speed", 3.0),
    ],
)
def test_boundary_values_are_accepted(field_name: str, value: float) -> None:
    """TC-BE-099: the inclusive ends of every declared range validate."""
    settings = Settings(**{field_name: value})

    assert getattr(settings, field_name) == value


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("stt_provider", "bogus"),
        ("stt_provider", "kokoro"),
        ("llm_provider", "kokoro"),
        ("llm_provider", "local"),
        ("tts_provider", "groq"),
        ("tts_provider", "elevenlabs"),
    ],
)
def test_unknown_provider_names_are_rejected(field_name: str, value: str) -> None:
    """TC-BE-106: provider fields accept only the literals declared for them."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(**{field_name: value})

    assert field_name in str(excinfo.value)


@pytest.mark.parametrize("stt_var_name", ["STT_PROVIDER", "stt_provider"])
def test_environment_variables_override_defaults_case_insensitively(
    monkeypatch: pytest.MonkeyPatch,
    stt_var_name: str,
) -> None:
    """TC-BE-105: env vars win over defaults and match field names in any case."""
    monkeypatch.setenv(stt_var_name, "fake")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("TTS_PROVIDER", "fake")
    monkeypatch.setenv("BACKEND_PORT", "9100")
    monkeypatch.setenv("LOG_JSON", "true")

    settings = Settings()

    assert settings.stt_provider == "fake"
    assert settings.llm_provider == "ollama"
    assert settings.tts_provider == "fake"
    assert settings.backend_port == 9100
    assert settings.log_json is True


def test_get_settings_returns_the_same_cached_instance() -> None:
    """TC-BE-103: get_settings is memoised, and cache_clear starts a new one."""
    first = get_settings()
    second = get_settings()

    assert first is second
    assert get_settings.cache_info().currsize == 1

    get_settings.cache_clear()
    third = get_settings()

    assert third is not first
    assert third == first


def test_repo_root_is_the_directory_holding_docs_and_claude_md() -> None:
    """TC-BE-104: the ``parents[N]`` arithmetic in config.py resolves correctly."""
    config_file = Path(config.__file__).resolve()

    assert config_file.parent.name == "app"
    assert config_file.parent.parent == BACKEND_ROOT
    assert BACKEND_ROOT.name == "backend"
    assert BACKEND_ROOT.parent == REPO_ROOT
    assert MODELS_DIR == BACKEND_ROOT / "models"

    assert REPO_ROOT.is_absolute()
    assert (REPO_ROOT / "CLAUDE.md").is_file()
    assert (REPO_ROOT / "docs").is_dir()
    assert (REPO_ROOT / "docs" / "TRD.md").is_file()
    assert (BACKEND_ROOT / "pyproject.toml").is_file()
    # The one that is easy to get wrong: REPO_ROOT must not be `backend/`.
    assert REPO_ROOT != BACKEND_ROOT


def test_settings_read_the_isolated_env_file_not_the_repo_root_one(
    isolated_env: Path,
) -> None:
    """TC-BE-107: the suite's dotenv redirect is live for every instantiation."""
    assert Settings.model_config["env_file"] == isolated_env
    assert isolated_env != REPO_ROOT / ".env"

    # Proof the redirect is honoured rather than merely stored: a value written
    # to the temporary file after class creation still reaches a new Settings.
    isolated_env.write_text("STT_PROVIDER=fake\n", encoding="utf-8")
    settings = Settings()

    assert settings.stt_provider == "fake"
    # ...and the developer's real .env, which does hold a key, was not read.
    assert settings.groq_api_key is None
