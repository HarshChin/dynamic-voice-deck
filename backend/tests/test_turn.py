"""Unit tests for one turn of the pipeline (:mod:`app.pipeline.turn`).

``run_turn`` is driven directly here, with a scripted model and a collector in
place of the socket, because the behaviour these cases pin is not visible from
the protocol: which messages the *model* gets back, how many requests a turn
makes, and what history holds at the moment a sentence goes out. The same code
is exercised end to end over a WebSocket in ``tests/test_session.py``.

:class:`ScriptedLLM` exists because :class:`~tests.fakes.FakeLLM` replays one
script for every request, and the two-step tool loop is precisely a test about
the *second* request differing from the first.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

import pytest
from app.decks.models import Deck
from app.decks.repository import DeckRepository
from app.errors import ProviderError
from app.pipeline.history import ConversationHistory
from app.pipeline.metrics import TurnMetrics
from app.pipeline.prompt import PromptBuilder
from app.pipeline.slides import SlideController
from app.pipeline.tools import GO_TO_SLIDE, HIGHLIGHT_BULLET
from app.pipeline.turn import (
    FILLER_TRANSCRIPTS,
    MAX_LLM_STEPS,
    NO_ANSWER_FALLBACK,
    Emit,
    cancel_task,
    is_filler,
    is_off_topic_redirect,
    looks_like_tool_syntax,
    provider_error_message,
    run_turn,
    strip_scaffolding,
)
from app.protocol import (
    ErrorCode,
    ServerMessage,
    SlideGotoMsg,
    ToolCallMsg,
    ToolSource,
    TranscriptAgentMsg,
    TranscriptUserMsg,
)
from app.providers.base import LLMDone, LLMEvent, Message, TokenDelta, ToolCallDelta, ToolSpec

from tests.fakes import FakeTTS, LLMCall

DECK_ID = "anatomy_of_a_voice_agent"

MAX_HISTORY_TURNS = 20
"""History cap used here; large enough that nothing under test is capped away."""

TURN_ID = 1
"""Turn identifier stamped on every message these tests assert about."""

MessageT = TypeVar("MessageT", bound=ServerMessage)


class ScriptedLLM:
    """A model provider that answers each request with its own script.

    Args:
        *scripts: One event list per expected request, in order.
        delay_s: Delay before each event, for cancellation tests.

    Attributes:
        name: Provider name, as the health probe would report it.
        calls: Every request, recording the messages and tools it was given.
        closed: How many of those streams have been closed. A generator the
            consumer merely abandoned stays suspended and does not count, which
            is what makes this the observable form of "the HTTP connection was
            released" without a socket to look at.
    """

    def __init__(self, *scripts: Sequence[LLMEvent], delay_s: float = 0.0) -> None:
        self.name = "scripted_llm"
        self._scripts = [list(script) for script in scripts]
        self._delay_s = delay_s
        self.calls: list[LLMCall] = []
        self.closed = 0

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        tool_choice: str = "auto",
    ) -> Any:
        """Replay the script for this request.

        Args:
            messages: Conversation history, recorded then ignored.
            tools: Tools offered, recorded then ignored.
            tool_choice: Whether calls were permitted, recorded then ignored.

        Yields:
            The scripted events for this request.

        Raises:
            AssertionError: If more requests are made than were scripted, which
                is how a runaway tool loop shows up rather than as an IndexError
                deep inside the pipeline.
        """
        index = len(self.calls)
        self.calls.append(
            LLMCall(messages=list(messages), tools=list(tools), tool_choice=tool_choice)
        )
        if index >= len(self._scripts):
            msg = f"request {index + 1} was made but only {len(self._scripts)} were scripted"
            raise AssertionError(msg)
        try:
            for event in self._scripts[index]:
                await asyncio.sleep(self._delay_s)
                yield event
        finally:
            # Reached when the consumer calls aclose(), and not when it simply
            # walks away from a suspended generator.
            self.closed += 1


@dataclass(slots=True)
class Turn:
    """Everything one driven turn produced.

    Attributes:
        result: What ``run_turn`` returned.
        messages: Messages emitted, in order.
        history: The history the turn wrote to.
        slides: The navigation state the turn drove.
        metrics: The timings the turn recorded.
        llm: The provider that served it.
        audio: Binary frames sent, in order.
    """

    result: Any
    messages: list[ServerMessage] = field(default_factory=list)
    audio: list[bytes] = field(default_factory=list)
    history: ConversationHistory = field(default_factory=lambda: ConversationHistory(1))
    slides: Any = None
    metrics: Any = None
    llm: Any = None

    def sent(self, kind: type[MessageT]) -> list[MessageT]:
        """Return every emitted message of one type.

        Args:
            kind: The message class to keep.

        Returns:
            The matching messages, in order.
        """
        return [message for message in self.messages if isinstance(message, kind)]

    @property
    def order(self) -> list[str]:
        """The ``type`` of each emitted message, in order."""
        return [message.type for message in self.messages]

    def replayed(self, call: int) -> list[tuple[str, str]]:
        """Return the role and content of the history sent on one model request.

        Args:
            call: Index into the provider's recorded calls.

        Returns:
            One ``(role, content)`` pair per message after the system prompt.
        """
        return [(message.role, message.content) for message in self.llm.calls[call].messages[1:]]


@pytest.fixture
def deck() -> Deck:
    """Return the shipped deck.

    Returns:
        The six-slide deck every session presents.
    """
    return DeckRepository().get(DECK_ID)


async def drive(
    llm: Any,
    text: str,
    *,
    deck: Deck,
    slides: SlideController | None = None,
    history: ConversationHistory | None = None,
    on_message: Callable[[ServerMessage], None] | None = None,
) -> Turn:
    """Run one turn against a collector and return everything it produced.

    Args:
        llm: The model provider to use.
        text: The user's words.
        deck: The deck being presented.
        slides: Navigation state; a fresh controller on slide 1 by default.
        history: Conversation history; a fresh one by default.
        on_message: Called with each message as it is emitted, before it is
            recorded. Used to observe state *during* the turn.

    Returns:
        The turn's result, messages, and the state it mutated.
    """
    controller = slides if slides is not None else SlideController(deck)
    log = history if history is not None else ConversationHistory(MAX_HISTORY_TURNS)
    metrics = TurnMetrics(turn_id=TURN_ID)
    turn = Turn(result=None, history=log, slides=controller, metrics=metrics, llm=llm)

    async def emit(message: ServerMessage) -> None:
        if on_message is not None:
            on_message(message)
        turn.messages.append(message)

    async def send_audio(frame: bytes) -> None:
        turn.audio.append(frame)

    turn.result = await run_turn(
        turn_id=TURN_ID,
        text=text,
        deck=deck,
        llm=llm,
        tts=FakeTTS(),
        voice=None,
        history=log,
        slides=controller,
        prompts=PromptBuilder(),
        metrics=metrics,
        emit=emit,
        send_audio=send_audio,
    )
    return turn


def start(
    llm: Any,
    text: str,
    *,
    deck: Deck,
    slides: SlideController,
    emit: Emit,
    history: ConversationHistory | None = None,
) -> asyncio.Task[Any]:
    """Start one turn as its own task so a test can cancel it mid-flight.

    ``drive`` awaits the turn inline, which cannot express "cancelled at this
    exact await". These tests need the turn to be a task they hold a handle to.

    Args:
        llm: The model provider to use.
        text: The user's words.
        deck: The deck being presented.
        slides: Navigation state.
        emit: The send callback, which is where the tests land their cancel.
        history: Conversation history; a fresh one by default.

    Returns:
        The running task.
    """
    log = history if history is not None else ConversationHistory(MAX_HISTORY_TURNS)
    return asyncio.create_task(
        run_turn(
            turn_id=TURN_ID,
            text=text,
            deck=deck,
            llm=llm,
            tts=FakeTTS(),
            voice=None,
            history=log,
            slides=slides,
            prompts=PromptBuilder(),
            metrics=TurnMetrics(turn_id=TURN_ID),
            emit=emit,
            send_audio=_discard_audio,
        )
    )


async def _discard_audio(frame: bytes) -> None:
    """Swallow audio frames for tests that only assert on JSON messages.

    Args:
        frame: The framed audio, ignored.
    """


def cancel_self() -> None:
    """Cancel the task this is called from.

    Used inside an ``emit`` callback so the cut lands on a precisely named
    ``await`` inside the turn rather than at whatever the scheduler chose.
    """
    current = asyncio.current_task()
    assert current is not None
    current.cancel()


def speaking(*sentences: str) -> list[LLMEvent]:
    """Build a script that speaks each sentence and stops.

    Args:
        *sentences: Sentences to stream, each ending in a full stop.

    Returns:
        The scripted events.
    """
    events: list[LLMEvent] = [TokenDelta(text=f"{sentence} ") for sentence in sentences]
    events.append(LLMDone(finish_reason="stop"))
    return events


# --------------------------------------------------------------------------- #
# TC-BE-170 -- filler
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("text", ["", "   ", "\n\t ", *sorted(FILLER_TRANSCRIPTS), "THANK YOU."])
def test_filler_is_recognised_whatever_its_spacing_or_case(text: str) -> None:
    """TC-BE-170: F4 -- empty text and Whisper's silence artefacts are filler."""
    assert is_filler(text) is True


@pytest.mark.parametrize("text", ["Thank you for that.", "you know", "What is this?", "..fine"])
def test_a_real_question_is_never_filler(text: str) -> None:
    """TC-BE-170: F4 -- text that merely resembles an artefact is still answered."""
    assert is_filler(text) is False


async def test_a_filler_turn_is_dropped_before_the_model_is_asked(deck: Deck) -> None:
    """TC-BE-170: F4 -- nothing is emitted, nothing is asked, nothing is remembered."""
    llm = ScriptedLLM(speaking("This should never be spoken."))

    turn = await drive(llm, "Thank you.", deck=deck)

    assert turn.result.answered is False
    assert turn.result.text == ""
    assert turn.messages == []
    # Not even a transcript: the user did not say anything to echo back.
    assert llm.calls == []
    assert turn.history.messages == ()
    assert turn.metrics.llm_started is None


# --------------------------------------------------------------------------- #
# TC-BE-171 -- history is written as the answer is spoken
# --------------------------------------------------------------------------- #


async def test_each_sentence_is_in_history_by_the_time_it_is_sent(deck: Deck) -> None:
    """TC-BE-171: TR-051 -- truncating at any sentence is honest, mid-turn.

    An interrupt lands between two sentences, so the only way truncation can
    report what the room heard is for history to already hold the sentence as it
    goes out. This drives the race deliberately: history is truncated from
    inside the emit callback, exactly as the session would on a barge-in.
    """
    llm = ScriptedLLM(speaking("Sure thing.", "Here is the detail.", "And one more point."))
    history = ConversationHistory(MAX_HISTORY_TURNS)
    cuts: list[str] = []

    def truncate_at(message: ServerMessage) -> None:
        if isinstance(message, TranscriptAgentMsg) and message.sentence_id == 1:
            history.truncate_current(TURN_ID, message.sentence_id)
            cuts.append(history.messages[-1].content)

    turn = await drive(
        llm, "Tell me about the deck.", deck=deck, history=history, on_message=truncate_at
    )

    assert [message.text for message in turn.sent(TranscriptAgentMsg)] == [
        "Sure thing.",
        "Here is the detail.",
        "And one more point.",
    ]
    # Sentence two was already recorded when its transcript went out, so the cut
    # keeps both heard sentences and claims nothing else.
    assert cuts == ["Sure thing. Here is the detail. [interrupted by user]"]
    # And the sentence generated after the cut never re-enters history, even
    # though the turn ran on to completion (the interrupt/completion race).
    assert history.messages[-1].content == "Sure thing. Here is the detail. [interrupted by user]"


async def test_a_completed_answer_is_recorded_whole(deck: Deck) -> None:
    """TC-BE-171: TR-050 -- an uninterrupted turn stores the answer and its sentences."""
    llm = ScriptedLLM(speaking("Sure thing.", "Here is the detail."))

    turn = await drive(llm, "Tell me about the deck.", deck=deck)

    assert turn.result.answered is True
    assert turn.result.text == "Sure thing. Here is the detail."
    assert turn.result.sentences == ["Sure thing.", "Here is the detail."]
    assert turn.metrics.sentences == 2
    assert turn.order == ["transcript.user", "transcript.agent", "transcript.agent"]
    assert turn.sent(TranscriptUserMsg)[0].text == "Tell me about the deck."

    stored = [(message.role, message.content) for message in turn.history.messages]
    assert stored == [
        ("user", "Tell me about the deck."),
        ("assistant", "Sure thing. Here is the detail."),
    ]
    assert turn.history.messages[-1].sentences == ["Sure thing.", "Here is the detail."]


# --------------------------------------------------------------------------- #
# TC-BE-172 -- a tool call the deck cannot honour
# --------------------------------------------------------------------------- #


async def test_an_invalid_tool_call_is_reported_to_the_model_and_moves_nothing(
    deck: Deck,
) -> None:
    """TC-BE-172: TR-061 -- a hallucinated slide is refused softly, not forwarded."""
    llm = ScriptedLLM(
        [
            ToolCallDelta(
                call_id="call_1",
                name=GO_TO_SLIDE,
                arguments={"slide_index": 99, "reason": "invented"},
            ),
            LLMDone(finish_reason="tool_calls"),
        ],
        speaking("Sorry, slide six is the last one."),
    )
    slides = SlideController(deck)

    turn = await drive(llm, "Go to slide ninety-nine.", deck=deck, slides=slides)

    # The client never hears about a navigation that did not happen.
    assert turn.sent(ToolCallMsg) == []
    assert turn.sent(SlideGotoMsg) == []
    assert slides.current_slide == 1

    # The model is told why, in a sentence it can act on, and it gets another
    # step to correct itself.
    rejections = [message.content for message in llm.calls[1].messages if message.role == "tool"]
    assert len(rejections) == 1
    assert "does not exist" in rejections[0]
    assert "1 to 6" in rejections[0]
    assert turn.result.answered is True


async def test_an_unknown_tool_is_refused_without_raising(deck: Deck) -> None:
    """TC-BE-172: TR-061 -- a tool that does not exist is a message, not a crash."""
    llm = ScriptedLLM(
        [
            ToolCallDelta(call_id="call_1", name="delete_deck", arguments={}),
            LLMDone(finish_reason="tool_calls"),
        ],
        speaking("I cannot do that."),
    )

    turn = await drive(llm, "Delete the deck.", deck=deck)

    assert turn.sent(SlideGotoMsg) == []
    rejection = [message.content for message in llm.calls[1].messages if message.role == "tool"]
    assert len(rejection) == 1
    assert GO_TO_SLIDE in rejection[0]
    assert HIGHLIGHT_BULLET in rejection[0]


# --------------------------------------------------------------------------- #
# TC-BE-173 / TC-BE-175 -- the two-step loop
# --------------------------------------------------------------------------- #


async def test_a_tool_call_finish_triggers_a_second_request_that_still_offers_tools(
    deck: Deck,
) -> None:
    """TC-BE-173: TR-032 -- the model navigates, then is asked again so it can speak."""
    llm = ScriptedLLM(
        [
            ToolCallDelta(
                call_id="call_1",
                name=GO_TO_SLIDE,
                arguments={"slide_index": 4, "reason": "User asked about interruption"},
            ),
            LLMDone(finish_reason="tool_calls"),
        ],
        speaking("Two layers, actually."),
    )
    slides = SlideController(deck)

    turn = await drive(llm, "How do you handle interruptions?", deck=deck, slides=slides)

    assert len(llm.calls) == 2
    # The first request offers the tools; the second deliberately offers none,
    # so the model cannot navigate twice for one question and the schemas are
    # not paid for again.
    assert [tool.name for tool in llm.calls[0].tools] == [GO_TO_SLIDE, HIGHLIGHT_BULLET]
    # Both calls offer the tools. Withholding them on the second call was tried
    # against the live API in two forms and both failed: omitting the schemas
    # makes the server reject the model's own tool call, and declaring them with
    # tool_choice="none" does the same. The model also began typing tool syntax
    # into its spoken answer when it could not see a tool to call.
    assert llm.calls[1].tools == llm.calls[0].tools
    assert [message.role for message in llm.calls[1].messages].count("tool") == 1

    # The second prompt is rebuilt against the moved deck, so it carries the
    # notes of the slide the model just navigated to.
    assert "SLIDE 1" in llm.calls[0].messages[0].content
    assert "SLIDE 4" in llm.calls[1].messages[0].content
    assert "Interruption is handled in two tiers." in llm.calls[1].messages[0].content

    assert turn.order == ["transcript.user", "tool.call", "slide.goto", "transcript.agent"]
    assert turn.sent(ToolCallMsg)[0].source is ToolSource.LLM
    assert turn.sent(SlideGotoMsg)[0].index == 4
    assert slides.current_slide == 4
    assert turn.result.text == "Two layers, actually."


async def test_the_turn_stops_at_the_step_ceiling_however_the_model_finishes(deck: Deck) -> None:
    """TC-BE-175: the step ceiling holds even when every step calls a tool.

    Three steps, not two, because of a failure seen live: the model called
    `go_to_slide` and then `highlight_bullet`, both with empty content, and the
    turn ended having moved the deck in silence. The extra request is the chance
    to speak; the ceiling is what stops it looping.
    """
    navigate = [
        ToolCallDelta(
            call_id="call_1", name=GO_TO_SLIDE, arguments={"slide_index": 2, "reason": "latency"}
        ),
        LLMDone(finish_reason="tool_calls"),
    ]
    # Exactly as many scripts as steps: one more request would raise rather than
    # silently loop.
    llm = ScriptedLLM(*([list(navigate)] * MAX_LLM_STEPS))

    turn = await drive(llm, "How fast are you?", deck=deck)

    assert len(llm.calls) == MAX_LLM_STEPS == 3
    # The model navigated on both steps and never spoke, so the turn falls back
    # to naming the slide. Silence after a visible slide change reads as a
    # broken app, which is worse than a plain sentence.
    assert turn.result.answered is True
    assert turn.result.text == "Here's slide 2."
    # Every step navigated, and the client was told about each.
    assert [message.index for message in turn.sent(SlideGotoMsg)] == [2] * MAX_LLM_STEPS


async def test_the_second_request_replays_what_was_already_spoken(deck: Deck) -> None:
    """TC-BE-234: TR-050 -- a model that speaks then navigates must not repeat itself."""
    llm = ScriptedLLM(
        [
            TokenDelta(text="Two layers, actually. "),
            ToolCallDelta(
                call_id="call_1",
                name=GO_TO_SLIDE,
                arguments={"slide_index": 4, "reason": "User asked about interruption"},
            ),
            LLMDone(finish_reason="tool_calls"),
        ],
        speaking("The browser stops playback first."),
    )

    turn = await drive(llm, "How do you handle interruptions?", deck=deck)

    # The opener reached the client during step one but only reaches history
    # when the whole turn ends, so the second request has to carry it. Without
    # it the model cannot see its own first sentence, says it again, and the
    # room hears "Two layers, actually" twice.
    assert ("assistant", "Two layers, actually.") in turn.replayed(1)
    assert turn.result.text == "Two layers, actually. The browser stops playback first."
    # The first request has nothing to replay, so it is left exactly as it was.
    # The slide on screen is stamped INSIDE the question rather than beside it,
    # because a model cannot skim past a phrase in the sentence it is answering
    # (see prompt._stamp_last_question).
    replayed = turn.replayed(0)
    assert [role for role, _ in replayed] == ["user"]
    question = replayed[0][1]
    assert question.startswith("[Looking at slide 1 of 6:")
    assert question.endswith("How do you handle interruptions?")


async def test_a_plain_answer_costs_a_single_request(deck: Deck) -> None:
    """TC-BE-173: a turn that finishes with ``stop`` never asks a second time."""
    llm = ScriptedLLM(speaking("Sure thing."))

    await drive(llm, "What is this deck about?", deck=deck)

    assert len(llm.calls) == 1


# --------------------------------------------------------------------------- #
# TC-BE-174 -- the keyword fallback
# --------------------------------------------------------------------------- #


async def test_the_fallback_routes_an_answer_that_called_no_tool(deck: Deck) -> None:
    """TC-BE-174: TR-062 -- an answer about another slide moves the deck after the text."""
    llm = ScriptedLLM(
        speaking(
            "It comes down to milliseconds.",
            "The latency budget is mostly endpointing, and time to first audio is "
            "about one and a half seconds.",
        )
    )
    slides = SlideController(deck)

    turn = await drive(llm, "How fast are you?", deck=deck, slides=slides)

    assert turn.order[-2:] == ["tool.call", "slide.goto"]
    call = turn.sent(ToolCallMsg)[0]
    assert call.source is ToolSource.FALLBACK
    assert call.name == GO_TO_SLIDE
    assert call.args["slide_index"] == 2
    assert slides.current_slide == 2
    # The model is told the deck moved, so its next turn is not confused by it.
    notes = [
        message.content for message in turn.history.messages if message.content.startswith("[Deck")
    ]
    assert notes == ["[Deck moved to slide 2 by keyword match]"]


async def test_the_fallback_is_not_consulted_once_a_tool_has_fired(deck: Deck) -> None:
    """TC-BE-174: TR-062 -- a deliberate "stay here" is never overridden by keywords."""
    # The answer is full of slide-four vocabulary, which on its own would route
    # there; the model's own call to slide six must win.
    llm = ScriptedLLM(
        [
            ToolCallDelta(
                call_id="call_1",
                name=GO_TO_SLIDE,
                arguments={"slide_index": 6, "reason": "Comparing the approaches"},
            ),
            *speaking(
                "Two layers, actually.",
                "The browser flushes playback and the server cancels the pipeline task.",
            ),
        ]
    )
    slides = SlideController(deck)

    turn = await drive(llm, "What would you do differently?", deck=deck, slides=slides)

    assert [message.source for message in turn.sent(ToolCallMsg)] == [ToolSource.LLM]
    assert [message.index for message in turn.sent(SlideGotoMsg)] == [6]
    assert slides.current_slide == 6


async def test_a_rejected_tool_call_still_lets_the_fallback_route_the_answer(deck: Deck) -> None:
    """TC-BE-232: TR-062 -- a call the deck refused is not the model navigating."""
    llm = ScriptedLLM(
        [
            ToolCallDelta(
                call_id="call_1",
                name=GO_TO_SLIDE,
                arguments={"slide_index": 99, "reason": "invented"},
            ),
            LLMDone(finish_reason="tool_calls"),
        ],
        speaking(
            "It comes down to milliseconds.",
            "The latency budget is mostly endpointing, and time to first audio is "
            "about one and a half seconds.",
        ),
    )
    slides = SlideController(deck)

    turn = await drive(llm, "How fast are you?", deck=deck, slides=slides)

    # The rejected call moved nothing, so the answer is still unrouted and the
    # keyword fallback is the only thing that can put the room on slide two.
    # Counting the call as "the model navigated" would leave the deck on slide
    # one with the agent describing the latency budget.
    assert [message.source for message in turn.sent(ToolCallMsg)] == [ToolSource.FALLBACK]
    assert [message.index for message in turn.sent(SlideGotoMsg)] == [2]
    assert slides.current_slide == 2


async def test_an_off_topic_redirect_leaves_the_deck_where_it_is(deck: Deck) -> None:
    """TC-BE-233: PRD §8 -- declining a question must not move the deck."""
    llm = ScriptedLLM(
        speaking("That's outside this deck, but I can show you the slide on tool calling.")
    )
    slides = SlideController(deck)

    turn = await drive(llm, "What's the capital of France?", deck=deck, slides=slides)

    # The sentence is loud with slide five's vocabulary -- "tool calling" is one
    # of its aliases, and scoring it alone routes there. That is exactly the
    # mistake: the agent offered the slide, it did not describe it, and the
    # prompt requires an off-topic answer to navigate nothing at all.
    assert turn.sent(ToolCallMsg) == []
    assert turn.sent(SlideGotoMsg) == []
    assert slides.current_slide == 1


@pytest.mark.parametrize(
    "answer",
    [
        "That's outside this deck, but I can show you the slide on tool calling.",
        "That's not in this deck. What I can tell you is why we picked open weights.",
        "Sorry, that isn't in this deck; the trade-offs slide is the closest thing.",
        "This deck doesn't cover pricing, but slide six covers the cost trade-off.",
        "The deck does not go into that.",
        "That's beyond the deck, though slide two covers latency.",
    ],
)
def test_a_decline_is_recognised_however_it_is_phrased(answer: str) -> None:
    """TC-BE-233: PRD §8 -- every redirect the prompt asks for is caught."""
    assert is_off_topic_redirect(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "Two layers, actually. The browser flushes playback and the server cancels.",
        "It comes down to milliseconds; the budget is one and a half seconds.",
        "This deck is about a voice agent explaining its own architecture.",
        "Not much, really.",
    ],
)
def test_an_answer_that_engages_with_the_deck_is_not_a_redirect(answer: str) -> None:
    """TC-BE-233: TR-062 -- the guard must not swallow ordinary answers."""
    assert is_off_topic_redirect(answer) is False


async def test_the_fallback_leaves_the_deck_alone_when_the_answer_is_local(deck: Deck) -> None:
    """TC-BE-174: TR-062 -- thin or on-topic evidence moves nothing."""
    llm = ScriptedLLM(speaking("Sure thing.", "Let me answer that."))
    slides = SlideController(deck)

    turn = await drive(llm, "What is this deck about?", deck=deck, slides=slides)

    assert turn.sent(ToolCallMsg) == []
    assert turn.sent(SlideGotoMsg) == []
    assert slides.current_slide == 1


# --------------------------------------------------------------------------- #
# TC-BE-176 / TC-BE-177 -- failures and cancellation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("groq_llm", ErrorCode.LLM_FAILED),
        ("ollama", ErrorCode.LLM_FAILED),
        ("groq_stt", ErrorCode.STT_FAILED),
        ("kokoro", ErrorCode.TTS_FAILED),
        ("something_new", ErrorCode.LLM_FAILED),
    ],
)
def test_a_provider_failure_maps_to_its_error_code(provider: str, expected: ErrorCode) -> None:
    """TC-BE-176: TR-170 -- each stage's failure reaches the client under its own code."""
    message = provider_error_message(ProviderError(provider, "upstream refused"))

    assert message.code is expected
    assert message.message == "upstream refused"
    # Recoverable: the session returns to listening and the user may ask again.
    assert message.recoverable is True


def test_a_throttled_provider_is_reported_as_rate_limited() -> None:
    """TC-BE-176: TR-171 -- a retryable failure carrying retry-after is rate_limited."""
    message = provider_error_message(
        ProviderError("groq_llm", "slow down", retryable=True, retry_after=3.0)
    )

    assert message.code is ErrorCode.RATE_LIMITED
    assert message.recoverable is True

    # Retryable without a retry-after is not throttling; it stays llm_failed.
    unspecified = provider_error_message(ProviderError("groq_llm", "reset", retryable=True))

    assert unspecified.code is ErrorCode.LLM_FAILED


async def test_cancelling_a_turn_waits_for_it_to_unwind() -> None:
    """TC-BE-177: TR-022 -- cancel_task awaits, so two turns never overlap."""
    started = asyncio.Event()
    finished = False

    async def slow() -> None:
        nonlocal finished
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            finished = True
            raise

    task = asyncio.create_task(slow())
    await started.wait()
    await cancel_task(task)

    assert task.cancelled()
    # The cancellation was awaited, not merely requested: the task has already
    # run its cleanup by the time cancel_task returns.
    assert finished is True


async def test_cancelling_nothing_is_safe() -> None:
    """TC-BE-177: TR-022 -- no task, or a finished one, is a no-op."""
    await cancel_task(None)

    async def quick() -> None:
        return None

    done = asyncio.create_task(quick())
    await done

    await cancel_task(done)

    assert done.done()
    assert not done.cancelled()


# --------------------------------------------------------------------------- #
# TC-BE-235 -- a turn that produces nothing
# --------------------------------------------------------------------------- #


async def test_a_turn_that_generates_nothing_still_says_something(deck: Deck) -> None:
    """TC-BE-235: F5 -- silence reads as a crash, and an empty turn poisons history."""
    llm = ScriptedLLM([LLMDone(finish_reason="stop")])
    slides = SlideController(deck)

    turn = await drive(llm, "What is this deck about?", deck=deck, slides=slides)

    assert turn.result.answered is True
    assert turn.result.text == NO_ANSWER_FALLBACK
    assert turn.result.sentences == [NO_ANSWER_FALLBACK]
    assert [message.text for message in turn.sent(TranscriptAgentMsg)] == [NO_ANSWER_FALLBACK]
    # Nothing moved, so the turn does not pretend the deck did.
    assert turn.sent(SlideGotoMsg) == []
    assert slides.current_slide == 1
    # And no empty assistant entry is left behind. One would be replayed on
    # every later request as a turn in which the agent answered with nothing.
    assert [(message.role, message.content) for message in turn.history.messages] == [
        ("user", "What is this deck about?"),
        ("assistant", NO_ANSWER_FALLBACK),
    ]


# --------------------------------------------------------------------------- #
# TC-BE-236 -- cancellation closes what it opened
# --------------------------------------------------------------------------- #


async def test_cancelling_mid_answer_closes_the_model_stream_at_once(deck: Deck) -> None:
    """TC-BE-236: TR-031, TR-034 -- barge-in releases the HTTP stream, not the collector."""
    llm = ScriptedLLM(speaking("First point.", "Second point."))
    slides = SlideController(deck)
    sent: list[ServerMessage] = []

    async def emit(message: ServerMessage) -> None:
        sent.append(message)
        if isinstance(message, TranscriptAgentMsg):
            cancel_self()
        # A real send suspends, which is where a pending cancellation lands: in
        # the consumer's own loop body, with the generator parked on its yield.
        await asyncio.sleep(0)

    task = start(llm, "Tell me about the deck.", deck=deck, slides=slides, emit=emit)

    with pytest.raises(asyncio.CancelledError):
        await task

    # Closed while the turn unwound, not whenever the collector next runs. This
    # is the property barge-in rests on: the provider uses raw httpx precisely
    # so that closing the iterator closes the connection.
    assert llm.closed == 1
    # Nothing was generated after the cut, either.
    assert [message.type for message in sent] == ["transcript.user", "transcript.agent"]


async def test_an_ordinary_turn_leaves_no_stream_suspended(deck: Deck) -> None:
    """TC-BE-236: TR-031 -- the loop breaks on ``done``, so it must close on the way out."""
    plain = ScriptedLLM(speaking("Sure thing."))

    await drive(plain, "What is this deck about?", deck=deck)

    assert plain.closed == 1

    two_step = ScriptedLLM(
        [
            ToolCallDelta(
                call_id="call_1",
                name=GO_TO_SLIDE,
                arguments={"slide_index": 4, "reason": "User asked about interruption"},
            ),
            LLMDone(finish_reason="tool_calls"),
        ],
        speaking("Two layers, actually."),
    )

    await drive(two_step, "How do you handle interruptions?", deck=deck)

    assert two_step.closed == len(two_step.calls) == 2


async def test_a_cancel_between_the_tool_call_and_the_goto_still_moves_the_deck(
    deck: Deck,
) -> None:
    """TC-BE-236: TR-021 -- the pair the audience depends on is never split."""
    llm = ScriptedLLM(
        [
            ToolCallDelta(
                call_id="call_1",
                name=GO_TO_SLIDE,
                arguments={"slide_index": 4, "reason": "User asked about interruption"},
            ),
            LLMDone(finish_reason="tool_calls"),
        ],
        speaking("Two layers, actually."),
    )
    slides = SlideController(deck)
    sent: list[ServerMessage] = []

    async def emit(message: ServerMessage) -> None:
        sent.append(message)
        if isinstance(message, ToolCallMsg):
            cancel_self()
        await asyncio.sleep(0)

    task = start(llm, "How do you handle interruptions?", deck=deck, slides=slides, emit=emit)

    with pytest.raises(asyncio.CancelledError):
        await task

    # The controller moved the moment the call validated, so a cut here would
    # otherwise leave the server on slide four and the room on slide one, for
    # the rest of the session, with nothing to reconcile them.
    assert slides.current_slide == 4
    assert [message.type for message in sent] == ["transcript.user", "tool.call", "slide.goto"]
    assert [message.index for message in sent if isinstance(message, SlideGotoMsg)] == [4]


async def test_cancel_task_lets_a_cancellation_aimed_at_the_caller_through() -> None:
    """TC-BE-237: TR-022 -- the waiter's own cancellation is not the turn's acknowledgement."""
    started = asyncio.Event()
    acknowledged = asyncio.Event()
    release = asyncio.Event()

    async def slow_to_unwind() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            acknowledged.set()
            # Cleanup that outlives the caller, so the caller is still parked in
            # `await task` at the moment it is cancelled itself.
            await release.wait()
            raise

    inner = asyncio.create_task(slow_to_unwind())
    await started.wait()
    waiter = asyncio.create_task(cancel_task(inner))
    await acknowledged.wait()

    waiter.cancel()
    release.set()

    # Two CancelledErrors reach the same await. Swallowing both -- which is all
    # `contextlib.suppress` can do -- strands whoever cancelled the waiter: they
    # asked it to stop and it returned as though nothing had happened.
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert waiter.cancelled()
    assert inner.done()


async def test_a_sentence_already_spoken_this_turn_is_not_said_again(deck: Deck) -> None:
    """TC-BE-242: the second request reopening with the first one's words is dropped.

    A navigating turn costs two model requests, and the second regularly repeats
    the sentence the first already spoke. Merging the spoken text into the
    tool-call message and instructing the model not to repeat both reduced it
    without removing it, so the guarantee is made in code.
    """
    opener = "Two layers, actually."
    llm = ScriptedLLM(
        [
            TokenDelta(text=f"{opener} "),
            ToolCallDelta(
                call_id="call_1",
                name=GO_TO_SLIDE,
                arguments={"slide_index": 4, "reason": "interruption"},
            ),
            LLMDone(finish_reason="tool_calls"),
        ],
        # The model reopens with the same sentence, differently punctuated.
        [
            TokenDelta(text="two layers actually! The browser stops first."),
            LLMDone(finish_reason="stop"),
        ],
    )

    turn = await drive(llm, "How do you handle interruptions?", deck=deck)

    spoken = [message.text for message in turn.sent(TranscriptAgentMsg)]
    assert spoken.count(opener) == 1
    assert any("browser stops first" in text for text in spoken)


async def test_a_repeat_in_a_later_turn_is_still_allowed(deck: Deck) -> None:
    """TC-BE-243: the drop is scoped to one turn, not to the conversation.

    Asked the same question twice a listener should hear the answer twice; it is
    only within a single answer that a repeat is a defect.
    """
    line = "Two layers, actually."
    history = ConversationHistory(MAX_HISTORY_TURNS)
    controller = SlideController(deck)

    for _ in range(2):
        llm = ScriptedLLM([TokenDelta(text=line), LLMDone(finish_reason="stop")])
        turn = await drive(
            llm, "How do you handle interruptions?", deck=deck, slides=controller, history=history
        )
        assert [message.text for message in turn.sent(TranscriptAgentMsg)] == [line]


@pytest.mark.parametrize(
    "sentence",
    [
        'Go to slide(4, "User asked about interruption handling")',
        "go_to_slide(2, 'latency')",
        "Let me go to slide (6) for you.",
        "highlight_bullet(1)",
        "highlightbullet (0)",
    ],
)
def test_a_tool_call_the_model_typed_is_never_spoken(sentence: str) -> None:
    """TC-BE-320: TR-086 -- a call written as prose is dropped, not read aloud.

    Seen from the local fallback model and, earlier, from gpt-oss-120b: the
    model writes the call instead of making it, and the listener hears the
    arguments read out. The prompt asks them not to; this makes it true.
    """
    assert looks_like_tool_syntax(sentence)


@pytest.mark.parametrize(
    "sentence",
    [
        "I call a function called go_to_slide to move the deck.",
        "There is also highlight_bullet for emphasising one line.",
        "Navigating costs two model round trips per turn.",
        "The slide about tool calling is slide five.",
    ],
)
def test_an_answer_that_merely_names_a_tool_is_still_spoken(sentence: str) -> None:
    """TC-BE-321: TR-086 -- slide 5 explains these tools by name, and must still be readable.

    The opening bracket is the whole discriminator: a description names the
    tool, a mistyped call follows it with arguments.
    """
    assert not looks_like_tool_syntax(sentence)


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        # Observed from the local fallback model, which continued its own history instead of
        # answering afresh and read the markers out.
        (
            "interrupted by user before speaking Detection runs in your browser,",
            "Detection runs in your browser,",
        ),
        ("interrupted by user before speaking Sure,", "Sure,"),
        ('[Looking at slide 3 of 6: "Hearing"] Detection runs here.', "Detection runs here."),
        ("[interrupted by user] Two layers, actually.", "Two layers, actually."),
        ("Looking at slide 1 of 6:", ""),
    ],
)
def test_scaffolding_the_model_read_back_is_stripped(spoken: str, expected: str) -> None:
    """TC-BE-334: TR-088 -- a marker meant for the model is never read to the listener.

    The conversation carries two bracketed notes that are instructions rather
    than things to say: where a turn was cut off, and which slide the room is
    looking at. Stripping rather than dropping keeps the sentence the echo was
    prefixed to, which is usually the actual answer.
    """
    assert strip_scaffolding(spoken) == expected


@pytest.mark.parametrize(
    "spoken",
    [
        'The cut is marked "[interrupted by user]".',
        "with a marker reading interrupted by user appended",
        "Two layers, actually.",
        "The browser flushes playback at once.",
    ],
)
def test_an_answer_about_the_marker_is_still_spoken(spoken: str) -> None:
    """TC-BE-335: TR-088 -- slide 4 explains that marker, so it must stay speakable.

    The guard is anchored to the start of a segment for exactly this reason: an
    answer that *mentions* the marker is the deck doing its job, and only an
    answer that *begins* by reciting it is an echo.
    """
    assert strip_scaffolding(spoken) == spoken
