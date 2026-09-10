"""Tests for :class:`app.pipeline.slides.SlideController` (TR-060 .. TR-064).

Two decks are built here rather than loaded from ``app/decks/``. ``demo_deck``
mirrors the shape of the shipped deck (six slides, a four-bullet opener) for the
tool-validation cases, and ``scoring_deck`` uses deliberately disjoint vocabulary
so every fallback score is arithmetic the reader can check by hand. Scoring
against real deck copy would make the threshold and margin assertions hostage to
someone else's wording.

Expected ``scoring_deck`` scores, from the weights in TR-062
(alias phrase 3, title word 2, bullet word 1):

    "charlie sierra"                          S2 = 4                (threshold)
    "charlie delta"                           S2 = 3                (below)
    "charlie sierra delta echo tango"         S2 = 5, S3 = 4        (margin 1)
    "charlie sierra delta point echo tango"   S2 = 6, S3 = 4        (margin 2)
    "we compare charlie sierra with echo tango"  S2 = 4, S3 = 4     (tie)
"""

from __future__ import annotations

import pytest
from app.decks.models import Deck, Slide
from app.pipeline.slides import (
    MAX_REASON_CHARS,
    MIN_FALLBACK_SCORE,
    SlideAction,
    SlideController,
)
from app.pipeline.tools import GO_TO_SLIDE, HIGHLIGHT_BULLET
from app.protocol import SessionMode, ToolSource

# Answers whose scores are worked out in the module docstring.
THRESHOLD_ANSWER = "charlie sierra"
BELOW_THRESHOLD_ANSWER = "charlie delta"
MARGIN_ONE_ANSWER = "charlie sierra delta echo tango"
MARGIN_TWO_ANSWER = "charlie sierra delta point echo tango"
TIE_ANSWER = "We compare charlie sierra with echo tango."

LATENCY_ANSWER = (
    "The latency budget is about nine hundred milliseconds, "
    "from speech to text all the way through playback."
)


def _slide(index: int, title: str, bullets: list[str], aliases: list[str]) -> Slide:
    """Build a slide with generated notes, which routing never reads."""
    return Slide(
        index=index,
        title=title,
        bullets=bullets,
        notes=f"Speaker notes for {title}.",
        aliases=aliases,
    )


@pytest.fixture
def demo_deck() -> Deck:
    """A six-slide deck shaped like the shipped one; slide 1 has four bullets."""
    return Deck(
        id="demo",
        title="Dynamic Voice Deck",
        slides=[
            _slide(
                1,
                "Voice First Presenting",
                ["Opening promise", "Live demo", "Ask anything", "No clicking"],
                ["intro", "opening"],
            ),
            _slide(
                2,
                "Latency Budget",
                ["Speech to text", "Model streaming", "Audio playback"],
                ["latency", "milliseconds"],
            ),
            _slide(
                3,
                "Endpointing And Turn Taking",
                ["Silero voice detection", "Redemption frames"],
                ["endpointing", "turn taking"],
            ),
            _slide(
                4,
                "Barge In",
                ["Cancel the task", "Truncate history"],
                ["barge in", "interruptions"],
            ),
            _slide(
                5,
                "Slide Routing",
                ["Tool calls", "Alias scoring"],
                ["routing", "navigation"],
            ),
            _slide(
                6,
                "Cost Per Session",
                ["Cents per minute", "Local synthesis"],
                ["cost", "pricing"],
            ),
        ],
    )


@pytest.fixture
def scoring_deck() -> Deck:
    """A five-slide deck whose vocabulary is disjoint slide to slide."""
    return Deck(
        id="scoring",
        title="Scoring Fixture",
        slides=[
            _slide(1, "Alpha", ["Bravo"], ["alpha overview", "aa"]),
            _slide(2, "Charlie Sierra", ["Delta point"], ["charlie sierra channel", "cc"]),
            _slide(3, "Echo Tango", ["Foxtrot"], ["echo tango channel", "ee"]),
            _slide(4, "Golf", ["Hotel"], ["quick brown fox", "lazy dog"]),
            _slide(5, "India Metrics", ["Numbers"], ["time to first token", "first token latency"]),
        ],
    )


@pytest.fixture
def controller(demo_deck: Deck) -> SlideController:
    """A controller on the demo deck, on slide 1, in question-answer mode."""
    return SlideController(demo_deck)


# --------------------------------------------------------------------------- #
# Tool validation (TR-061)
# --------------------------------------------------------------------------- #


def test_go_to_slide_moves_the_deck(controller: SlideController) -> None:
    """TC-BE-030."""
    action = controller.apply_tool(GO_TO_SLIDE, {"slide_index": 4, "reason": "r"})

    assert action == SlideAction(index=4, highlight=None, reason="r", source=ToolSource.LLM)
    assert controller.current_slide == 4
    assert controller.last_error is None


def test_go_to_slide_beyond_the_deck_is_refused(controller: SlideController) -> None:
    """TC-BE-031."""
    action = controller.apply_tool(GO_TO_SLIDE, {"slide_index": 9, "reason": "r"})

    assert action is None
    assert controller.current_slide == 1
    assert controller.last_error is not None
    # The message must tell the model the real range, or it cannot self-correct.
    assert "1 to 6" in controller.last_error


def test_go_to_slide_below_the_deck_is_refused(controller: SlideController) -> None:
    """TC-BE-031a."""
    assert controller.apply_tool(GO_TO_SLIDE, {"slide_index": 0, "reason": "r"}) is None
    assert controller.current_slide == 1
    assert controller.last_error is not None


def test_go_to_slide_accepts_the_last_slide(controller: SlideController) -> None:
    """TC-BE-031b."""
    action = controller.apply_tool(GO_TO_SLIDE, {"slide_index": 6, "reason": "end"})

    assert action is not None
    assert action.index == 6
    assert controller.current_slide == 6


def test_go_to_slide_coerces_a_numeric_string(controller: SlideController) -> None:
    """TC-BE-031c."""
    action = controller.apply_tool(GO_TO_SLIDE, {"slide_index": "3", "reason": "r"})

    assert action is not None
    assert action.index == 3
    assert controller.current_slide == 3


@pytest.mark.parametrize("raw", [None, True, 2.5, "third", "", [3]])
def test_go_to_slide_refuses_a_non_integer_index(controller: SlideController, raw: object) -> None:
    """TC-BE-031d."""
    assert controller.apply_tool(GO_TO_SLIDE, {"slide_index": raw, "reason": "r"}) is None
    assert controller.current_slide == 1
    assert controller.last_error is not None


def test_go_to_slide_without_a_reason_supplies_one(controller: SlideController) -> None:
    """TC-BE-031e."""
    action = controller.apply_tool(GO_TO_SLIDE, {"slide_index": 2})

    assert action is not None
    assert action.reason.strip()


def test_go_to_slide_truncates_a_rambling_reason(controller: SlideController) -> None:
    """TC-BE-031f."""
    action = controller.apply_tool(GO_TO_SLIDE, {"slide_index": 2, "reason": "x" * 500})

    assert action is not None
    assert len(action.reason) == MAX_REASON_CHARS


def test_highlight_bullet_beyond_the_slide_is_refused(controller: SlideController) -> None:
    """TC-BE-032."""
    action = controller.apply_tool(HIGHLIGHT_BULLET, {"bullet_index": 7})

    assert action is None
    assert controller.last_error is not None
    assert "0 to 3" in controller.last_error


def test_highlight_bullet_within_the_slide_is_applied(controller: SlideController) -> None:
    """TC-BE-032a."""
    action = controller.apply_tool(HIGHLIGHT_BULLET, {"bullet_index": 0})

    assert action is not None
    assert action.index == 1
    assert action.highlight == 0
    assert action.source is ToolSource.LLM
    # Highlighting annotates the current slide; it must never navigate.
    assert controller.current_slide == 1


def test_highlight_bullet_is_validated_against_the_current_slide(
    controller: SlideController,
) -> None:
    """TC-BE-032b."""
    controller.apply_tool(GO_TO_SLIDE, {"slide_index": 4, "reason": "r"})

    # Slide 4 has two bullets; index 3 was valid on slide 1 but is not here.
    assert controller.apply_tool(HIGHLIGHT_BULLET, {"bullet_index": 3}) is None
    assert controller.apply_tool(HIGHLIGHT_BULLET, {"bullet_index": 1}) is not None


def test_highlight_bullet_refuses_a_negative_index(controller: SlideController) -> None:
    """TC-BE-032c."""
    assert controller.apply_tool(HIGHLIGHT_BULLET, {"bullet_index": -1}) is None
    assert controller.last_error is not None


@pytest.mark.parametrize("raw", [None, True, 1.5, "second", {}])
def test_highlight_bullet_refuses_a_non_integer_index(
    controller: SlideController, raw: object
) -> None:
    """TC-BE-032d."""
    assert controller.apply_tool(HIGHLIGHT_BULLET, {"bullet_index": raw}) is None
    assert controller.last_error is not None


def test_unknown_tool_is_refused_without_raising(controller: SlideController) -> None:
    """TC-BE-033."""
    action = controller.apply_tool("delete_deck", {"anything": 1})

    assert action is None
    assert controller.current_slide == 1
    assert controller.last_error is not None
    assert "delete_deck" in controller.last_error


def test_a_valid_call_clears_the_previous_error(controller: SlideController) -> None:
    """TC-BE-033a."""
    controller.apply_tool(GO_TO_SLIDE, {"slide_index": 99, "reason": "r"})
    assert controller.last_error is not None

    controller.apply_tool(GO_TO_SLIDE, {"slide_index": 2, "reason": "r"})

    assert controller.last_error is None


def test_two_navigations_in_one_turn_both_apply(controller: SlideController) -> None:
    """TC-BE-039."""
    actions = [
        controller.apply_tool(GO_TO_SLIDE, {"slide_index": 3, "reason": "first"}),
        controller.apply_tool(GO_TO_SLIDE, {"slide_index": 5, "reason": "second"}),
    ]

    assert [action.index for action in actions if action is not None] == [3, 5]
    assert controller.current_slide == 5


# --------------------------------------------------------------------------- #
# Keyword fallback (TR-062)
# --------------------------------------------------------------------------- #


def test_fallback_routes_an_untooled_answer(demo_deck: Deck) -> None:
    """TC-BE-034."""
    controller = SlideController(demo_deck, current_slide=1)

    action = controller.keyword_fallback(LATENCY_ANSWER)

    assert action is not None
    assert action.index == 2
    assert action.source is ToolSource.FALLBACK
    assert action.reason.startswith("Keyword match:")
    assert controller.current_slide == 2


def test_fallback_declines_a_tie(scoring_deck: Deck) -> None:
    """TC-BE-035."""
    controller = SlideController(scoring_deck, current_slide=1)

    assert controller.keyword_fallback(TIE_ANSWER) is None
    assert controller.current_slide == 1


def test_fallback_declines_a_one_point_margin(scoring_deck: Deck) -> None:
    """TC-BE-035a."""
    controller = SlideController(scoring_deck, current_slide=1)

    assert controller.keyword_fallback(MARGIN_ONE_ANSWER) is None
    assert controller.current_slide == 1


def test_fallback_accepts_a_two_point_margin(scoring_deck: Deck) -> None:
    """TC-BE-035b."""
    controller = SlideController(scoring_deck, current_slide=1)

    action = controller.keyword_fallback(MARGIN_TWO_ANSWER)

    assert action is not None
    assert action.index == 2


def test_fallback_counts_the_current_slide_as_a_rival(scoring_deck: Deck) -> None:
    """TC-BE-035c."""
    # Slide 3 scores 4 against slide 2's 5. Starting on slide 3 must not make
    # that near-miss into a jump: the current slide stays in the ranking.
    controller = SlideController(scoring_deck, current_slide=3)

    assert controller.keyword_fallback(MARGIN_ONE_ANSWER) is None
    assert controller.current_slide == 3


def test_fallback_declines_the_current_slide(demo_deck: Deck) -> None:
    """TC-BE-036."""
    controller = SlideController(demo_deck, current_slide=2)

    assert controller.keyword_fallback(LATENCY_ANSWER) is None
    assert controller.current_slide == 2


def test_fallback_declines_the_current_slide_even_when_it_wins_outright(
    scoring_deck: Deck,
) -> None:
    """TC-BE-036a."""
    # The same text moves the deck from slide 1 (TC-BE-035b) but must not
    # re-fire while the deck already sits on the winner.
    controller = SlideController(scoring_deck, current_slide=2)

    assert controller.keyword_fallback(MARGIN_TWO_ANSWER) is None
    assert controller.current_slide == 2


def test_fallback_meets_the_threshold_exactly(scoring_deck: Deck) -> None:
    """TC-BE-036b."""
    controller = SlideController(scoring_deck, current_slide=1)

    action = controller.keyword_fallback(THRESHOLD_ANSWER)

    assert action is not None
    assert action.index == 2


def test_fallback_declines_below_the_threshold(scoring_deck: Deck) -> None:
    """TC-BE-036c."""
    controller = SlideController(scoring_deck, current_slide=1)

    # Three points, one short, and unopposed -- only the threshold can refuse it.
    assert MIN_FALLBACK_SCORE == 4
    assert controller.keyword_fallback(BELOW_THRESHOLD_ANSWER) is None


def test_fallback_matches_aliases_as_phrases_not_loose_words(scoring_deck: Deck) -> None:
    """TC-BE-036d."""
    controller = SlideController(scoring_deck, current_slide=1)
    ordered = controller.keyword_fallback("the quick brown fox met the lazy dog")

    assert ordered is not None
    assert ordered.index == 4

    scrambled = SlideController(scoring_deck, current_slide=1)
    assert scrambled.keyword_fallback("the brown quick fox met the dog lazy") is None


def test_fallback_keeps_stopwords_inside_an_alias_phrase(scoring_deck: Deck) -> None:
    """TC-BE-036e."""
    controller = SlideController(scoring_deck, current_slide=1)

    action = controller.keyword_fallback("we measure time to first token latency here")

    assert action is not None
    assert action.index == 5


def test_fallback_ignores_an_answer_of_pure_stopwords(scoring_deck: Deck) -> None:
    """TC-BE-036f."""
    controller = SlideController(scoring_deck, current_slide=1)

    assert controller.keyword_fallback("and then it was the same as that one for us") is None


@pytest.mark.parametrize("text", ["", "   ", "!!! ... ???"])
def test_fallback_ignores_an_empty_answer(scoring_deck: Deck, text: str) -> None:
    """TC-BE-036g."""
    controller = SlideController(scoring_deck, current_slide=1)

    assert controller.keyword_fallback(text) is None
    assert controller.current_slide == 1


def test_fallback_reason_names_the_matched_alias(demo_deck: Deck) -> None:
    """TC-BE-036h."""
    controller = SlideController(demo_deck, current_slide=1)

    action = controller.keyword_fallback(LATENCY_ANSWER)

    assert action is not None
    assert action.reason in {"Keyword match: latency", "Keyword match: milliseconds"}


def test_bullet_words_alone_never_clear_the_threshold(scoring_deck: Deck) -> None:
    """TC-BE-036j."""
    controller = SlideController(scoring_deck, current_slide=1)

    # One bullet word each for slides 1, 3 and 4: the weakest signal there is.
    assert controller.keyword_fallback("bravo foxtrot hotel") is None
    assert controller.current_slide == 1


def test_repetition_alone_does_not_move_the_deck(scoring_deck: Deck) -> None:
    """TC-BE-036i."""
    controller = SlideController(scoring_deck, current_slide=1)

    # "charlie delta" scores 3; saying it six times must still score 3.
    assert controller.keyword_fallback(" ".join([BELOW_THRESHOLD_ANSWER] * 6)) is None


# --------------------------------------------------------------------------- #
# User navigation and the presentation cursor (TR-063, TR-064)
# --------------------------------------------------------------------------- #


def test_user_navigation_moves_the_slide_but_not_the_cursor(demo_deck: Deck) -> None:
    """TC-BE-037."""
    controller = SlideController(demo_deck, mode=SessionMode.PRESENT)
    assert controller.advance_cursor() is True
    assert controller.presentation_cursor == 2

    note = controller.on_user_navigation(5)

    assert controller.current_slide == 5
    assert controller.presentation_cursor == 2
    assert "slide 5" in note
    assert demo_deck.slide(5).title in note


def test_user_navigation_clamps_an_impossible_index(controller: SlideController) -> None:
    """TC-BE-037a."""
    note = controller.on_user_navigation(99)

    assert controller.current_slide == 6
    assert "slide 6" in note


def test_advance_cursor_stops_at_the_end_of_the_deck(demo_deck: Deck) -> None:
    """TC-BE-038."""
    controller = SlideController(demo_deck, mode=SessionMode.PRESENT)
    while controller.advance_cursor():
        pass
    assert controller.presentation_cursor == 6

    assert controller.advance_cursor() is False
    assert controller.presentation_cursor == 6


def test_advance_cursor_is_inert_in_qa_mode(controller: SlideController) -> None:
    """TC-BE-038a."""
    assert controller.advance_cursor() is False
    assert controller.presentation_cursor == 1


def test_advance_cursor_leaves_the_current_slide_alone(demo_deck: Deck) -> None:
    """TC-BE-038b."""
    controller = SlideController(demo_deck, mode=SessionMode.PRESENT)

    assert controller.advance_cursor() is True

    assert controller.presentation_cursor == 2
    assert controller.current_slide == 1


# --------------------------------------------------------------------------- #
# Construction and snapshot (TR-060, TR-064)
# --------------------------------------------------------------------------- #


def test_snapshot_reports_the_prompt_facts(demo_deck: Deck) -> None:
    """TC-BE-030a."""
    controller = SlideController(demo_deck, current_slide=3, mode=SessionMode.PRESENT)

    assert controller.snapshot() == {
        "current_slide": 3,
        "presentation_cursor": 3,
        "mode": "present",
        "slide_count": 6,
    }


def test_construction_clamps_an_impossible_starting_slide(demo_deck: Deck) -> None:
    """TC-BE-030b."""
    assert SlideController(demo_deck, current_slide=99).current_slide == 6
    assert SlideController(demo_deck, current_slide=0).current_slide == 1


def test_controllers_over_one_deck_are_independent(demo_deck: Deck) -> None:
    """TC-BE-030c."""
    first = SlideController(demo_deck)
    second = SlideController(demo_deck)

    first.apply_tool(GO_TO_SLIDE, {"slide_index": 5, "reason": "r"})

    assert second.current_slide == 1
    assert second.last_error is None
