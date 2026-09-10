"""Tests for ``GET /api/health`` (TRD section 4.10).

The client fixture does not run the application lifespan, so these assertions
also pin the endpoint's behaviour on a cold process: it must answer without
providers built and without a TTS warm-up.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app import __version__
from app.config import get_settings
from app.main import create_app
from fastapi.testclient import TestClient

HEALTH_PATH = "/api/health"
EXPECTED_KEYS = {"status", "version", "providers", "tts_warm"}
EXPECTED_PROVIDER_KEYS = {"stt", "llm", "tts"}


def test_health_returns_ok_with_the_documented_key_set(client: TestClient) -> None:
    """TC-BE-095: 200 with exactly the four documented keys and a real version."""
    response = client.get(HEALTH_PATH)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")

    payload = response.json()

    assert set(payload) == EXPECTED_KEYS
    assert payload["status"] == "ok"
    assert isinstance(payload["version"], str)
    assert payload["version"] != ""
    assert payload["version"] == __version__


def test_health_reports_the_configured_providers(client: TestClient) -> None:
    """TC-BE-096: the providers block mirrors the settings, with no extra keys."""
    payload = client.get(HEALTH_PATH).json()
    providers = payload["providers"]
    settings = get_settings()

    assert set(providers) == EXPECTED_PROVIDER_KEYS
    assert providers == {
        "stt": settings.stt_provider,
        "llm": settings.llm_provider,
        "tts": settings.tts_provider,
    }
    # With the isolated environment those are the declared defaults.
    assert providers == {"stt": "groq", "llm": "groq", "tts": "kokoro"}


def test_health_reports_tts_warm_as_false_before_warm_up(client: TestClient) -> None:
    """TC-BE-097: tts_warm is a boolean and is false until a warm-up has run."""
    payload = client.get(HEALTH_PATH).json()

    assert isinstance(payload["tts_warm"], bool)
    assert payload["tts_warm"] is False


def test_health_reflects_overridden_provider_configuration(
    monkeypatch: pytest.MonkeyPatch,
    isolated_env: Path,
) -> None:
    """TC-BE-098: reconfiguring the providers changes what the probe reports.

    Built without the ``client`` fixture so the environment is rewritten *before*
    the application reads it; ``isolated_env`` is still requested to keep the
    developer's ``.env`` out of the picture.
    """
    monkeypatch.setenv("STT_PROVIDER", "fake")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("TTS_PROVIDER", "fake")
    get_settings.cache_clear()

    test_client = TestClient(create_app())
    try:
        payload = test_client.get(HEALTH_PATH).json()
    finally:
        test_client.close()

    assert payload["status"] == "ok"
    assert payload["providers"] == {"stt": "fake", "llm": "ollama", "tts": "fake"}
