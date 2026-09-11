"""Tests for the deck models and :class:`~app.decks.repository.DeckRepository`.

Two things are pinned here. The first is the shipped deck itself: it is the
agent's ground truth, embedded in the system prompt, so a slide that quietly
grows past a budget or steals another slide's routing alias degrades the demo
instead of failing loudly. The second is the repository's error contract — a
malformed deck must surface as a :class:`~app.errors.DeckError` naming the file
and the broken field, because the person reading that line is an operator with
a typo, not a pydantic maintainer.

Every case that needs a bad deck writes it into ``tmp_path``; nothing here
writes into the package directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app.decks.models import (
    MAX_NOTES_CHARS,
    MIN_ALIASES,
    MIN_SLIDES,
    PROMPT_NOTES_CHARS,
    Deck,
    Slide,
)
from app.decks.repository import DeckRepository, DeckSummary
from app.errors import DeckError
from pydantic import ValidationError

SHIPPED_DECK_ID = "anatomy_of_a_voice_agent"
"""Id of the deck shipped inside the package (PRD §4)."""

SHIPPED_SLIDE_COUNT = 6
"""Slides the shipped deck must have (PRD §4 table)."""

SHIPPED_TITLES = [
    "Anatomy of a Voice Agent",
    "The Latency Budget",
    "Hearing: VAD and Turn-Taking",
    "Barge-in: Interrupting Gracefully",
    "Thinking: Tool Calling and Intent Routing",
    "Trade-offs and What's Next",
]
"""Slide titles from the PRD §4 table, in order."""

REQUIRED_ALIASES = {
    1: ("intro", "start", "overview"),
    2: ("latency", "speed", "budget"),
    3: ("vad", "turn taking", "endpointing"),
    4: ("interrupt", "barge in", "talk over"),
    5: ("tools", "tool calling", "routing"),
    6: ("trade offs", "speech to speech", "cost"),
}
"""Routing phrases PRD §4 assigns to each slide. The deck may add more, but
dropping one of these breaks a documented navigation path."""

MIN_NOTES_CHARS = 500
"""Shortest notes accepted on a shipped slide. Below this the agent has nothing
specific to say and falls back to paraphrasing the bullets aloud."""

MAX_BULLET_CHARS = 60
"""Longest on-screen bullet. Bullets are read at a glance; the substance lives
in the notes."""

UNSPEAKABLE_MARKUP = ("~", "*", "`", "#", "http")
"""Fragments no shipped slide may print.

The deck is the strongest example the model has of how to write, because it is
the only prose in the prompt that is not an instruction. ``prompts/presenter.md``
tells it to say "about three hundred milliseconds" rather than "~300ms" and to
avoid markdown and URLs entirely -- and a bullet reading "~30 ms" teaches the
opposite on every single turn. The chunker strips markdown before synthesis
(TR-044), but that is a net under the model, not a licence to write markup here.
"""


def _slide(index: int, **overrides: Any) -> dict[str, Any]:
    """Return a minimal valid slide payload, with any field overridden."""
    slide: dict[str, Any] = {
        "index": index,
        "title": f"Slide {index}",
        "bullets": [f"Bullet {index}"],
        "notes": f"Notes for slide {index}.",
        "aliases": [f"alias {index} one", f"alias {index} two"],
    }
    slide.update(overrides)
    return slide


def _deck_payload(slide_count: int = MIN_SLIDES, **overrides: Any) -> dict[str, Any]:
    """Return a minimal valid deck payload holding ``slide_count`` slides."""
    payload: dict[str, Any] = {
        "id": "test_deck",
        "title": "Test Deck",
        "voice": None,
        "slides": [_slide(index) for index in range(1, slide_count + 1)],
    }
    payload.update(overrides)
    return payload


def _write(directory: Path, name: str, payload: Any) -> Path:
    """Write ``payload`` as JSON into ``directory`` and return the path."""
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def shipped_deck() -> Deck:
    """Return the packaged deck, loaded the way the application loads it."""
    return DeckRepository().get(SHIPPED_DECK_ID)


# --- The shipped deck --------------------------------------------------------


def test_the_shipped_deck_loads_with_six_slides_and_unique_aliases(shipped_deck: Deck) -> None:
    """TC-BE-001: the default deck JSON validates with 6 slides, 1..6, no shared alias."""
    assert shipped_deck.id == SHIPPED_DECK_ID
    assert shipped_deck.title == "Anatomy of a Voice Agent"
    assert len(shipped_deck.slides) == SHIPPED_SLIDE_COUNT

    assert [slide.index for slide in shipped_deck.slides] == [1, 2, 3, 4, 5, 6]
    assert [slide.title for slide in shipped_deck.slides] == SHIPPED_TITLES

    aliases = [alias for slide in shipped_deck.slides for alias in slide.aliases]
    assert len(aliases) == len(set(aliases))
    # The model lower-cases on the way in; assert the file already agrees, so a
    # reviewer diffing the JSON sees the strings the router matches against.
    assert aliases == [alias.lower() for alias in aliases]


def test_the_shipped_deck_meets_the_authoring_contract(shipped_deck: Deck) -> None:
    """TC-BE-140: every shipped slide keeps speakable notes, glanceable bullets, PRD aliases."""
    for slide in shipped_deck.slides:
        assert MIN_NOTES_CHARS <= len(slide.notes) <= MAX_NOTES_CHARS, slide.title
        # Notes are spoken prose, not a second copy of the bullets: several
        # sentences, ending in a full stop.
        assert slide.notes.endswith("."), slide.title
        assert slide.notes.count(". ") >= 2, slide.title

        assert 1 <= len(slide.bullets) <= 6, slide.title
        for bullet in slide.bullets:
            assert len(bullet) <= MAX_BULLET_CHARS, bullet

        for text in (slide.notes, *slide.bullets):
            for fragment in UNSPEAKABLE_MARKUP:
                assert fragment not in text.lower(), f"slide {slide.index}: {text}"

        assert len(slide.aliases) >= MIN_ALIASES, slide.title
        missing = set(REQUIRED_ALIASES[slide.index]) - set(slide.aliases)
        assert not missing, f"slide {slide.index} dropped PRD aliases {missing}"


def test_every_authored_note_reaches_the_prompt_whole(shipped_deck: Deck) -> None:
    """TC-BE-140a: no shipped slide's notes are truncated on the way into the prompt.

    The prompt now carries the notes of one slide rather than all six
    (:func:`app.pipeline.prompt.render_deck_json`), so a tight budget saves
    almost nothing and costs a great deal. At 600 characters every slide in this
    deck lost its second half -- which is where the notes stop restating the
    bullets and start explaining them, the part the agent is worth listening to
    for. The truncation guard stays for a deck authored up to the limit; the
    shipped deck must simply never reach it.
    """
    for slide in shipped_deck.slides:
        assert slide.prompt_notes == slide.notes, (
            f"slide {slide.index} loses {len(slide.notes) - PROMPT_NOTES_CHARS} characters "
            f"of notes on the way into the prompt"
        )
    assert PROMPT_NOTES_CHARS < MAX_NOTES_CHARS, (
        "the prompt budget must stay under the authoring limit, or truncation is dead code"
    )


def test_slide_lookup_is_one_based_and_last_index_names_the_final_slide(
    shipped_deck: Deck,
) -> None:
    """TC-BE-141: `slide()` indexes from 1 and raises outside the deck; `last_index` is the end."""
    assert shipped_deck.slide(1) is shipped_deck.slides[0]
    assert shipped_deck.last_index == SHIPPED_SLIDE_COUNT
    assert shipped_deck.slide(shipped_deck.last_index) is shipped_deck.slides[-1]
    assert shipped_deck.slides[-1].index == shipped_deck.last_index

    for out_of_range in (0, -1, SHIPPED_SLIDE_COUNT + 1):
        # The message names the deck, so a stray tool call is traceable in the log.
        with pytest.raises(IndexError, match=SHIPPED_DECK_ID):
            shipped_deck.slide(out_of_range)


def test_prompt_notes_truncate_only_once_past_the_prompt_budget() -> None:
    """TC-BE-142: notes at or under the budget pass through unchanged; longer notes are elided."""
    under = Slide.model_validate(_slide(1, notes="a" * (PROMPT_NOTES_CHARS - 1)))
    assert under.prompt_notes == under.notes

    exact = Slide.model_validate(_slide(1, notes="a" * PROMPT_NOTES_CHARS))
    assert exact.prompt_notes == exact.notes
    assert len(exact.prompt_notes) == PROMPT_NOTES_CHARS

    over = Slide.model_validate(_slide(1, notes="a" * (PROMPT_NOTES_CHARS + 1)))
    assert over.prompt_notes != over.notes
    assert over.prompt_notes.endswith("…")
    assert len(over.prompt_notes) == PROMPT_NOTES_CHARS
    assert over.prompt_notes.startswith("a" * (PROMPT_NOTES_CHARS - 1))


# --- Loading errors, reported per file ---------------------------------------


def test_a_duplicate_alias_across_slides_is_a_deck_error_naming_both_slides(
    tmp_path: Path,
) -> None:
    """TC-BE-002: a deck sharing an alias between slides fails, and the message names both."""
    payload = _deck_payload()
    payload["slides"][0]["aliases"] = ["shared alias", "alias one only"]
    payload["slides"][2]["aliases"] = ["shared alias", "alias three only"]
    _write(tmp_path, "duplicate_aliases.json", payload)

    with pytest.raises(DeckError) as caught:
        DeckRepository(tmp_path)

    message = str(caught.value)
    assert "duplicate_aliases.json" in message
    assert "shared alias" in message
    assert "slide 1" in message
    assert "slide 3" in message


def test_a_deck_with_four_slides_is_rejected_by_the_minimum(tmp_path: Path) -> None:
    """TC-BE-003: a 4-slide deck fails validation because the declared minimum is 5."""
    _write(tmp_path, "too_short.json", _deck_payload(slide_count=4))

    with pytest.raises(DeckError) as caught:
        DeckRepository(tmp_path)

    message = str(caught.value)
    assert "too_short.json" in message
    assert "slides" in message
    assert str(MIN_SLIDES) in message


def _broken_deck(**slide_overrides: Any) -> str:
    """Return a deck as JSON text whose second slide carries ``slide_overrides``."""
    payload = _deck_payload()
    payload["slides"][1].update(slide_overrides)
    return json.dumps(payload)


@pytest.mark.parametrize(
    ("name", "content", "expected_fragment"),
    [
        ("truncated.json", "{ this is not json", "not valid JSON"),
        ("empty.json", "", "not valid JSON"),
        ("wrong_shape.json", '["a list, not an object"]', "failed validation"),
        ("blank_bullet.json", _broken_deck(bullets=["  "]), "must not be blank"),
        ("repeat_alias.json", _broken_deck(aliases=["same", "SAME"]), "unique within a slide"),
        ("bad_index.json", _broken_deck(index=4), "slide indices must be"),
        ("unpacked.json", None, "could not be read"),
    ],
    ids=[
        "truncated-object",
        "empty-file",
        "wrong-top-level-type",
        "blank-bullet",
        "alias-repeated-in-one-slide",
        "non-contiguous-index",
        "directory-not-a-file",
    ],
)
def test_a_malformed_deck_file_raises_a_deck_error_naming_the_file(
    tmp_path: Path, name: str, content: str | None, expected_fragment: str
) -> None:
    """TC-BE-143: an unreadable, unparsable, or invalid deck file names itself in the DeckError."""
    # `None` stands for the operator who unpacked a deck into a *directory*
    # ending in .json: the glob still matches it, and reading it must fail
    # with the same shaped message as any other broken deck.
    if content is None:
        (tmp_path / name).mkdir()
    else:
        (tmp_path / name).write_text(content, encoding="utf-8")

    with pytest.raises(DeckError) as caught:
        DeckRepository(tmp_path)

    message = str(caught.value)
    assert name in message
    assert expected_fragment in message
    # Operator feedback, not a pydantic stack trace: no library internals leak.
    assert "Traceback" not in message
    assert "pydantic" not in message.lower()


def test_a_validation_failure_names_the_field_path_and_caps_the_problem_list(
    tmp_path: Path,
) -> None:
    """TC-BE-144: the DeckError quotes the broken field path and elides a long tail."""
    payload = _deck_payload()
    payload["slides"][1]["notes"] = ""
    _write(tmp_path, "bad.json", payload)

    with pytest.raises(DeckError) as caught:
        DeckRepository(tmp_path)
    assert "slides.1.notes" in str(caught.value)

    # Every slide broken at once: the message must stay short rather than
    # printing one line per failed validator.
    flooded = _deck_payload()
    for slide in flooded["slides"]:
        slide["bullets"] = []
        slide["aliases"] = ["only one"]
    _write(tmp_path, "bad.json", flooded)

    with pytest.raises(DeckError) as caught:
        DeckRepository(tmp_path)
    assert "more)" in str(caught.value)


def test_two_files_declaring_the_same_deck_id_name_both_files(tmp_path: Path) -> None:
    """TC-BE-145: a deck id claimed by two files fails at load, naming the pair."""
    _write(tmp_path, "alpha.json", _deck_payload())
    _write(tmp_path, "beta.json", _deck_payload())

    with pytest.raises(DeckError) as caught:
        DeckRepository(tmp_path)

    message = str(caught.value)
    assert "alpha.json" in message
    assert "beta.json" in message
    assert "test_deck" in message


# --- Lookup, listing, and runtime registration -------------------------------


def test_getting_an_unknown_deck_id_raises_and_lists_what_is_known(tmp_path: Path) -> None:
    """TC-BE-146: `get` on an id the repository does not hold raises DeckError."""
    empty = DeckRepository(tmp_path)
    with pytest.raises(DeckError, match="none"):
        empty.get("anything")

    _write(tmp_path, "deck.json", _deck_payload())
    loaded = DeckRepository(tmp_path)

    with pytest.raises(DeckError) as caught:
        loaded.get("no_such_deck")

    message = str(caught.value)
    assert "no_such_deck" in message
    # The usual cause is a typo, so the message has to show the real ids.
    assert "test_deck" in message


def test_listing_summarises_each_deck_and_length_agrees(tmp_path: Path) -> None:
    """TC-BE-147: `list_decks` gives id/title/slide_count ordered by id; `__len__` matches."""
    empty = DeckRepository(tmp_path)
    assert len(empty) == 0
    assert empty.list_decks() == []

    # File names deliberately sort opposite to the deck ids, proving the
    # listing orders by id rather than by directory order.
    _write(tmp_path, "a.json", _deck_payload(6, id="zulu", title="Zulu"))
    _write(tmp_path, "z.json", _deck_payload(5, id="alpha", title="Alpha"))
    repository = DeckRepository(tmp_path)

    assert len(repository) == 2
    assert repository.list_decks() == [
        DeckSummary(id="alpha", title="Alpha", slide_count=5),
        DeckSummary(id="zulu", title="Zulu", slide_count=6),
    ]


def test_registering_a_runtime_deck_adds_it_and_a_repeated_id_is_refused(tmp_path: Path) -> None:
    """TC-BE-148: `register` exposes a generated deck; registering the same id twice raises."""
    repository = DeckRepository(tmp_path)
    generated = Deck.model_validate(_deck_payload(id="generated-1234", title="Generated"))

    repository.register(generated)

    assert len(repository) == 1
    assert repository.get("generated-1234") is generated
    assert DeckSummary(id="generated-1234", title="Generated", slide_count=5) in (
        repository.list_decks()
    )

    with pytest.raises(DeckError, match="already registered"):
        repository.register(generated)


def test_decks_are_read_once_at_construction_and_never_re_read(tmp_path: Path) -> None:
    """TC-BE-149: once loaded, deleting the source file does not affect later lookups."""
    path = _write(tmp_path, "deck.json", _deck_payload())
    repository = DeckRepository(tmp_path)

    path.unlink()

    assert len(repository) == 1
    assert repository.get("test_deck").title == "Test Deck"
    assert repository.list_decks() == [
        DeckSummary(id="test_deck", title="Test Deck", slide_count=5)
    ]


def test_a_figure_must_show_every_bullet_exactly_once() -> None:
    """TC-BE-330: PRD F1 -- the screen and the agent's view of a slide are the same list.

    The model is given a slide's bullets and nothing else. A bullet the figure
    omits is therefore something the agent can assert that nobody can see, and a
    bullet shown twice is a point the room hears once and reads twice. Neither
    is visible without this check, and both make a slide lie about itself.
    """
    base = {
        "index": 1,
        "title": "A slide",
        "bullets": ["first", "second"],
        "notes": "Notes.",
        "aliases": ["a slide", "the slide"],
    }

    with pytest.raises(ValidationError, match="every bullet exactly once"):
        Slide(**base, figure={"kind": "metrics", "items": [{"bullets": [0], "heading": "one"}]})

    with pytest.raises(ValidationError, match="every bullet exactly once"):
        Slide(
            **base,
            figure={
                "kind": "metrics",
                "items": [{"bullets": [0, 0, 1], "heading": "one"}],
            },
        )

    with pytest.raises(ValidationError, match="every bullet exactly once"):
        Slide(**base, figure={"kind": "metrics", "items": [{"bullets": [0, 5], "heading": "one"}]})


def test_a_figure_may_group_bullets_across_its_items() -> None:
    """TC-BE-331: PRD F1 -- a column or a step presents several points at once."""
    slide = Slide(
        index=1,
        title="A slide",
        bullets=["first", "second", "third"],
        notes="Notes.",
        aliases=["a slide", "the slide"],
        figure={
            "kind": "split",
            "items": [
                {"bullets": [0], "heading": "Left"},
                {"bullets": [1, 2], "heading": "Right"},
            ],
        },
    )

    assert slide.figure is not None
    assert [item.heading for item in slide.figure.items] == ["Left", "Right"]


def test_a_slide_without_a_figure_is_still_valid() -> None:
    """TC-BE-332: PRD F1 -- the arrangement is optional; a plain list is a slide."""
    slide = Slide(
        index=1,
        title="A slide",
        bullets=["first"],
        notes="Notes.",
        aliases=["a slide", "the slide"],
    )

    assert slide.figure is None


def test_every_slide_in_the_shipped_deck_declares_an_arrangement() -> None:
    """TC-BE-333: PRD F1 -- the deck this product presents is not a wall of bullets."""
    deck = DeckRepository().get("anatomy_of_a_voice_agent")

    for slide in deck.slides:
        assert slide.figure is not None, f"slide {slide.index} has no arrangement"
        assert slide.figure.items, f"slide {slide.index} has an empty arrangement"
