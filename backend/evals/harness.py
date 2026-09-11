"""Drive one turn of the real pipeline, and keep everything it produced.

The runner reuses the shipped pipeline rather than reimplementing it (TR-200): the same
``run_turn``, the same prompt builder, the same slide controller and keyword fallback. Only the two
ends are replaced -- there is no WebSocket, and synthesis is faked unless a suite is measuring it --
so what an eval measures is the agent as it actually ships.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings
from app.decks.models import Deck
from app.decks.repository import DeckRepository
from app.errors import ProviderError
from app.pipeline.history import ConversationHistory
from app.pipeline.metrics import TurnMetrics
from app.pipeline.prompt import PromptBuilder
from app.pipeline.slides import SlideAction, SlideController
from app.pipeline.turn import run_turn
from app.protocol import ServerMessage
from app.providers.base import LLMProvider, TTSProvider
from app.providers.groq_llm import GroqLLM
from app.providers.kokoro_tts import KokoroTTS
from app.providers.ollama_llm import OllamaLLM

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:  # pragma: no cover - import convenience for `python -m evals`
    sys.path.insert(0, str(BACKEND))

from tests.fakes import FakeTTS  # noqa: E402 - needs the path above

DEFAULT_DECK = "anatomy_of_a_voice_agent"
"""The deck every suite runs against."""

RATE_LIMIT_ATTEMPTS = 5
"""How many times an item waits out a rate limit before it is recorded as a failure.

The product does not retry: a person who asked a question is owed an answer or an explanation, not
a silent wait. An eval is the opposite -- a 429 says nothing about the agent's behaviour, and
counting it as a wrong answer would make the measurement a measurement of the free tier.
"""

MAX_RATE_LIMIT_WAIT_S = 300.0
"""Longest to wait on one attempt.

Generous on purpose. When the daily budget is nearly spent the free tier stops answering in
minutes rather than seconds -- observed waits of 115 to 224 seconds -- and an opt-in eval run can
afford to sit through that where a live session cannot. Past five minutes the budget is gone rather
than throttled, and no amount of patience recovers it; those items are recorded as failures and
counted in the run's notes rather than as wrong answers.
"""


@dataclass(slots=True)
class TurnTrace:
    """Everything one evaluated turn produced.

    Attributes:
        answer: What the agent said, as one string.
        sentences: The answer split the way it was spoken.
        actions: Navigation the turn applied, in order.
        final_slide: Where the deck ended up.
        messages: Every protocol message the turn emitted.
        metrics: Stage timings for the turn.
        error: The failure, when the turn raised instead of answering.
    """

    answer: str = ""
    sentences: list[str] = field(default_factory=list)
    actions: list[SlideAction] = field(default_factory=list)
    final_slide: int = 1
    messages: list[ServerMessage] = field(default_factory=list)
    metrics: TurnMetrics | None = None
    error: str | None = None

    @property
    def navigated(self) -> bool:
        """Whether the deck moved at all, by either route."""
        return bool(self.actions)

    @property
    def tool_actions(self) -> list[SlideAction]:
        """Navigation the model asked for, as opposed to the server's fallback."""
        return [action for action in self.actions if action.source == "llm"]


def build_llm(
    settings: Settings,
    model: str | None = None,
    *,
    provider: str = "groq",
    temperature: float | None = None,
) -> LLMProvider:
    """Build the model provider an eval calls.

    Args:
        settings: Loaded settings, supplying the credential and base URL.
        model: Model identifier to override the configured one, for comparisons.
        provider: ``groq`` for the hosted models, ``ollama`` for one running on
            this machine. The local fallback has to be measurable by the same
            suites as the hosted model, or choosing it is guesswork (TR-085).
        temperature: Sampling temperature; the judge uses zero, because a rubric
            graded differently on two runs is not a measurement (TR-201).

    Returns:
        The provider.
    """
    sampling = settings.llm_temperature if temperature is None else temperature
    if provider == "ollama":
        return OllamaLLM(
            model=model or settings.ollama_model,
            base_url=settings.ollama_base_url,
            temperature=sampling,
            max_tokens=settings.llm_max_tokens,
        )
    key = settings.groq_api_key
    if key is None:
        msg = "GROQ_API_KEY is not set; evals call real models"
        raise SystemExit(msg)
    return GroqLLM(
        api_key=key.get_secret_value(),
        base_url=settings.groq_base_url,
        model=model or settings.groq_llm_model,
        temperature=sampling,
        max_tokens=settings.llm_max_tokens,
    )


def build_tts(settings: Settings, *, real: bool) -> TTSProvider:
    """Build a synthesiser.

    Args:
        settings: Loaded settings, for the voice and weights directory.
        real: Whether to load Kokoro. Only the latency suite needs it; for every
            other suite the audio is irrelevant and loading a model would add
            seconds per item for nothing.

    Returns:
        The synthesiser.
    """
    if not real:
        return FakeTTS()
    return KokoroTTS(
        models_dir=settings.kokoro_models_dir,
        voice=settings.kokoro_voice,
        speed=settings.kokoro_speed,
        download=settings.kokoro_download,
    )


def load_deck(deck_id: str = DEFAULT_DECK) -> Deck:
    """Load the deck the suites present.

    Args:
        deck_id: Which deck to load.

    Returns:
        The deck.
    """
    return DeckRepository().get(deck_id)


async def run_one(
    *,
    utterance: str,
    deck: Deck,
    llm: LLMProvider,
    tts: TTSProvider,
    prompts: PromptBuilder,
    current_slide: int = 1,
    history: ConversationHistory | None = None,
) -> TurnTrace:
    """Run a single turn and collect what it produced.

    Args:
        utterance: What the user said or typed.
        deck: The deck being presented.
        llm: Model provider.
        tts: Synthesis provider.
        prompts: Prompt builder.
        current_slide: Where the deck is before the turn.
        history: Conversation so far; a fresh one is used when omitted.

    Returns:
        The trace, with ``error`` set when the provider failed.
    """
    slides = SlideController(deck, current_slide=current_slide)
    conversation = history if history is not None else ConversationHistory()
    metrics = TurnMetrics(turn_id=1)
    trace = TurnTrace(final_slide=current_slide, metrics=metrics)

    async def emit(message: ServerMessage) -> None:
        trace.messages.append(message)

    async def send_audio(_frame: bytes) -> None:
        return None

    for attempt in range(RATE_LIMIT_ATTEMPTS):
        try:
            result = await run_turn(
                turn_id=1,
                text=utterance,
                deck=deck,
                llm=llm,
                tts=tts,
                voice=deck.voice,
                history=conversation,
                slides=slides,
                prompts=prompts,
                metrics=metrics,
                emit=emit,
                send_audio=send_audio,
            )
            break
        except ProviderError as exc:
            wait = exc.retry_after if exc.retryable else None
            last = attempt == RATE_LIMIT_ATTEMPTS - 1
            if wait is None or last or wait > MAX_RATE_LIMIT_WAIT_S:
                trace.error = f"{type(exc).__name__}: {exc}"
                trace.final_slide = slides.current_slide
                return trace
            # The deck moved on the attempt that failed, so the controller is reset with it.
            slides = SlideController(deck, current_slide=current_slide)
            await asyncio.sleep(wait + 0.5)
        except Exception as exc:  # an eval records a failure as a result, rather than dying
            trace.error = f"{type(exc).__name__}: {exc}"
            trace.final_slide = slides.current_slide
            return trace
    else:  # pragma: no cover - the loop always breaks or returns
        return trace

    trace.answer = result.text
    trace.sentences = list(result.sentences or [])
    trace.actions = list(result.actions or [])
    trace.final_slide = slides.current_slide
    return trace


LIMIT: int | None = None
"""Cap on items per dataset, set by ``--limit`` for a cheap smoke run.

Module state rather than a parameter threaded through every suite, because it exists only so a
change to the runner can be proved to work without spending a hundred model calls on it.
"""


def read_dataset(name: str) -> list[dict[str, Any]]:
    """Read one JSONL dataset.

    Args:
        name: File name under ``evals/datasets``.

    Returns:
        One dictionary per line, blank lines and ``#`` comments skipped, capped
        by :data:`LIMIT` when the runner set one.
    """
    path = Path(__file__).resolve().parent / "datasets" / name
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        items.append(json.loads(stripped))
    return items[:LIMIT] if LIMIT is not None else items
