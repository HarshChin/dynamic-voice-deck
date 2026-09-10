"""Tests for ConversationHistory (TRD TR-050 to TR-054).

The truncation cases are the point of the module: after a barge-in, history must
contain only what the room heard. A test that lets an unheard sentence survive is
testing the exact bug the feature exists to prevent.
"""

from __future__ import annotations

import json

import pytest
from app.config import Settings, get_settings
from app.pipeline.history import (
    INTERRUPTED_BEFORE_SPEAKING,
    INTERRUPTED_MARKER,
    ConversationHistory,
)
from app.providers.base import Message

SYSTEM_PROMPT = "You are the presenter."


def make_history(max_turns: int | None = None) -> ConversationHistory:
    """Build a history with the system prompt already pinned."""
    history = ConversationHistory(max_turns=max_turns)
    history.add_system(SYSTEM_PROMPT)
    return history


def speak(history: ConversationHistory, turn_id: int, sentences: list[str]) -> None:
    """Begin a turn and record every sentence as the TTS sender would."""
    history.begin_assistant_turn(turn_id)
    for sentence in sentences:
        history.record_sentence(sentence)


def add_pairs(history: ConversationHistory, count: int, *, with_tools: bool = False) -> None:
    """Append `count` complete user/assistant exchanges, numbered from one."""
    for n in range(1, count + 1):
        history.add_user(f"question {n}")
        history.begin_assistant_turn(n)
        if with_tools:
            history.add_tool_call(f"call_{n}", "go_to_slide", {"slide_index": n, "reason": "r"})
            history.add_tool_result(f"call_{n}", "go_to_slide", f"moved to slide {n}")
        history.add_assistant(f"answer {n}", [f"answer {n}"])


def assert_tool_messages_are_paired(messages: list[Message]) -> None:
    """Assert no `tool` message is orphaned and no announced call is unanswered.

    Either half of the invariant is a hard 400 from an OpenAI-compatible
    provider, so the assertion is deliberately symmetric.
    """
    announced: set[str] = set()
    answered: set[str] = set()
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            announced.update(str(call["id"]) for call in message.tool_calls)
        if message.role == "tool":
            assert message.tool_call_id is not None
            assert message.tool_call_id in announced, f"orphan tool message {message.tool_call_id}"
            answered.add(message.tool_call_id)
    assert announced == answered, "every announced tool call must have a result"


# --- Truncation (TR-051) -----------------------------------------------------


def test_truncating_mid_turn_keeps_only_the_sentences_that_were_heard() -> None:
    """TC-BE-220."""
    history = make_history()
    history.add_user("what is barge-in?")
    speak(history, 1, ["s0", "s1", "s2"])

    assert history.truncate_current(1, 1) is True

    assistant = history.to_provider_messages()[-1]
    assert assistant.role == "assistant"
    assert assistant.content == f"s0 s1 {INTERRUPTED_MARKER}"
    assert "s2" not in assistant.content


def test_truncating_before_playback_started_records_that_nothing_was_said() -> None:
    """TC-BE-021."""
    history = make_history()
    history.add_user("what is barge-in?")
    speak(history, 1, ["s0", "s1"])

    assert history.truncate_current(1, None) is True

    assistant = history.to_provider_messages()[-1]
    assert assistant.content == INTERRUPTED_BEFORE_SPEAKING
    assert "s0" not in assistant.content


def test_truncation_retains_the_tool_call_and_result_of_the_cut_turn() -> None:
    """TC-BE-022."""
    history = make_history()
    history.add_user("tell me about interruption")
    history.begin_assistant_turn(1)
    history.add_tool_call("call_1", "go_to_slide", {"slide_index": 4, "reason": "barge-in"})
    history.add_tool_result("call_1", "go_to_slide", "moved to slide 4")
    history.record_sentence("Slide four covers barge-in.")
    history.record_sentence("It cancels the turn task.")

    history.truncate_current(1, 0)

    messages = history.to_provider_messages()
    roles = [m.role for m in messages]
    # The slide really did move, so the model must still see that it moved.
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    assert messages[2].tool_calls is not None
    assert messages[3].content == "moved to slide 4"
    assert messages[4].content == f"Slide four covers barge-in. {INTERRUPTED_MARKER}"
    assert_tool_messages_are_paired(messages)


# --- Capping (TR-052) --------------------------------------------------------


def test_capping_keeps_the_system_message_and_the_most_recent_pairs() -> None:
    """TC-BE-023."""
    history = make_history()
    add_pairs(history, 25)

    messages = history.to_provider_messages()

    assert messages[0].role == "system"
    assert messages[0].content == SYSTEM_PROMPT
    assert len(messages) == 1 + 2 * get_settings().max_history_turns
    users = [m.content for m in messages if m.role == "user"]
    assert users == [f"question {n}" for n in range(6, 26)]


def test_capping_drops_the_tool_messages_of_a_dropped_pair() -> None:
    """TC-BE-024."""
    history = make_history(max_turns=2)
    add_pairs(history, 3, with_tools=True)

    messages = history.to_provider_messages()

    assert "moved to slide 1" not in [m.content for m in messages]
    assert not any(m.tool_call_id == "call_1" for m in messages)
    assert_tool_messages_are_paired(messages)


# --- System notes (TR-053) ---------------------------------------------------


def test_a_system_note_is_appended_as_a_system_message_at_the_end() -> None:
    """TC-BE-025."""
    history = make_history()
    add_pairs(history, 1)

    history.add_system_note("[User manually moved to slide 4: Barge-in]")

    messages = history.to_provider_messages()
    assert messages[-1].role == "system"
    assert messages[-1].content == "[User manually moved to slide 4: Barge-in]"


# --- Serialisation (TR-054) --------------------------------------------------


def test_serialised_messages_never_carry_the_sentences_bookkeeping() -> None:
    """TC-BE-026."""
    history = make_history()
    history.add_user("q")
    speak(history, 1, ["one.", "two."])
    history.add_assistant("one. two.", ["one.", "two."])

    dumped = [m.model_dump() for m in history.to_provider_messages()]

    assert all("sentences" not in payload for payload in dumped)
    assert all("turn_id" not in payload for payload in dumped)
    # The bookkeeping still exists internally, otherwise truncation could not work.
    assert history.messages[-1].sentences == ["one.", "two."]


# --- Recording an in-progress turn -------------------------------------------


def test_recorded_sentences_are_numbered_from_zero_and_feed_truncation() -> None:
    """TC-BE-027."""
    history = make_history()
    history.add_user("q")
    history.begin_assistant_turn(7)

    ids = [history.record_sentence(text) for text in ("a.", "b.", "c.")]

    assert ids == [0, 1, 2]
    assert history.current_turn_id == 7
    history.truncate_current(7, ids[1])
    assert history.to_provider_messages()[-1].content == f"a. b. {INTERRUPTED_MARKER}"


def test_truncating_a_turn_that_is_not_current_changes_nothing() -> None:
    """TC-BE-028."""
    history = make_history()
    history.add_user("q")
    speak(history, 2, ["a.", "b."])
    before = history.to_provider_messages()

    # A late or duplicate interrupt for an older turn is a routine race
    # (TR-024), so it is ignored rather than raised.
    assert history.truncate_current(1, 0) is False
    assert history.to_provider_messages() == before

    # Still ignored once that turn has finished generating: it is the turn *id*
    # that has to match. What finishing no longer does is make turn 2 itself
    # untouchable -- see the barge-in-after-generation case below.
    history.add_assistant("a. b.", ["a.", "b."])
    assert history.truncate_current(1, 0) is False
    assert history.to_provider_messages()[-1].content == "a. b."


def test_capping_leaves_every_retained_tool_message_paired() -> None:
    """TC-BE-029."""
    history = make_history()
    add_pairs(history, 25, with_tools=True)

    messages = history.to_provider_messages()

    assert_tool_messages_are_paired(messages)
    # system + 20 x (user, assistant tool_calls, tool, assistant answer)
    assert len(messages) == 1 + 4 * 20
    assert messages[1].role == "user"
    assert [m.role for m in messages[1:5]] == ["user", "assistant", "tool", "assistant"]


# --- Depth cases -------------------------------------------------------------


def test_recording_a_sentence_without_a_turn_is_a_programming_error() -> None:
    """TC-BE-221."""
    history = make_history()

    with pytest.raises(RuntimeError, match="no assistant turn in progress"):
        history.record_sentence("orphan.")


def test_a_late_completion_cannot_restore_the_sentences_nobody_heard() -> None:
    """TC-BE-222."""
    history = make_history()
    history.add_user("q")
    speak(history, 1, ["heard.", "unheard."])
    history.truncate_current(1, 0)

    # The turn task finished a moment after the interrupt landed.
    history.add_assistant("heard. unheard.", ["heard.", "unheard."])

    messages = history.to_provider_messages()
    assert [m.role for m in messages] == ["system", "user", "assistant"]
    assert messages[-1].content == f"heard. {INTERRUPTED_MARKER}"


def test_a_second_more_precise_interrupt_refines_the_same_message() -> None:
    """TC-BE-223."""
    history = make_history()
    history.add_user("q")
    speak(history, 1, ["a.", "b.", "c."])

    history.truncate_current(1, 0)
    # TR-023: the client may follow the VAD-triggered interrupt with a better id.
    assert history.truncate_current(1, 1) is True

    messages = history.to_provider_messages()
    assert [m.role for m in messages] == ["system", "user", "assistant"]
    assert messages[-1].content == f"a. b. {INTERRUPTED_MARKER}"
    assert history.messages[-1].sentences == ["a.", "b."]


def test_tool_calls_are_serialised_in_the_openai_wire_shape() -> None:
    """TC-BE-224."""
    history = make_history()
    history.add_user("q")
    history.begin_assistant_turn(1)
    history.add_tool_call("call_9", "go_to_slide", {"slide_index": 4, "reason": "asked"})

    call = history.to_provider_messages()[-1].tool_calls
    assert call is not None
    assert call[0]["id"] == "call_9"
    assert call[0]["type"] == "function"
    assert call[0]["function"]["name"] == "go_to_slide"
    # Arguments travel as a JSON string, not a nested object.
    assert isinstance(call[0]["function"]["arguments"], str)
    assert json.loads(call[0]["function"]["arguments"]) == {"slide_index": 4, "reason": "asked"}


@pytest.mark.parametrize(
    ("sentences", "last_id", "expected"),
    [
        (["a.", "b."], 5, f"a. b. {INTERRUPTED_MARKER}"),
        (["a.", "b."], 0, f"a. {INTERRUPTED_MARKER}"),
        ([], 0, INTERRUPTED_BEFORE_SPEAKING),
        (["a."], -1, INTERRUPTED_BEFORE_SPEAKING),
    ],
)
def test_truncation_boundaries_never_produce_a_dangling_marker(
    sentences: list[str],
    last_id: int,
    expected: str,
) -> None:
    """TC-BE-225."""
    history = make_history()
    history.add_user("q")
    speak(history, 1, sentences)

    history.truncate_current(1, last_id)

    assert history.to_provider_messages()[-1].content == expected


def test_the_cap_comes_from_settings_and_must_be_at_least_one() -> None:
    """TC-BE-226."""
    assert ConversationHistory().max_turns == Settings().max_history_turns
    assert ConversationHistory(max_turns=3).max_turns == 3

    with pytest.raises(ValueError, match="at least 1"):
        ConversationHistory(max_turns=0)


def test_serialisation_returns_provider_messages_with_the_system_prompt_first() -> None:
    """TC-BE-227."""
    history = make_history()
    add_pairs(history, 2)

    messages = history.to_provider_messages()

    assert all(isinstance(m, Message) for m in messages)
    assert [m.role for m in messages] == ["system", "user", "assistant", "user", "assistant"]
    assert messages[0].content == SYSTEM_PROMPT


def test_the_messages_snapshot_cannot_be_used_to_corrupt_history() -> None:
    """TC-BE-228."""
    history = make_history()
    history.add_user("q")
    speak(history, 1, ["a."])
    history.add_assistant("a.", ["a."])

    snapshot = history.messages
    snapshot[-1].content = "tampered"
    snapshot[-1].sentences.append("tampered")

    assert history.to_provider_messages()[-1].content == "a."
    assert history.messages[-1].sentences == ["a."]


def test_a_completed_turn_falls_back_to_the_sentences_it_recorded() -> None:
    """TC-BE-229."""
    history = make_history()
    history.add_user("q")
    speak(history, 1, ["a.", "b."])

    history.add_assistant("a. b.")

    assert history.messages[-1].sentences == ["a.", "b."]
    assert history.messages[-1].turn_id == 1
    # Still the turn in progress: it has stopped generating, not stopped being
    # spoken, and a barge-in landing now must still find something to cut.
    assert history.current_turn_id == 1


def test_the_pinned_prompt_survives_capping_but_a_note_ages_out_with_its_pair() -> None:
    """TC-BE-230."""
    history = make_history(max_turns=1)
    history.add_user("first")
    history.add_system_note("[User manually moved to slide 4: Barge-in]")
    history.add_assistant("answer one", ["answer one"])
    history.add_user("second")
    history.add_assistant("answer two", ["answer two"])

    messages = history.to_provider_messages()

    assert [m.content for m in messages] == [SYSTEM_PROMPT, "second", "answer two"]


def test_a_turn_left_unfinished_is_discarded_when_the_next_one_begins() -> None:
    """TC-BE-231."""
    history = make_history()
    history.add_user("q")
    speak(history, 1, ["a.", "b."])

    # Neither completed nor truncated: the turn failed some other way.
    history.begin_assistant_turn(2)

    assert history.current_turn_id == 2
    assert [m.role for m in history.to_provider_messages()] == ["system", "user"]


def test_a_finished_turn_can_still_be_truncated_until_the_next_one_begins() -> None:
    """TC-BE-200: TR-051 -- the barge-in window outlives generation.

    The model stops generating long before the room stops listening. With audio
    the gap is the whole playback of the answer; in this milestone it is the
    window between the SPEAKING and LISTENING transitions. Completing the turn
    used to clear it, so an interrupt landing there found nothing to cut and
    every sentence nobody heard stayed in history -- the exact failure this
    module exists to prevent.
    """
    history = make_history()
    history.add_user("q")
    speak(history, 1, ["One.", "Two.", "Three.", "Four."])
    history.add_assistant("One. Two. Three. Four.")

    assert history.truncate_current(1, 0) is True

    # Rewritten in place: the finished answer is cut down, not duplicated.
    assert [m.role for m in history.to_provider_messages()] == ["system", "user", "assistant"]
    assert history.to_provider_messages()[-1].content == f"One. {INTERRUPTED_MARKER}"
    assert history.messages[-1].sentences == ["One."]

    # And the completion cannot come back afterwards to undo the cut.
    history.add_assistant("One. Two. Three. Four.")
    assert history.to_provider_messages()[-1].content == f"One. {INTERRUPTED_MARKER}"


def test_the_finished_turn_stops_being_truncatable_once_the_next_one_begins() -> None:
    """TC-BE-201: TR-051 -- the next turn is the only thing that closes the window."""
    history = make_history()
    history.add_user("q")
    speak(history, 1, ["a.", "b."])
    history.add_assistant("a. b.")

    history.add_user("q2")
    history.begin_assistant_turn(2)

    assert history.current_turn_id == 2
    assert history.truncate_current(1, 0) is False
    assert [m.content for m in history.to_provider_messages()] == [
        SYSTEM_PROMPT,
        "q",
        "a. b.",
        "q2",
    ]


@pytest.mark.parametrize(
    ("sentences", "expected"),
    [([], None), (["a."], 0), (["a.", "b.", "c."], 2)],
    ids=["nothing-spoken", "one-sentence", "three-sentences"],
)
def test_the_last_recorded_sentence_is_the_cut_for_an_end_nobody_asked_for(
    sentences: list[str],
    expected: int | None,
) -> None:
    """TC-BE-202: TR-051 -- a watchdog or provider failure cuts at what was sent.

    Nobody interrupted, so there is no client-supplied truncation point; every
    sentence already handed to TTS is one the room heard, and there is nothing
    after it.
    """
    history = make_history()
    history.add_user("q")
    speak(history, 1, sentences)

    assert history.last_recorded_sentence_id == expected

    history.truncate_current(1, history.last_recorded_sentence_id)
    content = history.to_provider_messages()[-1].content
    if expected is None:
        assert content == INTERRUPTED_BEFORE_SPEAKING
    else:
        assert content == f"{' '.join(sentences)} {INTERRUPTED_MARKER}"


def test_no_turn_in_progress_has_no_last_recorded_sentence() -> None:
    """TC-BE-202: a session that has not answered anything yet reports None."""
    assert make_history().last_recorded_sentence_id is None
