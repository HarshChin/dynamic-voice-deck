"""Fixtures for tests that talk to the real providers.

The rest of the suite is deliberately cut off from the developer's machine: an autouse fixture in
``tests/conftest.py`` deletes every settings variable from the environment so that no unit test can
accidentally spend money or depend on a key. These tests need the opposite, so the credential is
read straight from the repository's ``.env`` rather than from the environment that fixture has
already emptied. Reading the file is also the honest thing to do: it is where the project documents
the key as living, so a developer who can run the app can run these.
"""

from __future__ import annotations

import wave
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import pytest
from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[3]
"""The directory holding ``.env``: ``backend/tests/integration`` is three levels down."""

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "audio"
"""Where the recorded utterances live."""

SAMPLE_RATE = 16_000
"""The rate the browser uploads at, and the rate the fixtures are stored at."""


def _env() -> dict[str, str | None]:
    """Read the repository's ``.env``.

    Returns:
        Its contents, empty when the file is absent.
    """
    env_file = REPO_ROOT / ".env"
    return dict(dotenv_values(env_file)) if env_file.is_file() else {}


@pytest.fixture(scope="session")
def groq_api_key() -> str:
    """The real Groq credential, or a skip.

    Returns:
        The key.
    """
    key = _env().get("GROQ_API_KEY")
    if not key:
        pytest.skip("no GROQ_API_KEY in .env; integration tests need a real credential")
    return key


@pytest.fixture(scope="session")
def groq_llm_model() -> str:
    """The model the project is configured to use, so a run reports on what ships.

    Returns:
        The model identifier.
    """
    return _env().get("GROQ_LLM_MODEL") or "qwen/qwen3.8-27b"


@pytest.fixture(scope="session")
def utterance() -> Callable[[str], bytes]:
    """Read a recorded utterance down to the samples the provider expects.

    A fixture rather than a plain function because ``tests/`` is not a package, so a test module
    cannot import from a sibling conftest.

    Returns:
        A reader taking a file name under ``tests/fixtures/audio`` and returning little-endian
        16-bit mono samples, skipping the test if the fixture is absent.
    """

    def read(name: str) -> bytes:
        path = FIXTURES / name
        if not path.is_file():
            pytest.skip(f"missing {path}; run scripts/make_audio_fixtures.py")
        with wave.open(BytesIO(path.read_bytes()), "rb") as handle:
            assert handle.getnchannels() == 1, "fixtures are mono, as the browser uploads"
            assert handle.getsampwidth() == 2, "fixtures are 16-bit"
            assert handle.getframerate() == SAMPLE_RATE, "fixtures are 16 kHz"
            return handle.readframes(handle.getnframes())

    return read
