"""Tests for :mod:`app.logging_setup` — renderers, the stdlib bridge, levels.

Output is captured by pointing the *installed* handler's stream at an
:class:`io.StringIO`, found on the root logger by :data:`~app.logging_setup.HANDLER_NAME`.
That is deliberately not ``caplog``: pytest's ``caplog`` attaches its own handler
and reports the unformatted record, so it would happily pass while the JSON
renderer was broken. Writing through the real handler exercises the real
:class:`structlog.stdlib.ProcessorFormatter`, which is the thing under test.

The module-level autouse fixture snapshots and restores the root logger, every
bridged logger, and structlog's own configuration. Without it these tests would
leave a handler and a level behind and quietly corrupt every test that ran
afterwards.
"""

from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
import structlog
from app.config import Settings
from app.logging_setup import BRIDGED_LOGGERS, HANDLER_NAME, configure_logging, get_logger

LOGGER_NAME = "tests.logging_setup"
"""Logger name used by the assertions; distinctive enough to spot in a line."""

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
"""Matches the SGR colour codes ``ConsoleRenderer(colors=True)`` emits."""

RENDERED_KEYS = frozenset({"event", "level", "logger", "timestamp"})
"""Keys the shared processor chain must put on every rendered record."""


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Undo everything ``configure_logging`` mutates, for every test in this module.

    ``configure_logging`` reaches into process-wide state: it adds a handler to
    the root logger, sets its level, empties the handler list of each bridged
    logger, and replaces structlog's global configuration. All of it is
    snapshotted here and restored afterwards, so a failure inside a test cannot
    leak a level or a handler into an unrelated test.

    Yields:
        ``None``, with the snapshot taken.
    """
    root = logging.getLogger()
    saved_root_handlers = list(root.handlers)
    saved_root_level = root.level
    saved_bridged = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
        )
        for name in BRIDGED_LOGGERS
    }

    try:
        yield
    finally:
        root.handlers[:] = saved_root_handlers
        root.setLevel(saved_root_level)
        for name, (handlers, level, propagate) in saved_bridged.items():
            bridged = logging.getLogger(name)
            bridged.handlers[:] = handlers
            bridged.setLevel(level)
            bridged.propagate = propagate
        structlog.contextvars.clear_contextvars()
        structlog.reset_defaults()


def _installed_handler() -> logging.StreamHandler:  # type: ignore[type-arg]
    """Return the one root handler named :data:`~app.logging_setup.HANDLER_NAME`.

    Returns:
        The single installed handler; the assertion doubles as the idempotency
        check, since a second copy would mean duplicated output.
    """
    named = [h for h in logging.getLogger().handlers if h.get_name() == HANDLER_NAME]

    assert len(named) == 1, f"expected exactly one {HANDLER_NAME!r} handler, got {len(named)}"
    handler = named[0]
    assert isinstance(handler, logging.StreamHandler)

    return handler


def _capture() -> io.StringIO:
    """Redirect the installed handler away from stdout and into memory.

    Returns:
        The buffer the handler now writes to, formatted exactly as it would be
        on a terminal.
    """
    stream = io.StringIO()
    _installed_handler().setStream(stream)
    return stream


def _configure_and_capture(settings: Settings) -> io.StringIO:
    """Configure logging from ``settings`` and capture the installed handler's output.

    Args:
        settings: Settings whose ``log_level`` and ``log_json`` drive the setup.

    Returns:
        The buffer receiving every rendered line from now on.
    """
    configure_logging(settings)
    return _capture()


def _lines(stream: io.StringIO) -> list[str]:
    """Return the rendered lines written so far.

    Args:
        stream: A buffer returned by :func:`_capture`.

    Returns:
        One entry per emitted record, with the trailing newlines removed.
    """
    return stream.getvalue().splitlines()


def test_json_mode_emits_one_parsable_object_per_call() -> None:
    """TC-BE-110: log_json=True renders one JSON line with event, level, logger, ISO time."""
    stream = _configure_and_capture(Settings(log_json=True))

    get_logger(LOGGER_NAME).info("pipeline.started", turn_id=3)

    lines = _lines(stream)
    assert len(lines) == 1

    record = json.loads(lines[0])

    assert set(record) >= RENDERED_KEYS
    assert record["event"] == "pipeline.started"
    assert record["level"] == "info"
    assert record["logger"] == LOGGER_NAME
    assert record["turn_id"] == 3

    stamped = datetime.fromisoformat(record["timestamp"])
    assert stamped.tzinfo is not None
    assert stamped.utcoffset() == timedelta(0)


def test_console_mode_renders_a_human_line_that_is_not_json() -> None:
    """TC-BE-111: log_json=False renders a coloured line that json.loads rejects."""
    stream = _configure_and_capture(Settings(log_json=False))

    get_logger(LOGGER_NAME).info("pipeline.started", turn_id=3)

    lines = _lines(stream)
    assert len(lines) == 1

    with pytest.raises(json.JSONDecodeError):
        json.loads(lines[0])

    plain = ANSI_ESCAPE.sub("", lines[0])
    # Colours were requested, so stripping them must actually change the line.
    assert plain != lines[0]
    assert "pipeline.started" in plain
    assert "info" in plain
    assert f"[{LOGGER_NAME}]" in plain
    assert "turn_id" in plain


def test_uvicorn_records_are_rendered_by_the_same_handler_as_structlog() -> None:
    """TC-BE-112: a stdlib uvicorn.access record reaches our handler and renderer."""
    stream = _configure_and_capture(Settings(log_json=True))
    access = logging.getLogger("uvicorn.access")

    assert access.handlers == []
    assert access.propagate is True

    get_logger(LOGGER_NAME).info("native.event")
    access.info("GET %s 200", "/api/health")

    # Both lines landing in this buffer is the proof: it belongs to the single
    # root handler, so the stdlib record was formatted by the same instance.
    lines = _lines(stream)
    assert len(lines) == 2
    assert isinstance(_installed_handler().formatter, structlog.stdlib.ProcessorFormatter)

    native, foreign = (json.loads(line) for line in lines)

    # Subset rather than equality: pytest's own capture handler formats the
    # record before ours runs, which sets ``record.message``, and ExtraAdder
    # faithfully copies that onto the foreign line. Outside pytest it is absent.
    assert set(native) >= RENDERED_KEYS
    assert set(foreign) >= RENDERED_KEYS
    assert native["logger"] == LOGGER_NAME
    assert foreign["logger"] == "uvicorn.access"
    assert native["level"] == foreign["level"] == "info"
    assert native["event"] == "native.event"
    # The %s placeholder is interpolated by PositionalArgumentsFormatter.
    assert foreign["event"] == "GET /api/health 200"


def test_configuring_twice_leaves_exactly_one_named_handler() -> None:
    """TC-BE-113: repeated configuration replaces the handler instead of stacking one."""
    configure_logging(Settings(log_json=True))
    first = _installed_handler()

    configure_logging(Settings(log_json=True))
    second = _installed_handler()

    assert second is not first
    root_handlers = logging.getLogger().handlers
    assert [h.get_name() for h in root_handlers].count(HANDLER_NAME) == 1
    assert first not in root_handlers

    stream = _capture()
    get_logger(LOGGER_NAME).info("once.only")

    # One handler means one line; a stacked duplicate would render it twice.
    assert len(_lines(stream)) == 1


def test_reconfiguring_from_console_to_json_changes_an_existing_logger() -> None:
    """TC-BE-114: cache_logger_on_first_use=False lets a live logger switch renderer."""
    console_stream = _configure_and_capture(Settings(log_json=False))
    logger = get_logger(LOGGER_NAME)

    logger.info("switch.me")

    console_line = _lines(console_stream)[0]
    with pytest.raises(json.JSONDecodeError):
        json.loads(console_line)

    json_stream = _configure_and_capture(Settings(log_json=True))
    # Same logger object as before: a cached processor chain would keep
    # rendering console lines here.
    logger.info("switch.me")

    record = json.loads(_lines(json_stream)[0])

    assert record["event"] == "switch.me"
    assert record["logger"] == LOGGER_NAME


@pytest.mark.parametrize("name", BRIDGED_LOGGERS)
def test_bridged_logger_ends_with_no_handlers_and_propagating(name: str) -> None:
    """TC-BE-115: each bridged stdlib logger is emptied and left propagating to root."""
    bridged = logging.getLogger(name)
    # Seed the state configure_logging has to undo, so an already-clean logger
    # cannot make this pass by accident.
    bridged.addHandler(logging.NullHandler())
    bridged.propagate = False

    configure_logging(Settings())

    assert bridged.handlers == []
    assert bridged.propagate is True


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("DEBUG", logging.DEBUG),
        ("INFO", logging.INFO),
        ("WARNING", logging.WARNING),
        ("ERROR", logging.ERROR),
        ("CRITICAL", logging.CRITICAL),
    ],
)
def test_configured_level_is_applied_to_root_and_bridged_loggers(
    configured: str,
    expected: int,
) -> None:
    """TC-BE-116: log_level becomes the numeric level of the root and bridged loggers."""
    configure_logging(Settings(log_level=configured))

    assert logging.getLogger().level == expected
    for name in BRIDGED_LOGGERS:
        assert logging.getLogger(name).level == expected


@pytest.mark.parametrize(
    ("configured", "below", "at_or_above"),
    [
        ("INFO", "debug", "info"),
        ("WARNING", "info", "warning"),
        ("ERROR", "warning", "error"),
        ("CRITICAL", "error", "critical"),
    ],
)
def test_records_below_the_threshold_are_suppressed(
    configured: str,
    below: str,
    at_or_above: str,
) -> None:
    """TC-BE-117: a call under the configured level emits nothing; one at it emits."""
    stream = _configure_and_capture(Settings(log_level=configured, log_json=True))
    logger = get_logger(LOGGER_NAME)

    getattr(logger, below)("too.quiet")

    assert _lines(stream) == []

    getattr(logger, at_or_above)("loud.enough")

    assert [json.loads(line)["event"] for line in _lines(stream)] == ["loud.enough"]


def test_get_logger_supports_bound_context_that_reaches_the_output() -> None:
    """TC-BE-118: keys bound onto a logger appear in each line it later renders."""
    stream = _configure_and_capture(Settings(log_json=True))
    logger = get_logger(LOGGER_NAME)
    bound = logger.bind(session_id="s-42")

    bound.info("turn.started", turn_id=7)
    bound.info("turn.finished")
    logger.info("unbound.event")

    first, second, third = (json.loads(line) for line in _lines(stream))

    assert first["session_id"] == "s-42"
    assert first["turn_id"] == 7
    assert second["session_id"] == "s-42"
    # Binding returns a new logger; the one it was bound from stays clean.
    assert "session_id" not in third


def test_context_variables_are_merged_into_native_and_bridged_records() -> None:
    """TC-BE-119: session context bound via contextvars reaches both record kinds."""
    stream = _configure_and_capture(Settings(log_json=True))
    structlog.contextvars.bind_contextvars(session_id="s-7", turn_id=2)

    get_logger(LOGGER_NAME).info("native.event")
    logging.getLogger("uvicorn.error").warning("foreign.event")

    native, foreign = (json.loads(line) for line in _lines(stream))

    assert native["session_id"] == "s-7"
    assert native["turn_id"] == 2
    assert foreign["session_id"] == "s-7"
    assert foreign["turn_id"] == 2
