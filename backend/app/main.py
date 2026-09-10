"""FastAPI application factory, lifespan, and HTTP routes.

Phase 0 serves only the health endpoint (TRD §4.10, TR-192). The WebSocket
session route, deck routes, and provider wiring arrive in later phases.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

from fastapi import (
    APIRouter,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from pydantic_settings import SettingsError

from . import __version__
from .config import Settings, get_settings
from .decks.models import Deck
from .decks.repository import DeckRepository, DeckSummary
from .errors import AppError, ConfigError, DeckError
from .logging_setup import configure_logging, get_logger
from .pipeline.prompt import PromptBuilder
from .providers.base import Providers
from .providers.registry import build_providers
from .session import Session, SessionManager

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
    decks: DeckRepository | None = None
    providers: Providers | None = None
    prompts: PromptBuilder | None = None
    sessions: SessionManager = field(default_factory=SessionManager)

    # TODO(hv): milestone M2 warms Kokoro during startup and flips ``tts_warm``
    # once the throwaway synthesis completes (TR-013).


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
    decks = DeckRepository()
    providers = build_providers(settings)
    app.state.app_state = AppState(
        settings=settings,
        decks=decks,
        providers=providers,
        prompts=PromptBuilder(),
    )
    # ``masked_dump`` is mandatory here: the raw settings carry the Groq API key
    # and must never reach the logs (TR-180).
    logger.info("app.startup", version=__version__, settings=settings.masked_dump())

    warm = getattr(providers.tts, "warm_up", None)
    if warm is not None:
        # Loading weights and running one throwaway synthesis costs about a
        # second. Paying it here means the first person to speak does not.
        try:
            await warm()
            app.state.app_state.tts_warm = True
        except AppError as exc:
            # A voice that will not load is not a reason to refuse to serve: the
            # deck, the routing and the transcript all still work.
            logger.error("tts.warm_up_failed", error=str(exc))

    logger.info(
        "app.ready",
        decks=[summary.id for summary in decks.list_decks()],
        providers=providers.names,
        tts_warm=app.state.app_state.tts_warm,
    )

    try:
        yield
    finally:
        await app.state.app_state.sessions.close_all()
        # Sessions first, providers second: a session still streaming a turn is
        # holding a connection from the pool this closes.
        await providers.aclose()
        logger.info("app.shutdown", version=__version__)


@router.get("/decks", summary="List available decks")
async def list_decks(request: Request) -> list[DeckSummary]:
    """Return a summary of every loaded deck.

    Args:
        request: The incoming request, used to reach the deck repository.

    Returns:
        One summary per deck, ordered by id.
    """
    return _require_decks(request).list_decks()


@router.get("/decks/{deck_id}", summary="Fetch one deck")
async def get_deck(request: Request, deck_id: str) -> Deck:
    """Return a deck by id.

    Args:
        request: The incoming request.
        deck_id: Identifier of the deck to fetch.

    Returns:
        The deck.

    Raises:
        HTTPException: With status 404 when no such deck exists.
    """
    try:
        return _require_decks(request).get(deck_id)
    except DeckError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def _require_decks(request: Request) -> DeckRepository:
    """Return the deck repository, or explain that startup did not run.

    Args:
        request: The incoming request.

    Returns:
        The repository built during startup.

    Raises:
        HTTPException: With status 503 when the lifespan has not completed, which
            is the honest answer: the process is up but not yet able to serve.
    """
    decks = get_app_state(request).decks
    if decks is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="application startup has not completed",
        )
    return decks


async def session_endpoint(websocket: WebSocket) -> None:
    """Serve one voice session over a WebSocket (TRD §6).

    Args:
        websocket: The incoming connection.
    """
    state: AppState = websocket.app.state.app_state
    if state.decks is None or state.providers is None or state.prompts is None:
        await websocket.close(code=1011, reason="startup incomplete")
        return

    await websocket.accept()
    session = Session(
        websocket,
        settings=state.settings,
        providers=state.providers,
        decks=state.decks,
        prompts=state.prompts,
    )
    state.sessions.add(session)
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            text = message.get("text")
            if text is not None:
                await session.handle_raw_text(text)
                continue
            payload = message.get("bytes")
            if payload is not None:
                await session.handle_utterance(payload)
    except WebSocketDisconnect:
        pass
    finally:
        await state.sessions.remove(session)


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
    app.add_api_websocket_route("/ws/session", session_endpoint, name="session")
    return app


app = create_app()
"""Module-level application instance for ``uvicorn app.main:app``."""
