"""Tests for :mod:`app.errors` — the exception hierarchy and error payloads.

The hierarchy is load-bearing rather than decorative. ``app.main.create_app``
registers ``app_error_handler`` for :class:`~app.errors.AppError` alone, and
Starlette resolves an exception handler by walking the raised class's MRO, so
an application error that is *not* a subclass of ``AppError`` would bypass the
handler entirely and surface as an unhandled 500 with a raw traceback. These
tests pin that relationship, the attributes each error carries, and the fact
that ``raise ... from`` keeps the original failure attached for the logs.
"""

from __future__ import annotations

import pytest
from app import errors
from app.errors import AppError, ConfigError, DeckError, ProtocolError, ProviderError

APP_ERROR_CLASSES: tuple[type[AppError], ...] = (
    ConfigError,
    DeckError,
    ProtocolError,
    ProviderError,
)
"""Every concrete application error. TC-BE-121 fails if ``app.errors`` grows one
that is not listed here, which keeps the parametrised cases below exhaustive."""

ERROR_CONSTRUCTIONS: list[tuple[type[AppError], tuple[str, ...]]] = [
    (ConfigError, ("configuration is missing",)),
    (DeckError, ("deck failed validation",)),
    (ProtocolError, ("bad_message", "frame was not JSON")),
    (ProviderError, ("groq_llm", "upstream refused the request")),
]
"""Each concrete error paired with positional arguments that build one."""

ERROR_IDS: list[str] = [error_class.__name__ for error_class, _ in ERROR_CONSTRUCTIONS]


@pytest.mark.parametrize("error_class", APP_ERROR_CLASSES, ids=ERROR_IDS)
def test_every_application_error_descends_from_app_error(error_class: type[AppError]) -> None:
    """TC-BE-120: each concrete error subclasses AppError, which subclasses Exception."""
    assert issubclass(error_class, AppError)
    assert issubclass(AppError, Exception)
    assert issubclass(error_class, Exception)
    # A distinct class, not an alias: the handler must still be able to name it.
    assert error_class is not AppError
    assert AppError in error_class.__mro__


def test_the_hierarchy_is_flat_and_the_catalogue_above_is_exhaustive() -> None:
    """TC-BE-121: siblings are unrelated, and app.errors declares no error outside the set."""
    for error_class in APP_ERROR_CLASSES:
        siblings = [other for other in APP_ERROR_CLASSES if other is not error_class]
        for sibling in siblings:
            # `except ProviderError` must never swallow a DeckError, and vice versa.
            assert not issubclass(error_class, sibling)

    declared = {
        obj for obj in vars(errors).values() if isinstance(obj, type) and issubclass(obj, AppError)
    }

    assert declared == {AppError, *APP_ERROR_CLASSES}


def test_provider_error_carries_its_fields_and_prefixes_the_provider_name() -> None:
    """TC-BE-122: ProviderError keeps provider/message/retryable/retry_after; str() is prefixed."""
    exc = ProviderError("groq_llm", "429 rate limited", retryable=True, retry_after=1.5)

    assert exc.provider == "groq_llm"
    assert exc.message == "429 rate limited"
    assert exc.retryable is True
    assert exc.retry_after == 1.5

    assert str(exc) == "groq_llm: 429 rate limited"
    assert str(exc).startswith("groq_llm")
    # `message` stays unprefixed so a caller can re-render it without stripping.
    assert exc.message in str(exc)
    assert exc.args == ("groq_llm: 429 rate limited",)


def test_provider_error_defaults_to_not_retryable_with_no_retry_after() -> None:
    """TC-BE-123: omitting the optional flags gives retryable False and retry_after None."""
    exc = ProviderError("kokoro_tts", "synthesis failed")

    assert exc.retryable is False
    assert exc.retry_after is None
    assert str(exc) == "kokoro_tts: synthesis failed"

    # Both flags are keyword-only, so a positional third argument is a TypeError
    # rather than a silently mis-assigned `retryable`.
    with pytest.raises(TypeError):
        ProviderError("kokoro_tts", "synthesis failed", True)


def test_protocol_error_carries_its_code_and_stringifies_to_the_message() -> None:
    """TC-BE-124: ProtocolError keeps code and message, and str() is exactly the message."""
    exc = ProtocolError("bad_message", "utterance.end arrived before utterance.start")

    assert exc.code == "bad_message"
    assert exc.message == "utterance.end arrived before utterance.start"
    assert str(exc) == "utterance.end arrived before utterance.start"
    # The code is deliberately not part of str(): it travels in the protocol
    # frame's own field, not inside the human-readable text.
    assert "bad_message" not in str(exc)
    assert exc.args == ("utterance.end arrived before utterance.start",)


@pytest.mark.parametrize(("error_class", "args"), ERROR_CONSTRUCTIONS, ids=ERROR_IDS)
def test_every_error_is_raisable_and_caught_as_app_error(
    error_class: type[AppError],
    args: tuple[str, ...],
) -> None:
    """TC-BE-125: raising any application error is caught by ``except AppError``."""
    with pytest.raises(AppError) as excinfo:
        raise error_class(*args)

    assert type(excinfo.value) is error_class
    assert isinstance(excinfo.value, AppError)
    assert str(excinfo.value) != ""

    # The narrower `except <subclass>` still works, which is what pipeline code
    # uses when it wants to react to one provider failure only.
    with pytest.raises(error_class):
        raise error_class(*args)


@pytest.mark.parametrize(("error_class", "args"), ERROR_CONSTRUCTIONS, ids=ERROR_IDS)
def test_raise_from_preserves_the_original_exception_as_cause(
    error_class: type[AppError],
    args: tuple[str, ...],
) -> None:
    """TC-BE-126: wrapping a third-party failure keeps it reachable via __cause__."""
    original = ValueError("the vendor SDK exploded")

    with pytest.raises(AppError) as excinfo:
        try:
            raise original
        except ValueError as exc:
            raise error_class(*args) from exc

    assert excinfo.value.__cause__ is original
    assert isinstance(excinfo.value.__cause__, ValueError)
    # `from` also suppresses the implicit chaining note in the traceback.
    assert excinfo.value.__suppress_context__ is True
