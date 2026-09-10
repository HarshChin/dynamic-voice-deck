"""FastAPI application factory, lifespan, and HTTP routes.

Phase 0 serves only the health endpoint (TRD §4.10, TR-192). The WebSocket
session route, deck routes, and provider wiring arrive in later phases.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from pydantic_settings import SettingsError

from . import __version__
from .config import Settings, get_settings
from .errors import AppError, ConfigError
from .logging_setup import configure_logging, get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["system"])
"""Router holding the HTTP (non-WebSocket) routes of the application."""


@dataclass(slots=True)
class AppState:
    """Process-wide state attached to ``app.state`` for the app's lifetime.

    Attributes:
        settings: The settings singleton loaded during startup.
        tts_warm: Whether the TTS engine has completed a warm-up synthesis.
    """

    settings: Settings
    tts_warm: bool = False

    # TODO(hv): Phase 1 adds the deck repository and the STT/LLM/TTS providers
    # here -- see TRD §4.1 (module layout) and §4.9 TR-080
    # (``registry.build_providers(settings)``). The lifespan will build them,
    # store them on this dataclass, and set ``tts_warm`` after the warm-up
    # synthesis required by TR-013.


class HealthProviders(BaseModel):
    """Names of the configured provider implementations.

    Attributes:
        stt: Selected speech-to-text provider.
        llm: Selected language-model provider.
        tts: Selected text-to-speech provider.
    """

    stt: str
    llm: str
    tts: str


class HealthResponse(BaseModel):
    """Body of ``GET /api/health``.

    Attributes:
        status: Always ``"ok"``; a failing process cannot answer at all.
        version: Version of the backend application package.
        providers: Names of the configured providers.
        tts_warm: Whether the TTS engine is warmed up and ready to synthesise.
    """

    status: Literal["ok"] = "ok"
    version: str
    providers: HealthProviders
    tts_warm: bool = False


def get_app_state(request: Request) -> AppState:
    """Return the application state attached to the running application.

    Args:
        request: The incoming request, used to reach ``request.app.state``.

    Returns:
        The :class:`AppState` created by the lifespan. If the lifespan has not
        run -- as happens with a test client used outside a context manager --
        a default state is created from the settings singleton and stored.
    """
    state: AppState | None = getattr(request.app.state, "app_state", None)
    if state is None:
        state = AppState(settings=get_settings())
        request.app.state.app_state = state
    return state


@router.get("/health", summary="Liveness and configuration probe")
async def health(request: Request) -> HealthResponse:
    """Report liveness, version, selected providers, and TTS warm status.

    Args:
        request: The incoming request, used to read the application state.

    Returns:
        The health payload described by :class:`HealthResponse`.
    """
    state = get_app_state(request)
    settings = state.settings
    return HealthResponse(
        version=__version__,
        providers=HealthProviders(
            stt=settings.stt_provider,
            llm=settings.llm_provider,
            tts=settings.tts_provider,
        ),
        tts_warm=state.tts_warm,
    )


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Convert an :class:`~app.errors.AppError` into a JSON error response.

    Registered for :class:`~app.errors.AppError` only, so ``exc`` is always one
    of the application's own errors; the broad annotation matches the signature
    Starlette expects from an exception handler.

    Args:
        request: The request that raised the error.
        exc: The raised error.

    Returns:
        A ``500`` response with the error class name and message.
    """
    logger.error(
        "http.app_error",
        error=type(exc).__name__,
        message=str(exc),
        path=request.url.path,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": type(exc).__name__, "message": str(exc)},
    )


def _load_settings() -> Settings:
    """Load the settings singleton, failing fast on invalid configuration.

    Returns:
        The validated settings.

    Raises:
        ConfigError: If the environment or ``.env`` file fails validation.
            Both failure modes are wrapped: ``ValidationError`` for a value that
            fails a field's constraints, and ``SettingsError`` for one
            pydantic-settings cannot even parse (it JSON-decodes complex-typed
            fields such as ``cors_origins`` inside the settings *source*, before
            any field validator runs).
    """
    try:
        return get_settings()
    except (ValidationError, SettingsError) as exc:
        msg = f"Invalid configuration; see .env.example for every variable: {exc}"
        raise ConfigError(msg) from exc


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Set up and tear down process-wide resources.

    Args:
        app: The application whose ``state`` receives the :class:`AppState`.

    Yields:
        ``None`` once startup is complete; the block after the yield runs on
        shutdown.
    """
    settings = _load_settings()
    configure_logging(settings)
    app.state.app_state = AppState(settings=settings)
    # ``masked_dump`` is mandatory here: the raw settings carry the Groq API key
    # and must never reach the logs (TR-180).
    logger.info("app.startup", version=__version__, settings=settings.masked_dump())

    # TODO(hv): Phase 1 loads the DeckRepository and builds the providers here,
    # then warms Kokoro and flips ``AppState.tts_warm`` -- TRD §4.1 and §4.9
    # (TR-080), with the warm-up budget in TR-013.

    try:
        yield
    finally:
        logger.info("app.shutdown", version=__version__)


def create_app() -> FastAPI:
    """Build the FastAPI application.

    Returns:
        An application with CORS configured from the settings, the system
        router mounted, and the :class:`~app.errors.AppError` handler
        registered.
    """
    settings = _load_settings()
    app = FastAPI(
        title="Dynamic Voice Deck",
        version=__version__,
        summary="Voice-first slide presenter backend.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Equivalent to ``add_exception_handler`` but typed to accept an async
    # handler, which Starlette supports at runtime yet types as synchronous.
    app.exception_handler(AppError)(app_error_handler)
    app.include_router(router)
    return app


app = create_app()
"""Module-level application instance for ``uvicorn app.main:app``."""
