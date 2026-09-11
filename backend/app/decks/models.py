"""Deck and slide models.

A deck is the agent's ground truth. The system prompt embeds it verbatim
(TR-071), the :class:`~app.pipeline.slides.SlideController` validates navigation
against it (TR-061), and the keyword fallback scores utterances against each
slide's aliases (TR-062). Validation is therefore strict: a malformed deck is a
configuration error, not something to tolerate at runtime.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, field_validator, model_validator

MIN_SLIDES = 5
"""Fewest slides a deck may contain (PRD §4 asks for five to six)."""

MAX_SLIDES = 8
"""Most slides a deck may contain; beyond this the prompt grows unwieldy."""

MAX_NOTES_CHARS = 1_200
"""Longest speaker notes accepted per slide (TR-150)."""

PROMPT_NOTES_CHARS = 1_100
"""Notes are truncated to this length when embedded in the prompt (TR-071).

Only the slide currently on screen contributes its notes to the prompt
(:func:`app.pipeline.prompt.render_deck_json`), so this cap now guards one slide
rather than six. That changes what it is worth: at 600 it saved roughly ninety
input tokens a turn and cost the second half of every authored slide, which is
where the notes stop restating the bullets and start explaining them. The
shipped deck runs 818 to 1,017 characters a slide, so 1,100 carries every
authored note whole and still leaves room to edit one.

It stays below :data:`MAX_NOTES_CHARS` on purpose. The deck validator's limit is
an authoring rule; this is a prompt budget, and a deck written right up to the
authoring limit must still be truncated rather than pushed into the prompt
whole.
"""

MIN_ALIASES = 2
"""Fewest routing aliases a slide must declare, so fallback routing has signal."""


class FigureKind(StrEnum):
    """How a slide arranges its points on screen (PRD F1).

    The kind changes the arrangement, never the content: every layout renders
    the same bullets, which is what keeps the agent's view of a slide and the
    audience's view of it the same thing.
    """

    METRICS = "metrics"
    """Value cards. For a slide whose points are mostly numbers."""

    SPLIT = "split"
    """Labelled columns. For a slide with two or three sides to it."""

    FLOW = "flow"
    """Numbered steps with connectors. For a slide describing a sequence."""


class FigureItem(BaseModel):
    """One card, column or step, and the bullets it presents.

    Attributes:
        bullets: Indices into :attr:`Slide.bullets`, in the order they should
            appear. The reference is what stops the screen and the prompt from
            drifting apart: a point the model can talk about is a point the room
            can see, because the same list produces both.
        heading: The large text. A measurement for a metric card, a side for a
            column, the name of a step for a flow.
        caption: A supporting line under the heading, or empty.
    """

    bullets: list[int] = Field(min_length=1)
    heading: str = Field(min_length=1, max_length=40)
    caption: str = Field(default="", max_length=80)


class Figure(BaseModel):
    """The arrangement of a slide's points.

    Attributes:
        kind: Which arrangement to draw.
        items: The cards, columns or steps, in order.
    """

    kind: FigureKind
    items: list[FigureItem] = Field(min_length=1, max_length=6)


class Slide(BaseModel):
    """One slide, with the notes and aliases the agent reasons over.

    Attributes:
        index: Position in the deck, 1-based and contiguous.
        title: Short heading shown on screen and used for routing.
        bullets: Points rendered on the slide; also weak routing signal.
        notes: Speaker notes the agent treats as ground truth. The agent may
            only assert what these support.
        aliases: Phrases a user might say to mean this slide. Used by the
            keyword fallback when the model does not call the navigation tool.
        figure: How the bullets are arranged on screen, or ``None`` for a plain
            list. Presentation only: the agent never sees it, because every
            point it could mention is in ``bullets`` either way.
    """

    index: int = Field(ge=1, le=MAX_SLIDES)
    title: str = Field(min_length=1, max_length=120)
    bullets: list[str] = Field(min_length=1, max_length=6)
    notes: str = Field(min_length=1, max_length=MAX_NOTES_CHARS)
    aliases: list[str] = Field(min_length=MIN_ALIASES)
    figure: Figure | None = None

    @model_validator(mode="after")
    def _figure_shows_every_bullet_once(self) -> Self:
        """Check the figure presents each bullet exactly once.

        This is the rule the whole design rests on. The model is given the
        bullets and nothing else, so a bullet the figure omits is something the
        agent can assert that nobody can see, and a bullet shown twice is a
        point the room hears about once and reads twice. Either is a slide that
        lies about itself, and neither is visible without this check.

        Returns:
            The validated slide.

        Raises:
            ValueError: If the figure omits, repeats, or invents a bullet.
        """
        if self.figure is None:
            return self
        shown = [index for item in self.figure.items for index in item.bullets]
        expected = list(range(len(self.bullets)))
        if sorted(shown) != expected:
            msg = (
                f"slide {self.index}: the figure must show every bullet exactly once; "
                f"it references {sorted(shown)} of {expected}"
            )
            raise ValueError(msg)
        return self

    @field_validator("bullets", "aliases")
    @classmethod
    def _no_blank_entries(cls, value: list[str]) -> list[str]:
        """Reject blank or whitespace-only list entries.

        Args:
            value: The list being validated.

        Returns:
            The list with every entry stripped.

        Raises:
            ValueError: If any entry is empty once stripped.
        """
        stripped = [item.strip() for item in value]
        if any(not item for item in stripped):
            msg = "entries must not be blank"
            raise ValueError(msg)
        return stripped

    @field_validator("aliases")
    @classmethod
    def _aliases_are_lowercase_and_unique(cls, value: list[str]) -> list[str]:
        """Normalise aliases to lower case and reject duplicates within a slide.

        Args:
            value: The alias list.

        Returns:
            The aliases, lower-cased.

        Raises:
            ValueError: If the same alias appears twice on one slide.
        """
        lowered = [item.lower() for item in value]
        if len(set(lowered)) != len(lowered):
            msg = "aliases must be unique within a slide"
            raise ValueError(msg)
        return lowered

    @property
    def prompt_notes(self) -> str:
        """Return the notes truncated for prompt embedding (TR-071).

        Authored notes are expected to pass through untouched; truncation is the
        guard against a deck written up to :data:`MAX_NOTES_CHARS`, not the
        normal path.

        Returns:
            The notes, shortened to :data:`PROMPT_NOTES_CHARS` with an ellipsis
            when they exceed it.
        """
        if len(self.notes) <= PROMPT_NOTES_CHARS:
            return self.notes
        return self.notes[: PROMPT_NOTES_CHARS - 1].rstrip() + "…"


class Deck(BaseModel):
    """A presentable deck.

    Attributes:
        id: Stable identifier used in URLs and ``session.start``.
        title: Human-readable deck name.
        voice: Kokoro voice used to present it, overriding the configured default.
        slides: The slides, in order.
    """

    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=160)
    voice: str | None = None
    slides: list[Slide] = Field(min_length=MIN_SLIDES, max_length=MAX_SLIDES)

    @model_validator(mode="after")
    def _indices_are_contiguous_and_aliases_unique(self) -> Self:
        """Check deck-wide invariants that a per-slide validator cannot see.

        Returns:
            The validated deck.

        Raises:
            ValueError: If slide indices are not ``1..n`` in order, or if any
                alias is claimed by more than one slide. A shared alias would
                make fallback routing ambiguous, so it is rejected outright, and
                the message names both slides to make the fix obvious.
        """
        expected = list(range(1, len(self.slides) + 1))
        actual = [slide.index for slide in self.slides]
        if actual != expected:
            msg = f"slide indices must be {expected}, got {actual}"
            raise ValueError(msg)

        owner: dict[str, int] = {}
        for slide in self.slides:
            for alias in slide.aliases:
                if alias in owner:
                    msg = (
                        f"alias {alias!r} is claimed by slide {owner[alias]} "
                        f"and slide {slide.index}; aliases must be unique across the deck"
                    )
                    raise ValueError(msg)
                owner[alias] = slide.index
        return self

    def slide(self, index: int) -> Slide:
        """Return the slide at a 1-based index.

        Args:
            index: The slide number.

        Returns:
            The slide.

        Raises:
            IndexError: If ``index`` is outside the deck.
        """
        if not 1 <= index <= len(self.slides):
            msg = f"slide {index} is outside deck {self.id!r} (1..{len(self.slides)})"
            raise IndexError(msg)
        return self.slides[index - 1]

    @property
    def last_index(self) -> int:
        """Return the index of the final slide.

        Returns:
            The 1-based index of the last slide.
        """
        return len(self.slides)
