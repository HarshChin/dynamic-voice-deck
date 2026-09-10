"""Tests for application startup: the lifespan, settings loading, and error handling.

The shared ``client`` fixture in ``conftest.py`` deliberately skips the lifespan,
so nothing else in the suite exercises it. That leaves the riskiest part of the
process unverified, including the one line that logs the settings — the place a
credential would leak if ``masked_dump()`` were ever dropped (TRD TR-180).

Running the lifespan is cheap and offline in Phase 0: it loads settings,
configures logging, and stores an :class:`~app.main.AppState`. It does mutate
process-wide logging state, so ``_restore_logging`` below snapshots and undoes
exactly what :func:`~app.logging_setup.configure_logging` changes — the root
logger's own handler and level, the bridged loggers, and structlog's
configuration — or every test that ran afterwards would inherit them.
"""

from __future__ import annotations

import io
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import structlog
from app import __version__
from app.config import Settings, get_settings
from app.errors import AppError, ConfigError, ProviderError
from app.logging_setup import BRIDGED_LOGGERS, HANDLER_NAME
from app.main import AppState, _load_settings, create_app
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pydantic_settings import SettingsError

HEALTH_PATH = "/api/health"
BOOM_PATH = "/api/boom"

FAKE_SECRET = "sk-STARTUP-MUST-NEVER-LOG-THIS"  # noqa: S105 - stand-in, not a real credential
"""Distinctive stand-in for a real key, chosen so a leak is unambiguous in output."""


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Undo every global logging change the lifespan makes.

    ``configure_logging`` installs a named handler on the root logger, sets the
    root level, and rewrites the handlers, propagation, and level of each
    bridged logger; ``structlog.configure`` replaces the process-wide processor
    chain. All of it survives the test that caused it, so it is snapshotted here
    and put back afterwards.

    Only handlers named :data:`~app.logging_setup.HANDLER_NAME` are touched on
    the root logger, leaving pytest's own capture handler — which pytest adds
    and removes around each phase — strictly alone.

    Yields:
        ``None``, with the logging state recorded.
    """
    root = logging.getLogger()
    saved_root_level = root.level
    saved_root_handlers = [h for h in root.handlers if h.get_name() == HANDLER_NAME]
    saved_bridged = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
            logging.getLogger(name).level,
        )
        for name in BRIDGED_LOGGERS
    }
    saved_structlog = structlog.get_config() if structlog.is_configured() else None

    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler.get_name() == HANDLER_NAME:
                root.removeHandler(handler)
                handler.close()
        for handler in saved_root_handlers:
            root.addHandler(handler)
        root.setLevel(saved_root_level)

        for name, (handlers, propagate, level) in saved_bridged.items():
            bridged = logging.getLogger(name)
            bridged.handlers[:] = handlers
            bridged.propagate = propagate
            bridged.setLevel(level)

        if saved_structlog is None:
            structlog.reset_defaults()
        else:
            structlog.configure(**saved_structlog)


@contextmanager
def _captured_stdout() -> Iterator[io.StringIO]:
    """Redirect ``sys.stdout`` to an in-memory buffer for the duration of the block.

    ``configure_logging`` reads ``sys.stdout`` when it builds its handler, so the
    redirect must be in place *before* the lifespan runs. Restoring on exit keeps
    assertion output visible when a test fails.

    Yields:
        The buffer receiving everything written to ``sys.stdout``.
    """
    stream = io.StringIO()
    original = sys.stdout
    sys.stdout = stream
    try:
        yield stream
    finally:
        sys.stdout = original


def test_lifespan_stores_an_app_state_holding_the_settings(
    isolated_env: Path, fake_providers: None
) -> None:
    """TC-BE-130: entering the client as a context manager runs startup and builds AppState."""
    app = create_app()

    # Nothing is attached until the lifespan runs; this is what makes the
    # conftest `client` fixture's fallback path necessary.
    assert getattr(app.state, "app_state", None) is None

    with TestClient(app):
        state = app.state.app_state

        assert isinstance(state, AppState)
        assert isinstance(state.settings, Settings)
        # The lifespan reuses the singleton rather than building a second copy.
        assert state.settings is get_settings()
        assert state.tts_warm is True  # the fake warms instantly

    # Shutdown leaves the state in place rather than tearing it down.
    assert app.state.app_state is state


def test_health_answers_from_the_lifespan_state_inside_the_context(
    isolated_env: Path, fake_providers: None
) -> None:
    """TC-BE-131: with startup complete, the probe answers from the state the lifespan built."""
    app = create_app()

    with TestClient(app) as test_client:
        state = app.state.app_state
        response = test_client.get(HEALTH_PATH)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        # The fixture selects the fakes so startup needs no credential; the
        # assertion that matters is that the probe reports what was configured.
        "providers": {"stt": "fake", "llm": "fake", "tts": "fake"},
        "tts_warm": True,
    }
    # `get_app_state` found the lifespan's state and did not substitute a default.
    assert app.state.app_state is state


@pytest.mark.parametrize("log_json", [False, True], ids=["console", "json"])
def test_startup_logs_the_settings_with_the_secret_masked(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: Path,
    fake_providers: None,
    log_json: bool,
) -> None:
    """TC-BE-132: TR-180 — the startup line carries ``"***"`` and never the raw key."""
    monkeypatch.setenv("GROQ_API_KEY", FAKE_SECRET)
    monkeypatch.setenv("LOG_JSON", "true" if log_json else "false")
    get_settings.cache_clear()

    with _captured_stdout() as stream, TestClient(create_app()):
        pass
    output = stream.getvalue()

    settings = get_settings()
    # Guard against a vacuous pass: the secret really was loaded and logged past.
    assert settings.groq_api_key is not None
    assert settings.groq_api_key.get_secret_value() == FAKE_SECRET

    assert "app.startup" in output
    assert "groq_api_key" in output
    assert "***" in output
    assert FAKE_SECRET not in output
    # Also catch a truncated or re-encoded leak of the same value.
    assert "sk-STARTUP" not in output

    # `masked_dump()` is what TR-180 mandates, and these two pin it rather than
    # letting a weaker substitute pass by luck. `model_dump()` renders the field
    # as `SecretStr('**********')` and `model_dump(mode="json")` as
    # `'**********'` -- both of which happen to satisfy `"***" in output` while
    # bypassing the masking the requirement asks for. pydantic's placeholder is
    # ten asterisks; ours is exactly three.
    assert "**********" not in output
    assert "SecretStr" not in output

    # A non-secret setting is logged in the clear, proving the whole dump is not
    # simply being suppressed.
    assert "whisper-large-v3-turbo" in output


def test_load_settings_wraps_a_validation_error_in_config_error(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: Path,
) -> None:
    """TC-BE-133: an invalid field value becomes ConfigError with the original as __cause__."""
    monkeypatch.setenv("LOG_LEVEL", "verbose")
    get_settings.cache_clear()

    with pytest.raises(ConfigError) as excinfo:
        _load_settings()

    assert isinstance(excinfo.value, AppError)
    assert isinstance(excinfo.value.__cause__, ValidationError)
    assert "log_level" in str(excinfo.value)
    assert ".env.example" in str(excinfo.value)

    # The whole point of the wrapper: building the app fails fast and loudly
    # rather than serving traffic it cannot handle.
    get_settings.cache_clear()
    with pytest.raises(ConfigError):
        create_app()


def test_load_settings_wraps_a_settings_source_error_in_config_error(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: Path,
) -> None:
    """TC-BE-134: a comma-separated CORS_ORIGINS raises SettingsError and is wrapped too."""
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    get_settings.cache_clear()

    with pytest.raises(ConfigError) as excinfo:
        _load_settings()

    cause = excinfo.value.__cause__

    assert isinstance(cause, SettingsError)
    # The regression this pins: pydantic-settings JSON-decodes complex-typed
    # fields inside the *source*, before any validator runs, and raises
    # SettingsError -- a plain ValueError, NOT a ValidationError. A wrapper that
    # caught only ValidationError let this escape as a raw startup traceback.
    assert not isinstance(cause, ValidationError)
    assert "cors_origins" in str(cause)
    assert ".env.example" in str(excinfo.value)

    # Control: the documented JSON form of the same setting still loads.
    monkeypatch.setenv("CORS_ORIGINS", '["http://example.test:5173"]')
    get_settings.cache_clear()

    assert _load_settings().cors_origins == ["http://example.test:5173"]


@pytest.mark.parametrize(
    "blank",
    ["", " ", "   ", "\t", "\n", " \t\n "],
    ids=["empty", "space", "spaces", "tab", "newline", "mixed-whitespace"],
)
def test_a_blank_groq_api_key_normalises_to_none(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: Path,
    blank: str,
) -> None:
    """TC-BE-135: a blank GROQ_API_KEY is absent, not a set-but-empty SecretStr."""
    monkeypatch.setenv("GROQ_API_KEY", blank)

    settings = Settings()

    assert settings.groq_api_key is None
    assert settings.masked_dump()["groq_api_key"] is None

    # Control: a real value is left untouched, so the normalisation is not
    # simply discarding every credential.
    monkeypatch.setenv("GROQ_API_KEY", FAKE_SECRET)
    configured = Settings()

    assert configured.groq_api_key is not None
    assert configured.groq_api_key.get_secret_value() == FAKE_SECRET


@pytest.mark.parametrize(
    "key_line",
    ["GROQ_API_KEY=", 'GROQ_API_KEY=""', "GROQ_API_KEY=   "],
    ids=["bare", "empty-quotes", "whitespace"],
)
def test_a_copied_env_example_leaves_no_secret_and_still_boots(
    isolated_env: Path,
    key_line: str,
) -> None:
    """TC-BE-136: the blank key line ``cp .env.example .env`` leaves loads as None."""
    # All three providers are named because startup builds every one (TR-080);
    # the point of this case is the blank key, not the provider selection.
    isolated_env.write_text(
        f"{key_line}\nSTT_PROVIDER=fake\nLLM_PROVIDER=fake\nTTS_PROVIDER=fake\n",
        encoding="utf-8",
    )
    get_settings.cache_clear()

    settings = Settings()

    assert settings.groq_api_key is None
    # Proof the dotenv file was actually read, so the assertion above is not
    # just observing an absent variable.
    assert settings.stt_provider == "fake"

    get_settings.cache_clear()
    app = create_app()

    with TestClient(app) as test_client:
        payload = test_client.get(HEALTH_PATH).json()

    assert payload["status"] == "ok"
    assert app.state.app_state.settings.groq_api_key is None


def test_an_app_error_becomes_a_500_json_body_naming_the_error(isolated_env: Path) -> None:
    """TC-BE-137: the AppError handler answers 500 with the class name and message."""
    app = create_app()

    # Registered for AppError only, so it fires for subclasses via the MRO and
    # never for arbitrary exceptions.
    assert AppError in app.exception_handlers

    @app.get(BOOM_PATH)
    async def boom() -> None:
        """Raise a provider failure so the registered handler renders it."""
        raise ProviderError("groq_llm", "upstream refused", retryable=True)

    test_client = TestClient(app)
    try:
        response = test_client.get(BOOM_PATH)
    finally:
        test_client.close()

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "error": "ProviderError",
        "message": "groq_llm: upstream refused",
    }
