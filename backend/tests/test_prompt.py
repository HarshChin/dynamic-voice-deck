"""Tests for the system prompt builder (TR-070, TR-071).

The builder's contract is that it always produces a prompt: deck copy is
arbitrary text and the template is hand-edited product copy, so these tests lean
on the awkward inputs rather than the happy path.

``load_template`` memoises file reads process-wide, so the autouse fixture
clears it around every test; otherwise a test that writes a template into
``tmp_path`` could be served a neighbour's cached text.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.decks.models import PROMPT_NOTES_CHARS, Deck, Slide
from app.errors import ConfigError
from app.pipeline.prompt import (
    DEFAULT_TEMPLATE_PATH,
    PromptBuilder,
    load_template,
    render_deck_json,
)
from app.protocol import SessionMode
from app.providers.base import Message

SLIDE_COUNT = 5

PLACEHOLDERS = ("deck_json", "current_slide", "presentation_cursor", "mode", "slide_count")


@pytest.fixture(autouse=True)
def _clear_template_cache() -> Iterator[None]:
    load_template.cache_clear()
    yield
    load_template.cache_clear()


def make_slide(
    index: int,
    *,
    title: str | None = None,
    bullets: list[str] | None = None,
    notes: str | None = None,
    aliases: list[str] | None = None,
) -> Slide:
    """Build a valid slide, overriding only the field under test."""
    return Slide(
        index=index,
        title=title if title is not None else f"Slide {index} Heading",
        bullets=bullets if bullets is not None else [f"Point {index}a", f"Point {index}b"],
        notes=notes if notes is not None else f"Ground truth for slide {index}.",
        aliases=aliases if aliases is not None else [f"alias{index}one", f"alias{index}two"],
    )


def make_deck(*, title: str = "Anatomy of a Voice Agent", first: Slide | None = None) -> Deck:
    """Build a valid deck whose first slide may be replaced by the caller."""
    slides = [make_slide(i) for i in range(1, SLIDE_COUNT + 1)]
    if first is not None:
        slides[0] = first
    return Deck(id="test-deck", title=title, slides=slides)


def system_prompt(deck: Deck, snapshot: dict[str, object] | None = None) -> str:
    """Render just the system message, which is what most of these tests inspect."""
    return PromptBuilder().build(deck, snapshot or {}, [])[0].content


def test_braces_in_deck_copy_render_literally() -> None:
    """TC-BE-004: deck copy containing braces renders without raising.

    Deck text is substituted into the template, never parsed as one, so every
    brace must survive byte for byte.
    """
    deck = make_deck(
        title="The {curly} Deck",
        first=make_slide(
            1,
            title="Formatting {edge} cases",
            bullets=["A {placeholder} bullet", "Braces {} on their own"],
            notes="Latency is {latency_ms} ms against a budget of {budget}.",
            aliases=["braces", "curly"],
        ),
    )

    content = system_prompt(deck)

    assert "{curly}" in content
    assert "{edge}" in content
    assert "{placeholder}" in content
    assert "{latency_ms}" in content
    assert "{budget}" in content
    assert "Braces {} on their own" in content


def test_long_notes_are_truncated_to_six_hundred_characters() -> None:
    """TC-BE-005: notes of 1,000 characters reach the prompt truncated."""
    head = "S" * (PROMPT_NOTES_CHARS - 1)
    tail_marker = "TAILMARKER"
    notes = head + tail_marker + "E" * (1_000 - len(head) - len(tail_marker))
    assert len(notes) == 1_000
    deck = make_deck(first=make_slide(1, notes=notes, aliases=["long", "notes"]))

    content = system_prompt(deck)

    assert deck.slides[0].prompt_notes == head + "…"
    assert len(deck.slides[0].prompt_notes) == PROMPT_NOTES_CHARS
    assert head + "…" in content
    assert tail_marker not in content
    assert notes not in content


@pytest.mark.parametrize("mode", [SessionMode.PRESENT, "present"])
def test_the_snapshot_position_appears_in_the_prompt(mode: SessionMode | str) -> None:
    """TC-BE-006: current_slide, cursor, and mode are rendered, and none of the
    template's placeholders is left unfilled.

    Parametrised over the enum and its bare string because ``snapshot()`` may
    hand back either; a ``StrEnum`` must not render as ``SessionMode.PRESENT``.
    """
    snapshot: dict[str, object] = {
        "current_slide": 3,
        "presentation_cursor": 2,
        "mode": mode,
    }

    content = system_prompt(make_deck(), snapshot)

    assert "current_slide: 3" in content
    assert "presentation_cursor: 2" in content
    assert "mode: present" in content
    assert "SessionMode" not in content

    template = DEFAULT_TEMPLATE_PATH.read_text(encoding="utf-8")
    for name in PLACEHOLDERS:
        assert "{" + name + "}" in template, "presenter.md dropped a placeholder"
        assert "{" + name + "}" not in content, "a placeholder survived rendering"


def test_the_prompt_embeds_every_slides_title_and_bullets() -> None:
    """TC-BE-007: every slide's title and bullets reach the model as compact JSON."""
    deck = make_deck()

    content = system_prompt(deck)
    payload = json.loads(render_deck_json(deck))

    assert deck.title in content
    for slide in deck.slides:
        assert slide.title in content
        for bullet in slide.bullets:
            assert bullet in content
    assert f"has {SLIDE_COUNT} slides" in content or f"({SLIDE_COUNT} slides)" in content

    assert set(payload) == {"title", "slides"}
    # Compact separators: every colon and comma costs tokens on every turn.
    assert '"index":1' in render_deck_json(deck)


def test_aliases_are_not_sent_to_the_model() -> None:
    """TC-BE-160: aliases stay server-side, where the keyword fallback uses them.

    They cost roughly 300 input tokens per request against a free-tier budget of
    8,000 per minute, and the model routes from titles and bullets.
    """
    deck = make_deck()

    payload = json.loads(render_deck_json(deck))

    assert all("aliases" not in slide for slide in payload["slides"])


def test_only_the_current_slides_notes_are_embedded() -> None:
    """TC-BE-161: notes ride along for the slide on screen, not for all of them.

    Sending every slide's notes cost about 1,570 tokens per request and capped
    the agent at two requests a minute on the free tier, which is less than one
    exchange because a turn that calls a tool needs two requests.
    """
    deck = make_deck()

    focused = json.loads(render_deck_json(deck, current_slide=2))
    with_notes = [slide["index"] for slide in focused["slides"] if "notes" in slide]

    assert with_notes == [2]
    assert deck.slides[1].notes in json.dumps(focused, ensure_ascii=False)

    # With no cursor -- tests, or any caller without one -- every slide keeps its
    # notes, so the focused behaviour is opt-in rather than a silent loss.
    unfocused = json.loads(render_deck_json(deck))
    assert all("notes" in slide for slide in unfocused["slides"])


def test_the_system_message_leads_and_history_follows_in_order() -> None:
    """TC-BE-008: build returns the system message first, then history untouched."""
    deck = make_deck()
    history = [
        Message(role="user", content="how do you handle interruptions?"),
        Message(role="assistant", content="Two layers, actually."),
        Message(role="user", content="go on"),
    ]
    original = [message.model_copy(deep=True) for message in history]

    messages = PromptBuilder().build(deck, {}, history)

    assert messages[0].role == "system"
    assert messages[0].content
    assert messages[1:] == original
    assert history == original, "the caller's history must not be mutated"
    assert messages is not history


def test_the_template_loads_from_the_package_and_renders_anything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-BE-009: template loading fails loudly only when the file is missing;
    rendering itself never raises.

    Four ways this could break are covered together: resolving the default
    template from a foreign working directory, an unknown placeholder, an
    unbalanced brace left behind by a hand edit, and a template that is gone.
    """
    deck = make_deck()

    # The default template resolves from the package, not the process's cwd.
    monkeypatch.chdir(tmp_path)
    assert deck.title in system_prompt(deck)

    # An unknown placeholder survives instead of raising KeyError.
    unknown = tmp_path / "unknown.md"
    unknown.write_text("Tone: {tone}. Slides: {slide_count}.", encoding="utf-8")
    content = PromptBuilder(unknown).build(deck, {}, [])[0].content
    assert content == f"Tone: {{tone}}. Slides: {SLIDE_COUNT}."

    # An unbalanced brace falls back to literal replacement instead of ValueError.
    malformed = tmp_path / "malformed.md"
    malformed.write_text("50% { of it. Slides: {slide_count}. Tone: {tone}.", encoding="utf-8")
    content = PromptBuilder(malformed).build(deck, {}, [])[0].content
    assert content == f"50% {{ of it. Slides: {SLIDE_COUNT}. Tone: {{tone}}."

    # A missing template is a deployment fault, and fails at construction.
    with pytest.raises(ConfigError, match="could not be read"):
        PromptBuilder(tmp_path / "nope.md")
