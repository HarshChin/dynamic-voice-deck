"""Deck loading, validation, and lookup (TRD §4.1, TR-151).

Decks are read once, at startup, from the JSON files sitting beside this module.
A deck that fails to parse or validate is a *configuration* error: the process
should refuse to start rather than serve a session whose ground truth is wrong,
so :class:`DeckRepository` raises :class:`~app.errors.DeckError` from its
constructor instead of skipping the offending file.

The other half of that contract is the message. Pydantic's own
``ValidationError`` text is a wall of internal locations and URLs; an operator
who mistyped a slide index needs the file name and the broken field, so every
failure here is reformatted into one line naming both.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ValidationError

from ..errors import DeckError
from ..logging_setup import get_logger
from .models import Deck

logger = get_logger(__name__)

DECK_DIRECTORY: Final[Path] = Path(__file__).parent
"""Directory scanned for deck files: the package directory holding this module."""

DECK_GLOB: Final[str] = "*.json"
"""Glob matching the deck files inside :data:`DECK_DIRECTORY`."""

MAX_REPORTED_PROBLEMS: Final[int] = 5
"""Validation problems quoted in a :class:`~app.errors.DeckError` message.

One malformed slide typically trips several validators at once, and a log line
carrying forty of them is no more actionable than one carrying five.
"""


class DeckSummary(BaseModel):
    """A deck reduced to what a listing needs (TRD §4.10, ``GET /api/decks``).

    Attributes:
        id: The deck's stable identifier.
        title: Human-readable deck name.
        slide_count: Number of slides in the deck.
    """

    id: str
    title: str
    slide_count: int


def _describe(error: ValidationError) -> str:
    """Render a pydantic validation failure as one operator-readable clause.

    Args:
        error: The failure raised by :meth:`Deck.model_validate`.

    Returns:
        Semicolon-separated ``location: message`` pairs, capped at
        :data:`MAX_REPORTED_PROBLEMS` with a count of what was elided.
    """
    problems: list[str] = []
    for detail in error.errors()[:MAX_REPORTED_PROBLEMS]:
        # An empty `loc` means the error is about the deck as a whole (for
        # example the model-level alias-uniqueness check), which has no field
        # path to quote.
        location = ".".join(str(part) for part in detail["loc"]) or "deck"
        problems.append(f"{location}: {detail['msg']}")

    elided = error.error_count() - len(problems)
    if elided > 0:
        problems.append(f"(+{elided} more)")
    return "; ".join(problems)


def _load_deck_file(path: Path) -> Deck:
    """Read and validate a single deck file.

    Args:
        path: Path to the JSON file.

    Returns:
        The validated deck.

    Raises:
        DeckError: If the file cannot be read, is not valid JSON, or does not
            satisfy the :class:`~app.decks.models.Deck` schema. The message
            names the file and the specific problem.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"deck file {path.name} could not be read: {exc.strerror or exc}"
        raise DeckError(msg) from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"deck file {path.name} is not valid JSON: {exc.msg} at line {exc.lineno}"
        raise DeckError(msg) from exc

    try:
        return Deck.model_validate(payload)
    except ValidationError as exc:
        msg = f"deck file {path.name} failed validation: {_describe(exc)}"
        raise DeckError(msg) from exc


class DeckRepository:
    """The decks this process can present, loaded once and held in memory.

    Files are read in the constructor so that a broken deck stops startup
    (TR-151) rather than surfacing on a user's first request. Nothing re-reads
    the directory afterwards; :meth:`register` is the only way the contents
    change, and it exists for the decks a later phase generates at runtime.

    Args:
        directory: Directory to scan for ``*.json`` deck files. Defaults to
            :data:`DECK_DIRECTORY`, the package directory. Tests pass a
            temporary directory so they never touch the shipped deck.

    Raises:
        DeckError: If any file in ``directory`` fails to load, or if two files
            declare the same deck id.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory if directory is not None else DECK_DIRECTORY
        self._decks: dict[str, Deck] = {}
        self._load()

    def _load(self) -> None:
        """Read every deck file in the configured directory into the cache.

        Raises:
            DeckError: If a file fails to load, or two files share a deck id.
        """
        # Sorted so that a duplicate-id collision names the same pair of files
        # on every machine, whatever order the filesystem hands them back in.
        sources: dict[str, str] = {}
        for path in sorted(self._directory.glob(DECK_GLOB)):
            deck = _load_deck_file(path)
            if deck.id in sources:
                msg = (
                    f"deck file {path.name} declares id {deck.id!r}, which "
                    f"{sources[deck.id]} already declares; deck ids must be unique"
                )
                raise DeckError(msg)
            sources[deck.id] = path.name
            self._decks[deck.id] = deck

        logger.info(
            "decks.loaded",
            directory=str(self._directory),
            count=len(self._decks),
            deck_ids=sorted(self._decks),
        )

    def list_decks(self) -> list[DeckSummary]:
        """Summarise every known deck, ordered by id.

        Returns:
            One :class:`DeckSummary` per deck, sorted by id so the listing is
            stable across restarts.
        """
        return [
            DeckSummary(id=deck.id, title=deck.title, slide_count=len(deck.slides))
            for deck in sorted(self._decks.values(), key=lambda deck: deck.id)
        ]

    def get(self, deck_id: str) -> Deck:
        """Return one deck by id.

        Args:
            deck_id: The identifier to look up.

        Returns:
            The deck.

        Raises:
            DeckError: If no deck carries that id. The message lists the ids
                that do exist, because the usual cause is a typo.
        """
        deck = self._decks.get(deck_id)
        if deck is None:
            known = ", ".join(sorted(self._decks)) or "none"
            msg = f"unknown deck {deck_id!r}; known decks: {known}"
            raise DeckError(msg)
        return deck

    def register(self, deck: Deck) -> None:
        """Add a deck built at runtime, such as one generated from a topic.

        Args:
            deck: An already-validated deck. Generated decks are given a fresh
                UUID id (TR-151), so a collision means a bug in the generator
                rather than a benign update, and is refused.

        Raises:
            DeckError: If a deck with the same id is already registered.
        """
        if deck.id in self._decks:
            msg = f"deck {deck.id!r} is already registered"
            raise DeckError(msg)
        self._decks[deck.id] = deck
        logger.info("decks.registered", deck_id=deck.id, slide_count=len(deck.slides))

    def __len__(self) -> int:
        """Return the number of decks held.

        Returns:
            The deck count, including any registered at runtime.
        """
        return len(self._decks)
