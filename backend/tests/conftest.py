"""Shared fixtures for the backend test suite.

Configuration tests are only honest if they are hermetic, and two things work
against that here:

* :class:`app.config.Settings` declares ``env_file=REPO_ROOT / ".env"``, so a
  plain ``Settings()`` reads the developer's real, git-ignored ``.env`` — which
  holds a live ``GROQ_API_KEY``. A test asserting "the secret is unset" would
  pass on a fresh clone and fail on the author's machine, or worse, quietly
  assert against a real key.
* :func:`app.config.get_settings` is wrapped in :func:`functools.lru_cache`, so
  the first call in a process freezes the answer and every later
  ``monkeypatch.setenv`` silently has no effect. Importing ``app.main`` already
  triggers one such call, because that module builds a module-level ``app``.

The autouse fixtures below neutralise both. ``_clear_settings_cache`` empties
the cache before and after every test. ``isolated_env`` removes every
environment variable named after a ``Settings`` field and repoints
``Settings.model_config["env_file"]`` at an empty file inside ``tmp_path``.

Repointing that single key is enough because pydantic-settings resolves
``env_file`` from ``self.model_config`` on *each* instantiation — inside
``BaseSettings._settings_build_values``, not at class-creation time — so the
redirect reaches every later ``Settings()`` call, including those made deep
inside ``create_app()``. ``monkeypatch.setitem`` restores the original value
during teardown, leaving the class untouched for other suites.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.config import Settings, get_settings
from app.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Empty the ``get_settings`` LRU cache before and after each test.

    Clearing *before* discards whatever was cached at import time (``app.main``
    builds a module-level application, which loads settings). Clearing *after*
    stops a test's monkeypatched environment from leaking into the next one.

    Yields:
        ``None``, once the cache is empty.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def isolated_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _clear_settings_cache: None,
) -> Path:
    """Cut every ``Settings`` instantiation off from the developer's machine.

    Two independent leaks are closed: inherited environment variables (a shell
    or CI runner may export ``GROQ_API_KEY`` or ``LOG_LEVEL``) and the
    repository-root ``.env`` baked into ``Settings.model_config``. Field names
    are read from ``Settings.model_fields`` so a new setting is covered the day
    it is added; both cases are deleted because ``case_sensitive=False`` lets
    pydantic-settings match either spelling.

    Args:
        monkeypatch: Pytest patcher; every change is undone at teardown.
        tmp_path: Per-test temporary directory holding the stand-in ``.env``.
        _clear_settings_cache: Ordering dependency — the cache must be empty
            before the environment is rewritten, otherwise a cached
            ``Settings`` built from the real ``.env`` survives into the test.

    Returns:
        Path to the empty temporary ``.env``. Tests may write to it to exercise
        dotenv parsing itself; anything written takes effect on the next
        ``Settings()`` call.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    # `case_sensitive=False` means pydantic-settings matches a field against ANY
    # casing of its name, so deleting just the lower and upper spellings would
    # leave e.g. `Groq_Api_Key` in place and silently defeat the isolation. Scan
    # the real environment and delete every variable that case-folds to a field.
    field_names = {name.casefold() for name in Settings.model_fields}
    for var in list(os.environ):
        if var.casefold() in field_names:
            monkeypatch.delenv(var, raising=False)

    monkeypatch.setitem(Settings.model_config, "env_file", env_file)
    return env_file


@pytest.fixture
def fake_providers(monkeypatch: pytest.MonkeyPatch, isolated_env: Path) -> None:
    """Select provider implementations that need no credential or network.

    Any test that runs the application lifespan needs this. Startup builds the
    providers (TR-080), and the configured defaults name real services, so an
    otherwise-isolated test would fail asking for ``GROQ_API_KEY``. Selecting
    the fakes keeps the test hermetic while still exercising the real
    construction path.

    Args:
        monkeypatch: Pytest patcher; every change is undone at teardown.
        isolated_env: Ordering dependency, so the scrub runs before this.
    """
    monkeypatch.setenv("STT_PROVIDER", "fake")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("TTS_PROVIDER", "fake")


@pytest.fixture
def settings(isolated_env: Path) -> Settings:
    """Return a freshly built ``Settings`` for the isolated environment.

    Args:
        isolated_env: Ordering dependency on the isolation fixture, so the
            instance is built after the environment has been scrubbed.

    Returns:
        A new :class:`~app.config.Settings`; not the ``get_settings`` singleton,
        so a test may build others alongside it.
    """
    return Settings()


@pytest.fixture
def client(isolated_env: Path, fake_providers: None) -> Iterator[TestClient]:
    """Return a test client for a freshly built application.

    The client is deliberately *not* entered as a context manager, so the
    lifespan does not run: startup work (provider construction, the Kokoro
    warm-up synthesis and its model download) must never happen in a unit test.
    Routes that need application state fall back to a default state built from
    the settings singleton.

    Args:
        isolated_env: Ordering dependency on the isolation fixture, so the
            application reads the scrubbed environment.
        fake_providers: Ordering dependency, so provider construction during a
            lifespan run needs no credential.

    Yields:
        A client bound to a new application instance.
    """
    test_client = TestClient(create_app())
    yield test_client
    test_client.close()
