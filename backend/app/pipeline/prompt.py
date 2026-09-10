"""System prompt construction (TRD §4.8, TR-070 to TR-072).

The prompt is the agent's whole world. It carries the deck the agent may speak
from, where that deck currently sits on screen, and the style rules that make an
answer sound like a person presenting rather than a chatbot writing. The wording
lives in ``app/prompts/presenter.md`` so it can be edited as product copy,
between demos, without touching code.

Rendering must therefore never raise. Two hazards exist and both are handled
rather than propagated (TR-070): deck copy is arbitrary text that may contain
braces, and a hand-edited template may pick up a placeholder nobody defined or
an unbalanced brace. Deck copy is safe by construction because it is
*substituted in*, never parsed as a format string; the template's own braces are
handled by :class:`SafeDict` and, for the unbalanced case, by a literal
replacement fallback.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..decks.models import Deck
from ..errors import ConfigError
from ..logging_setup import get_logger
from ..protocol import SessionMode
from ..providers.base import Message

logger = get_logger(__name__)

QUESTION_PREFIX_MARKER = "[Looking at slide "
"""Opening of the context stamped onto each question; also detects a double stamp."""

DEFAULT_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "prompts" / "presenter.md"
"""The presenter template, resolved from this package rather than the process's
working directory, which differs between uvicorn, pytest, and an editor."""

TEMPLATE_ENCODING = "utf-8"
"""Encoding of the template file; the copy contains non-ASCII punctuation."""

SNAPSHOT_CURRENT_SLIDE = "current_slide"
"""Snapshot key holding the 1-based slide the audience is looking at."""

SNAPSHOT_PRESENTATION_CURSOR = "presentation_cursor"
"""Snapshot key holding the slide an unattended walkthrough would resume from."""

SNAPSHOT_MODE = "mode"
"""Snapshot key holding the :class:`~app.protocol.SessionMode`."""

FIRST_SLIDE = 1
"""Fallback for both cursors when a snapshot omits them, e.g. before the first turn."""


class SafeDict(dict[str, Any]):
    """A format mapping that leaves unknown placeholders untouched.

    ``"{tone}".format_map(SafeDict())`` yields ``"{tone}"`` instead of raising
    ``KeyError``, so adding a placeholder to the template is never a crash — the
    unrendered name simply shows up in the prompt, which is visible in the logs
    and harmless to the model.
    """

    def __missing__(self, key: str) -> str:
        """Return the placeholder unchanged.

        Args:
            key: The placeholder name that has no value.

        Returns:
            The name wrapped back in the braces it was written with.
        """
        return "{" + key + "}"


@lru_cache(maxsize=4)
def load_template(path: Path = DEFAULT_TEMPLATE_PATH) -> str:
    """Read a prompt template from disk, once per process.

    The cache exists because a builder is constructed per session while the file
    never changes inside a run. ``lru_cache`` does not memoise exceptions, so a
    template fixed on disk is picked up by the next attempt.

    Args:
        path: File to read; defaults to :data:`DEFAULT_TEMPLATE_PATH`.

    Returns:
        The template text.

    Raises:
        ConfigError: If the file is missing or unreadable. A prompt the agent
            cannot load is a deployment fault, not a runtime condition.
    """
    try:
        return path.read_text(encoding=TEMPLATE_ENCODING)
    except OSError as exc:
        msg = f"prompt template {path} could not be read: {exc}"
        raise ConfigError(msg) from exc


def _stamp_last_question(messages: list[Message], deck: Deck, snapshot: Mapping[str, Any]) -> None:
    """Prefix the user's question with the slide they are looking at.

    The context goes *inside* the question rather than beside it, and that
    placement was reached by elimination against a live model. Naming the slide
    only in the system prompt lost to a near-identical exchange higher up the
    conversation: asked the same thing twice on different slides, the model
    replayed its first answer word for word. A system message placed just before
    the question did not help either, because the question was identical to the
    earlier one and the earlier answer sat directly above it. Appending the
    system message after the question was better and still missed. A model
    cannot skim past a phrase inside the sentence it is answering, so that is
    where the context now lives.

    The system prompt tells the agent that a bracketed prefix is context for it
    and is never read aloud.

    Args:
        messages: The message list, modified in place.
        deck: The deck being presented.
        snapshot: The slide controller's snapshot.
    """
    index = snapshot.get("current_slide")
    if not isinstance(index, int) or not 1 <= index <= deck.last_index:
        return
    slide = deck.slide(index)
    for position in range(len(messages) - 1, -1, -1):
        message = messages[position]
        if message.role != "user":
            continue
        if message.content.startswith(QUESTION_PREFIX_MARKER):
            return
        prefix = f'{QUESTION_PREFIX_MARKER}{index} of {deck.last_index}: "{slide.title}"] '
        messages[position] = message.model_copy(update={"content": prefix + message.content})
        return


def _current_slide(snapshot: Mapping[str, Any]) -> int | None:
    """Read the current slide index out of a controller snapshot.

    Args:
        snapshot: The mapping produced by
            :meth:`~app.pipeline.slides.SlideController.snapshot`.

    Returns:
        The 1-based current slide, or ``None`` when the snapshot does not carry
        one, in which case the caller falls back to embedding every slide's
        notes.
    """
    value = snapshot.get("current_slide")
    return value if isinstance(value, int) else None


def render_deck_json(deck: Deck, current_slide: int | None = None) -> str:
    """Serialise a deck for embedding in the prompt (TR-071).

    Only the fields the agent reasons over are included, and the JSON is compact
    because every separator costs input tokens on every turn.

    Notes are included in full **only for the current slide**. Every other slide
    contributes its title, bullets, and aliases, which is all the model needs to
    decide where to navigate. This matters for more than tidiness: embedding all
    six slides' notes cost roughly 1,570 tokens per request, and Groq's free tier
    allows 8,000 tokens per minute, which capped the agent at about two requests
    a minute -- less than one exchange, since a turn that calls a tool needs two.
    Sending the notes the agent is actually speaking from cuts that sharply. The
    cost is that the agent must navigate before it can quote another slide in
    detail, which is the behaviour the prompt asks for anyway.

    Args:
        deck: The deck being presented.
        current_slide: 1-based index whose notes to include in full. When
            ``None``, every slide's notes are included, which is the right
            behaviour for tests and for any caller without a cursor.

    Returns:
        Compact JSON holding the deck title and, per slide, its index, title,
        bullets, and -- for the current slide -- its notes.
    """
    slides: list[dict[str, Any]] = []
    for slide in deck.slides:
        # Aliases are deliberately NOT sent. They exist for the server-side
        # keyword fallback in SlideController, which runs in Python after the
        # model answers; the model routes perfectly well from titles and
        # bullets, and the alias lists cost roughly 300 tokens on every request
        # against a 8,000-token-per-minute budget.
        entry: dict[str, Any] = {
            "index": slide.index,
            "title": slide.title,
            "bullets": slide.bullets,
        }
        if current_slide is None or slide.index == current_slide:
            entry["notes"] = slide.prompt_notes
        slides.append(entry)
    payload = {"title": deck.title, "slides": slides}
    # ensure_ascii=False keeps the truncation ellipsis and any non-ASCII deck
    # copy readable to the model instead of expanding it into escape sequences.
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _render(template: str, values: Mapping[str, Any], source: Path) -> str:
    """Substitute ``values`` into ``template`` without ever raising.

    Args:
        template: Template text containing ``{placeholder}`` names.
        values: Values to substitute.
        source: Where the template came from, named in the warning below so a
            broken edit is traceable to a file.

    Returns:
        The rendered text. Placeholders with no value survive verbatim.
    """
    try:
        return template.format_map(SafeDict(values))
    except (ValueError, IndexError):
        # An unbalanced or positional brace in hand-edited copy: fall back to
        # literal replacement so a typo degrades the prompt instead of breaking
        # every turn mid-demo. Warned, not silent, so it still gets fixed.
        logger.warning("prompt.template_malformed", template=str(source))
        rendered = template
        for key, value in values.items():
            rendered = rendered.replace("{" + key + "}", str(value))
        return rendered


class PromptBuilder:
    """Builds the message list sent to the LLM for one turn.

    The builder is stateless once constructed: the template is read at
    construction so a missing file fails when the session is set up rather than
    in the middle of a turn.

    Args:
        template_path: Template to render; defaults to
            :data:`DEFAULT_TEMPLATE_PATH`.

    Raises:
        ConfigError: If the template cannot be read.
    """

    def __init__(self, template_path: Path | None = None) -> None:
        self._template_path = template_path or DEFAULT_TEMPLATE_PATH
        self._template = load_template(self._template_path)

    def render_system(self, deck: Deck, snapshot: Mapping[str, Any]) -> str:
        """Render the system prompt for a deck and its current position.

        Args:
            deck: The deck being presented; embedded verbatim (TR-071).
            snapshot: ``SlideController.snapshot()`` output (TR-064), read for
                ``current_slide``, ``presentation_cursor``, and ``mode``. Missing
                keys fall back to the first slide and question-answering mode, so
                a turn can be built before any navigation has happened.

        Returns:
            The rendered system prompt.
        """
        values = {
            "deck_json": render_deck_json(deck, _current_slide(snapshot)),
            "current_slide": snapshot.get(SNAPSHOT_CURRENT_SLIDE, FIRST_SLIDE),
            "presentation_cursor": snapshot.get(SNAPSHOT_PRESENTATION_CURSOR, FIRST_SLIDE),
            # StrEnum members render as their value, so a SessionMode and the
            # plain string "qa" produce the same prompt.
            "mode": snapshot.get(SNAPSHOT_MODE, SessionMode.QA),
            "slide_count": len(deck.slides),
        }
        return _render(self._template, values, self._template_path)

    def build(
        self,
        deck: Deck,
        snapshot: Mapping[str, Any],
        history_messages: Sequence[Message],
    ) -> list[Message]:
        """Build the full message list for one turn.

        Args:
            deck: The deck being presented.
            snapshot: The slide controller's snapshot (see :meth:`render_system`).
            history_messages: Conversation history, oldest first, already
                serialised by ``ConversationHistory.to_provider_messages()``.

        Returns:
            A new list holding the system message followed by the history in the
            order given. The caller's sequence is never mutated.
        """
        system = Message(role="system", content=self.render_system(deck, snapshot))
        messages = [system, *history_messages]

        _stamp_last_question(messages, deck, snapshot)

        logger.debug(
            "prompt.built",
            deck_id=deck.id,
            slides=len(deck.slides),
            history=len(history_messages),
            system_chars=len(system.content),
        )
        return messages
