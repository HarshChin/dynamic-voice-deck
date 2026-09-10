"""Structured logging configuration for the backend.

The application logs through :mod:`structlog`. Records emitted by the standard
library -- most importantly uvicorn's -- are bridged into the same processor
chain so that one renderer formats every line: coloured console output in
development and JSON in production (TRD TR-190).
"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.typing import Processor

from .config import Settings

HANDLER_NAME = "dynamic_voice_deck"
"""Name of the root handler installed by :func:`configure_logging`.

The name makes the handler identifiable so that repeated configuration replaces
it instead of stacking a second copy.
"""

BRIDGED_LOGGERS: tuple[str, ...] = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "fastapi",
    "httpx",
)
"""Standard-library loggers whose own handlers are dropped in favour of ours."""

_SHARED_PROCESSORS: tuple[Processor, ...] = (
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.PositionalArgumentsFormatter(),
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.UnicodeDecoder(),
)
"""Processors applied to both structlog-native and standard-library records."""


def _build_renderer(log_json: bool) -> list[Processor]:
    """Build the tail of the processor chain that renders a record.

    Args:
        log_json: Whether to render machine-readable JSON instead of a coloured
            console line.

    Returns:
        The processors to run after :data:`_SHARED_PROCESSORS`, ending with a
        renderer.
    """
    if log_json:
        return [structlog.processors.format_exc_info, structlog.processors.JSONRenderer()]
    return [structlog.dev.ConsoleRenderer(colors=True)]


def configure_logging(settings: Settings) -> None:
    """Configure structlog and the standard-library logging bridge.

    Installs a single root handler that renders every record -- whether it was
    emitted through structlog or through :mod:`logging` -- with the same
    timestamp, level, and logger name. Calling this function again replaces the
    handler it installed previously, so it is safe to call more than once (for
    example once per test) without duplicating output.

    Args:
        settings: Loaded application settings; ``log_level`` selects the
            threshold and ``log_json`` selects the renderer.
    """
    renderer_chain = _build_renderer(settings.log_json)

    structlog.configure(
        processors=[
            *_SHARED_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # Caching would freeze the current configuration into every logger that
        # has already been used, which would make reconfiguration a no-op.
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[*_SHARED_PROCESSORS, structlog.stdlib.ExtraAdder()],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            *renderer_chain,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.set_name(HANDLER_NAME)
    handler.setFormatter(formatter)

    level = logging.getLevelNamesMapping()[settings.log_level]
    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() == HANDLER_NAME:
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    for name in BRIDGED_LOGGERS:
        bridged = logging.getLogger(name)
        bridged.handlers.clear()
        bridged.propagate = True
        bridged.setLevel(level)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger for a module.

    Args:
        name: Logger name, conventionally the module's ``__name__``.

    Returns:
        A structlog logger backed by the standard library, so that its records
        pass through the handler installed by :func:`configure_logging`.
    """
    return structlog.stdlib.get_logger(name)
