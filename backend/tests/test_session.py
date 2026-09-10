"""Session state machine and turn pipeline, driven over a real WebSocket.

These tests exercise :mod:`app.session` the way the browser does: they connect
to ``/ws/session`` with :class:`fastapi.testclient.TestClient`, send protocol
frames, and assert on the frames that come back. Nothing calls a private method
of :class:`~app.session.Session`, because the contract this module is supposed
to protect is the *protocol*, not the class's internals -- a refactor that keeps
the wire behaviour identical should not break a single test here.

Two deliberate choices:

* **No lifespan.** The application state is built by hand
  (:func:`build_app`) instead of by entering the ``TestClient`` as a context
  manager. Startup configures process-wide logging and builds providers from
  the environment; ``tests/test_startup.py`` covers that path, and running it
  once per test here would leak logging handlers into the rest of the suite.
  The providers injected are the fakes, so the wiring under test is real while
  the network is not.
* **A barrier instead of a receive timeout.** Asserting that the server sent
  *nothing* is impossible with a blocking socket, so a frame the server is
  guaranteed to answer -- an unparseable ``type`` -- is sent as a barrier and
  everything received before its ``error`` reply is what the server had already
  produced. Frames dispatched by handlers that ran earlier are necessarily
  ahead of it, because the receive loop handles frames in order.

Audio does not exist in this milestone, so rows that name STT or TTS are
covered through ``text.input`` where the behaviour is genuinely the same
(``TC-BE-053``, ``TC-BE-054``) and skipped with a milestone where it is not
(``TC-BE-056``, ``TC-BE-060``). Each such row says so in its docstring.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
import structlog
from app.config import Settings
from app.decks.repository import DeckRepository
from app.errors import ProviderError
from app.main import AppState, create_app
from app.pipeline.history import INTERRUPTED_MARKER
from app.pipeline.prompt import PromptBuilder
from app.protocol import (
    InterruptMsg,
    PlaybackProgressMsg,
    SessionStartMsg,
    SessionState,
    TextInputMsg,
    decode_audio_frame,
)
from app.providers.base import (
    LLMDone,
    LLMEvent,
    Message,
    Providers,
    TokenDelta,
    ToolCallDelta,
    ToolSpec,
)
from app.session import INTERRUPT_DEBOUNCE_S, Session, SessionManager
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from tests.fakes import CHUNK_BYTES as FAKE_TTS_CHUNK_BYTES
from tests.fakes import FakeLLM, FakeSTT, FakeTTS, LLMCall

DECK_ID = "anatomy_of_a_voice_agent"
"""The shipped deck; every session in this module presents it."""

WS_PATH = "/ws/session"

RECEIVE_TIMEOUT_S = 5.0
"""How long a single expected frame may take before the test fails.

A blocking read that never returns would hang the whole suite instead of
failing one case, which is exactly what a broken watchdog (TC-BE-051) would
cause, so every read is bounded.
"""

BARRIER_TYPE = "__barrier__"
"""``type`` value that no message model claims, used to fence the stream."""

STEP_DELAY_S = 0.05
"""Delay between scripted model events, so a turn can be interrupted mid-flight."""

LONG_SCRIPT_SENTENCES = 30
"""Sentences in a script long enough that a turn cannot finish before an interrupt.

Thirty sentences at :data:`STEP_DELAY_S` is one and a half seconds of streaming,
comfortably longer than the cancellation this module asserts on.
"""

CANCEL_BUDGET_S = 0.5
"""Longest an interrupt may take to produce ``agent.cancelled`` (TR-022).

TC-BE-046 asks for 50 ms; the assertion is deliberately looser so a loaded CI
machine does not fail it, while still being far below the 1.5 s a turn that ran
to completion would take -- which is the regression it exists to catch.
"""

MESSAGES_WITH_TURN_ID = frozenset(
    {"transcript.user", "transcript.agent", "tool.call", "metrics", "agent.cancelled"}
)
"""Server messages that carry the ``turn_id`` of the turn that produced them."""


# --------------------------------------------------------------------------- #
# Providers used only here
# --------------------------------------------------------------------------- #


class FailingLLM:
    """A model provider that fails the way a dead upstream does.

    :class:`~tests.fakes.FakeLLM` replays a script and cannot fail, and the
    error path is what TC-BE-052 is about: a provider failure must reach the
    client as one recoverable ``error`` and leave the session listening.

    Args:
        error: The failure to raise on every request.

    Attributes:
        name: Provider name, as the health probe would report it.
        calls: How many streams were requested.
    """

    def __init__(self, error: ProviderError | None = None) -> None:
        self.name = "failing_llm"
        self._error = error or ProviderError("groq_llm", "upstream refused", retryable=False)
        self.calls = 0

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        tool_choice: str = "auto",
    ) -> AsyncIterator[LLMEvent]:
        """Fail instead of streaming.

        Args:
            messages: Conversation history, ignored.
            tools: Tools offered, ignored.
            tool_choice: Whether calls were permitted, ignored.

        Yields:
            Nothing; the generator raises before its first event.

        Raises:
            ProviderError: Always.
        """
        self.calls += 1
        raise self._error
        yield LLMDone(finish_reason="error")  # pragma: no cover - unreachable


class ExplodingLLM:
    """A model provider that fails in a way the pipeline never anticipated.

    :class:`FailingLLM` raises the one error every layer is written around.
    This raises the kind that only a bug produces -- a payload shape nobody
    modelled -- which is what TC-BE-210 is about: it must still reach the client
    as one recoverable error instead of stranding the session in THINKING.

    Attributes:
        name: Provider name, as the health probe would report it.
        calls: How many streams were requested.
    """

    def __init__(self) -> None:
        self.name = "exploding_llm"
        self.calls = 0

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        tool_choice: str = "auto",
    ) -> AsyncIterator[LLMEvent]:
        """Raise an unmodelled error instead of streaming.

        Args:
            messages: Conversation history, ignored.
            tools: Tools offered, ignored.
            tool_choice: Whether calls were permitted, ignored.

        Yields:
            Nothing; the generator raises before its first event.

        Raises:
            KeyError: Always, standing in for any unhandled failure.
        """
        self.calls += 1
        raise KeyError("choices")
        yield LLMDone(finish_reason="error")  # pragma: no cover - unreachable


class FailingMidAnswerLLM:
    """A model provider that speaks, then dies the way a rate limit does.

    The free tier running out halfway through an answer is the realistic shape
    of TC-BE-212: the room has already heard a sentence when the request fails,
    so what it heard has to survive in history even though nobody interrupted.

    Args:
        script: Events to yield before failing.
        error: The failure to raise once the script is exhausted.

    Attributes:
        name: Provider name, as the health probe would report it.
        calls: Every call, recorded like :class:`~tests.fakes.FakeLLM` records
            them, so a test can read the history replayed on the next request.
    """

    def __init__(self, script: Sequence[LLMEvent], error: ProviderError) -> None:
        self.name = "failing_mid_answer_llm"
        self._script = list(script)
        self._error = error
        self.calls: list[LLMCall] = []

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        tool_choice: str = "auto",
    ) -> AsyncIterator[LLMEvent]:
        """Replay the script, then fail.

        Args:
            messages: Conversation history, recorded then ignored.
            tools: Tools offered, recorded then ignored.
            tool_choice: Whether calls were permitted, recorded then ignored.

        Yields:
            The scripted events, then nothing.

        Raises:
            ProviderError: Once the script is exhausted.
        """
        self.calls.append(
            LLMCall(messages=list(messages), tools=list(tools), tool_choice=tool_choice)
        )
        for event in self._script:
            await asyncio.sleep(0)
            yield event
        raise self._error


# --------------------------------------------------------------------------- #
# Scripts
# --------------------------------------------------------------------------- #


def sentence_script(*sentences: str) -> list[LLMEvent]:
    """Build a model script that speaks each sentence and stops.

    Args:
        *sentences: Sentences to stream, each already ending in a full stop.

    Returns:
        One :class:`~app.providers.base.TokenDelta` per sentence -- the trailing
        space is what makes the chunker emit it -- then a normal completion.
    """
    events: list[LLMEvent] = [TokenDelta(text=f"{sentence} ") for sentence in sentences]
    events.append(LLMDone(finish_reason="stop"))
    return events


SHORT_SCRIPT_SENTENCES = 4
"""Sentences in a script that can be interrupted and still finish quickly.

Long enough that an interrupt lands mid-flight at :data:`SHORT_STEP_DELAY_S`,
short enough that a whole turn runs to completion inside the interrupt window
the next case has to stay within.
"""

SHORT_STEP_DELAY_S = 0.02
"""Delay between scripted events for :func:`short_script`."""


def short_script(count: int = SHORT_SCRIPT_SENTENCES) -> list[LLMEvent]:
    """Build a script that streams a few sentences and finishes.

    Args:
        count: How many sentences to stream.

    Returns:
        The scripted events.
    """
    return sentence_script(*(f"Sentence {index}." for index in range(count)))


def long_script(count: int = LONG_SCRIPT_SENTENCES) -> list[LLMEvent]:
    """Build a script long enough to still be streaming when an interrupt lands.

    Args:
        count: How many sentences to stream.

    Returns:
        The scripted events.
    """
    return sentence_script(*(f"Sentence {index}." for index in range(count)))


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def build_app(llm: Any, **overrides: Any) -> FastAPI:
    """Build an application wired to fake providers, without running startup.

    Args:
        llm: The model provider this application's sessions will use.
        **overrides: Field overrides for :class:`~app.config.Settings`, e.g.
            ``turn_timeout_s=0.05``. The environment is already scrubbed by the
            ``isolated_env`` fixture, so everything else takes its default.

    Returns:
        An application whose ``state.app_state`` is complete: decks loaded,
        providers injected, prompt builder ready.
    """
    app = create_app()
    app.state.app_state = AppState(
        settings=Settings(**overrides),
        decks=DeckRepository(),
        providers=Providers(stt=FakeSTT(), llm=llm, tts=FakeTTS()),
        prompts=PromptBuilder(),
    )
    return app


@dataclass(slots=True)
class Harness:
    """One connected client, with the helpers every test here needs.

    Attributes:
        app: The application under test.
        ws: The open WebSocket session.
        llm: The model provider handed to :func:`build_app`.
        received: Every frame read so far, in order, for post-hoc assertions.
    """

    app: FastAPI
    ws: WebSocketTestSession
    llm: Any
    received: list[dict[str, Any]] = field(default_factory=list)
    audio: list[bytes] = field(default_factory=list)

    # -- reading ---------------------------------------------------------- #

    def recv_frame(self) -> dict[str, Any]:
        """Read one raw ASGI frame, failing rather than blocking forever.

        The read runs on a daemon thread because
        :meth:`starlette.testclient.WebSocketTestSession.receive` blocks with no
        timeout of its own; a test that expects a frame the server never sends
        must fail, not wedge the suite.

        Returns:
            The frame as ASGI describes it: ``{"text": ...}`` or
            ``{"bytes": ...}``.

        Raises:
            AssertionError: If nothing arrives within :data:`RECEIVE_TIMEOUT_S`.
        """
        box: list[Any] = []

        def pump() -> None:
            try:
                box.append(self.ws.receive())
            except BaseException as exc:
                box.append(exc)

        reader = threading.Thread(target=pump, daemon=True, name="ws-recv")
        reader.start()
        reader.join(RECEIVE_TIMEOUT_S)
        assert not reader.is_alive(), (
            f"no frame within {RECEIVE_TIMEOUT_S}s; received so far: {self.summary()}"
        )
        frame = box[0]
        if isinstance(frame, BaseException):
            raise frame
        assert isinstance(frame, dict)
        return frame

    def recv(self) -> dict[str, Any]:
        """Read one JSON message, collecting any audio that arrives first.

        A real client receives both kinds on the same socket. Since M2 the
        server interleaves binary audio frames with the JSON, so this skips past
        them -- keeping each one in :attr:`audio` so a test can still assert on
        what was spoken.

        Returns:
            The decoded message, also appended to :attr:`received`.
        """
        while True:
            frame = self.recv_frame()
            payload = frame.get("bytes")
            if payload is not None:
                self.audio.append(payload)
                continue
            assert "text" in frame, f"expected a text or binary frame, got {frame!r}"
            message: dict[str, Any] = json.loads(frame["text"])
            self.received.append(message)
            return message

    def recv_until(self, matches: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
        """Read messages until one satisfies a predicate.

        Args:
            matches: Predicate applied to each message.

        Returns:
            Every message read, the matching one last.
        """
        collected: list[dict[str, Any]] = []
        while True:
            message = self.recv()
            collected.append(message)
            if matches(message):
                return collected

    # -- writing ---------------------------------------------------------- #

    def send(self, **payload: Any) -> None:
        """Send one JSON message.

        Args:
            **payload: The message body, ``type`` included.
        """
        self.ws.send_json(payload)

    def start(self, deck_id: str = DECK_ID, mode: str = "qa") -> list[dict[str, Any]]:
        """Open the session and read the two frames that admit it.

        Args:
            deck_id: Deck to present.
            mode: Session mode.

        Returns:
            The ``session.ready`` and ``state`` messages, in that order.
        """
        self.send(type="session.start", deck_id=deck_id, mode=mode)
        return [self.recv(), self.recv()]

    def ask(self, text: str) -> list[dict[str, Any]]:
        """Ask a question and read the whole turn it produces.

        Args:
            text: The typed question.

        Returns:
            Every message from the ``state thinking`` that opens the turn to the
            ``state listening`` that closes it.
        """
        self.send(type="text.input", text=text)
        return self.recv_until(is_state(SessionState.LISTENING))

    def barrier(self) -> list[dict[str, Any]]:
        """Fence the stream and return everything the server had already sent.

        Returns:
            Messages received before the barrier's own ``error`` reply, which is
            consumed and not included.
        """
        self.send(type=BARRIER_TYPE)
        collected = self.recv_until(is_barrier)
        return collected[:-1]

    # -- inspection ------------------------------------------------------- #

    @property
    def sessions(self) -> SessionManager:
        """The manager tracking live sessions for this application."""
        manager: SessionManager = self.app.state.app_state.sessions
        return manager

    def summary(self) -> list[tuple[str, Any]]:
        """Return a compact view of what has been received, for failure output.

        Returns:
            One ``(type, value)`` pair per message, where ``value`` is whichever
            of ``value``, ``code``, or ``sentence_id`` the message carries.
        """
        return [
            (
                message["type"],
                message.get("value") or message.get("code") or message.get("sentence_id"),
            )
            for message in self.received
        ]


@contextmanager
def connect(llm: Any, *, start: bool = True, **overrides: Any) -> Iterator[Harness]:
    """Open a session against a freshly built application.

    Args:
        llm: The model provider the session will use.
        start: Whether to send ``session.start`` and consume its two replies.
        **overrides: Settings overrides, passed to :func:`build_app`.

    Yields:
        The connected harness.
    """
    app = build_app(llm, **overrides)
    client = TestClient(app)
    try:
        with client.websocket_connect(WS_PATH) as ws:
            harness = Harness(app=app, ws=ws, llm=llm)
            if start:
                harness.start()
            yield harness
    finally:
        client.close()


def is_state(value: SessionState) -> Callable[[dict[str, Any]], bool]:
    """Build a predicate matching a ``state`` message.

    Args:
        value: The state to wait for.

    Returns:
        The predicate.
    """

    def matches(message: dict[str, Any]) -> bool:
        return message["type"] == "state" and message["value"] == value.value

    return matches


def is_type(name: str) -> Callable[[dict[str, Any]], bool]:
    """Build a predicate matching a message type.

    Args:
        name: The ``type`` to wait for.

    Returns:
        The predicate.
    """
    return lambda message: bool(message["type"] == name)


def is_barrier(message: dict[str, Any]) -> bool:
    """Report whether a message is the reply to a barrier frame.

    Args:
        message: The message to test.

    Returns:
        ``True`` for the ``bad_message`` error naming the barrier's own tag.
    """
    return message["type"] == "error" and BARRIER_TYPE in message["message"]


def types_of(messages: list[dict[str, Any]]) -> list[str]:
    """Return just the ``type`` of each message.

    Args:
        messages: The messages to reduce.

    Returns:
        The type strings, in order.
    """
    return [message["type"] for message in messages]


def only(messages: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """Filter messages by type.

    Args:
        messages: The messages to filter.
        name: The ``type`` to keep.

    Returns:
        The matching messages, in order.
    """
    return [message for message in messages if message["type"] == name]


def system_prompt(llm: FakeLLM, call: int = -1) -> str:
    """Return the system prompt sent on one recorded model call.

    Args:
        llm: The fake that recorded the call.
        call: Index into ``llm.calls``; defaults to the most recent.

    Returns:
        The system message's content.
    """
    return llm.calls[call].messages[0].content


# --------------------------------------------------------------------------- #
# TC-BE-040 -- opening a session
# --------------------------------------------------------------------------- #


def test_session_start_is_answered_with_ready_then_listening(isolated_env: Any) -> None:
    """TC-BE-040: session.ready carries the deck and precedes state listening."""
    with connect(FakeLLM(), start=False) as harness:
        harness.send(type="session.start", deck_id=DECK_ID, mode="qa")
        ready = harness.recv()
        state = harness.recv()

    assert ready["type"] == "session.ready"
    assert ready["protocol_version"] == 1
    assert ready["deck"]["id"] == DECK_ID
    assert len(ready["deck"]["slides"]) == 6
    assert ready["providers"] == {"stt": "fake_stt", "llm": "fake_llm", "tts": "fake_tts"}
    assert ready["session_id"]

    # The order is the contract: a client that renders the deck on `ready` and
    # enables the microphone on `listening` must never see them the other way.
    assert state["type"] == "state"
    assert state["value"] == SessionState.LISTENING.value
    assert state["turn_id"] == 0
    assert isinstance(state["server_ts"], float)


def test_a_message_before_session_start_is_refused(isolated_env: Any) -> None:
    """TC-BE-040: anything before session.start is a bad_message and starts no turn."""
    llm = FakeLLM()
    with connect(llm, start=False) as harness:
        harness.send(type="text.input", text="What is this deck about?")
        error = harness.recv()

        assert error["type"] == "error"
        assert error["code"] == "bad_message"
        assert "session.start" in error["message"]
        assert error["recoverable"] is True
        # No turn was started: the model was never asked anything.
        assert llm.calls == []

        # And the session still works once it is opened properly.
        ready, state = harness.start()

    assert ready["type"] == "session.ready"
    assert state["value"] == SessionState.LISTENING.value


def test_an_unknown_deck_is_refused_as_unrecoverable(isolated_env: Any) -> None:
    """TC-BE-063: session.start naming a deck that does not exist ends the session."""
    with connect(FakeLLM(), start=False) as harness:
        harness.send(type="session.start", deck_id="no_such_deck", mode="qa")
        error = harness.recv()

    assert error["type"] == "error"
    assert error["code"] == "deck_not_found"
    # Unrecoverable: there is nothing to present, so the client must not retry
    # the same session.
    assert error["recoverable"] is False
    assert DECK_ID in error["message"]


def test_an_oversized_text_frame_is_refused_before_parsing(isolated_env: Any) -> None:
    """TC-BE-065: TR-142 -- a frame past max_json_message_bytes is rejected by size."""
    with connect(FakeLLM()) as harness:
        harness.ws.send_text(json.dumps({"type": "text.input", "text": "x" * 20_000}))
        error = harness.recv()

    assert error["type"] == "error"
    assert error["code"] == "bad_message"
    assert error["message"] == "message too large"


# --------------------------------------------------------------------------- #
# TC-BE-041 to TC-BE-045 -- the text path
# --------------------------------------------------------------------------- #


def test_a_typed_question_runs_a_turn_and_returns_to_listening(isolated_env: Any) -> None:
    """TC-BE-041: F13 -- text.input drives THINKING, the answer, SPEAKING, LISTENING."""
    llm = FakeLLM(sentence_script("Sure thing.", "Let me answer that."))
    question = "How do you handle interruptions?"

    with connect(llm) as harness:
        turn = harness.ask(question)

    assert types_of(turn) == [
        "state",
        "transcript.user",
        "transcript.agent",
        "transcript.agent",
        "metrics",
        "state",
        "state",
    ]
    states = [message["value"] for message in only(turn, "state")]
    assert states == [
        SessionState.THINKING.value,
        SessionState.SPEAKING.value,
        SessionState.LISTENING.value,
    ]

    transcript = only(turn, "transcript.user")[0]
    assert transcript["text"] == question
    assert transcript["final"] is True
    assert transcript["turn_id"] == 1

    # Every message of the turn is stamped with the turn it belongs to (TR-021).
    assert {message["turn_id"] for message in turn if message["type"] in MESSAGES_WITH_TURN_ID} == {
        1
    }
    assert only(turn, "metrics")[0]["sentences"] == 2


def test_a_tool_call_moves_the_deck_before_the_answer_is_spoken(isolated_env: Any) -> None:
    """TC-BE-042: F5 -- tool.call and slide.goto precede the first transcript.agent."""
    llm = FakeLLM(
        [
            ToolCallDelta(
                call_id="call_1",
                name="go_to_slide",
                arguments={"slide_index": 4, "reason": "User asked about interruption handling"},
            ),
            *sentence_script("Two layers, actually.", "The browser stops first."),
        ]
    )

    with connect(llm) as harness:
        turn = harness.ask("How do you handle me interrupting you?")

    order = types_of(turn)
    assert order.index("tool.call") < order.index("transcript.agent")
    assert order.index("slide.goto") < order.index("transcript.agent")
    # The audience must see the slide before hearing about it. The row asks for
    # this to hold "before any audio frame"; with audio (M2) the first frame of
    # sentence zero follows its transcript, which TC-BE-044 will extend to.

    # Exactly one navigation: the keyword fallback is not consulted once the
    # model has navigated for itself (TR-062).
    assert len(only(turn, "tool.call")) == 1
    call = only(turn, "tool.call")[0]
    assert call["name"] == "go_to_slide"
    assert call["source"] == "llm"
    assert call["args"] == {
        "slide_index": 4,
        "reason": "User asked about interruption handling",
    }

    goto = only(turn, "slide.goto")[0]
    assert goto["index"] == 4
    assert goto["highlight"] is None
    assert goto["reason"] == "User asked about interruption handling"


def test_the_keyword_fallback_moves_the_deck_when_no_tool_was_called(isolated_env: Any) -> None:
    """TC-BE-043: TR-062 -- an answer about slide 2 routes there after the text."""
    llm = FakeLLM(
        sentence_script(
            "It comes down to milliseconds.",
            "The latency budget is mostly endpointing, and time to first audio is "
            "about one and a half seconds.",
        )
    )

    with connect(llm) as harness:
        turn = harness.ask("How fast are you?")

    order = types_of(turn)
    # The fallback reads the finished answer, so it can only fire after it.
    assert order.index("tool.call") > order.index("transcript.agent")
    assert order.index("slide.goto") > order.index("transcript.agent")

    call = only(turn, "tool.call")[0]
    assert call["source"] == "fallback"
    assert call["name"] == "go_to_slide"
    assert call["args"]["slide_index"] == 2
    assert only(turn, "slide.goto")[0]["index"] == 2


def test_each_sentence_is_announced_in_order_with_its_own_id(isolated_env: Any) -> None:
    """TC-BE-044: TR-033 -- transcript.agent arrives per sentence, numbered from zero.

    Partially covered until M2: the row also asks that each transcript precede
    the audio frames carrying the same ``sentence_id``, and no audio exists yet.
    """
    llm = FakeLLM(sentence_script("Sure thing.", "Let me answer that."))

    with connect(llm) as harness:
        turn = harness.ask("How does barge-in work?")

    sentences = only(turn, "transcript.agent")
    assert [message["sentence_id"] for message in sentences] == [0, 1]
    assert [message["text"] for message in sentences] == ["Sure thing.", "Let me answer that."]


def test_audio_frames_ship_alongside_the_transcript(isolated_env: Any) -> None:
    """TC-BE-045: TR-141/TR-033 -- each sentence's transcript precedes its audio.

    Ordering is the point. Captions that lag the voice look broken, so the
    transcript for a sentence is sent before its first frame, and every frame
    carries the id of the sentence it belongs to.
    """
    llm = FakeLLM(sentence_script("Sure thing.", "Let me answer that."))

    with connect(llm) as harness:
        harness.send(type="text.input", text="How does barge-in work?")
        order: list[tuple[str, int]] = []
        while True:
            frame = harness.recv_frame()
            payload = frame.get("bytes")
            if payload is not None:
                sentence_id, seq, pcm = decode_audio_frame(payload)
                order.append((f"audio:{sentence_id}", seq))
                assert 0 < len(pcm) <= FAKE_TTS_CHUNK_BYTES
                assert len(pcm) % 2 == 0, "PCM16 frames must contain whole samples"
                continue
            message = json.loads(frame["text"])
            harness.received.append(message)
            if message["type"] == "transcript.agent":
                order.append((f"text:{message['sentence_id']}", 0))
            if is_state(SessionState.LISTENING)(message):
                break

    spoken_ids = [label for label, _ in order if label.startswith("text:")]
    assert spoken_ids == ["text:0", "text:1"]
    for sentence_id in (0, 1):
        text_at = order.index((f"text:{sentence_id}", 0))
        audio_at = next(i for i, (label, _) in enumerate(order) if label == f"audio:{sentence_id}")
        assert text_at < audio_at, "the transcript must precede its own audio (TR-033)"


# --------------------------------------------------------------------------- #
# TC-BE-046 to TC-BE-050 -- interrupts
# --------------------------------------------------------------------------- #


def test_an_interrupt_cancels_the_turn_and_truncates_history(isolated_env: Any) -> None:
    """TC-BE-046: TR-022/023 -- the turn stops, agent.cancelled lands, history is cut.

    Partially covered until M2: the row says the turn is in SPEAKING, which only
    happens once audio is being played. In this milestone an in-flight turn is
    in THINKING, and the interrupt handler treats the two identically.
    """
    llm = FakeLLM(long_script(), delay_s=STEP_DELAY_S)

    with connect(llm) as harness:
        harness.send(type="text.input", text="Tell me everything.")
        harness.recv_until(is_type("transcript.agent"))

        started = time.perf_counter()
        harness.send(type="interrupt", last_completed_sentence_id=0)
        after = harness.recv_until(is_type("agent.cancelled"))
        elapsed = time.perf_counter() - started

        cancelled = after[-1]
        state = harness.recv()

        cancelled_streams = llm.cancelled

        # Ask again so the next request shows what history the model now sees.
        harness.send(type="text.input", text="Go on.")
        harness.recv_until(
            lambda message: message["type"] == "transcript.agent" and message["turn_id"] == 2
        )

    assert elapsed < CANCEL_BUDGET_S
    assert cancelled["turn_id"] == 1
    assert cancelled["truncated_at_sentence_id"] == 0
    assert state["value"] == SessionState.HEARING.value
    # Generation really stopped rather than being ignored (TR-034).
    assert cancelled_streams == 1

    replayed = [message.content for message in llm.calls[1].messages if message.role == "assistant"]
    assert replayed == ["Sentence 0. [interrupted by user]"]


def test_an_interrupt_while_listening_does_nothing(isolated_env: Any) -> None:
    """TC-BE-047: TR-024 -- an interrupt outside a turn is a no-op, not an error."""
    with connect(FakeLLM()) as harness:
        harness.send(type="interrupt", last_completed_sentence_id=3)
        quiet = harness.barrier()

        # The session is still usable afterwards.
        turn = harness.ask("What is this deck about?")

    assert quiet == []
    assert only(turn, "transcript.user")[0]["turn_id"] == 1


def test_an_interrupt_cancel_after_a_misfire_does_nothing(isolated_env: Any) -> None:
    """TC-BE-064: interrupt.cancel is accepted and changes nothing."""
    with connect(FakeLLM()) as harness:
        harness.send(type="interrupt.cancel")
        quiet = harness.barrier()

        turn = harness.ask("What is this deck about?")

    assert quiet == []
    assert only(turn, "state")[-1]["value"] == SessionState.LISTENING.value


def test_two_interrupts_in_quick_succession_cancel_the_turn_once(isolated_env: Any) -> None:
    """TC-BE-048: TR-024 -- a repeated interrupt is idempotent within the window.

    The window is scoped to the turn being interrupted, not to the clock. The
    second interrupt is absorbed by the turn it names: it cancels nothing more
    and reports nothing more, and because it carries a better sentence id it
    only refines where history was cut (TR-023).

    This case used to end by asserting that a *newly started* turn interrupted
    inside the same 500 ms was left running. That was the bug, not the contract:
    a wall-clock debounce lets the agent talk over a user who barges in twice in
    quick succession. The opposite is now asserted in
    ``test_a_barge_in_on_a_new_turn_is_honoured_inside_the_previous_window``.
    """
    llm = FakeLLM(long_script(), delay_s=STEP_DELAY_S)

    with connect(llm) as harness:
        harness.send(type="text.input", text="Tell me everything.")
        harness.recv_until(
            lambda message: message["type"] == "transcript.agent" and message["sentence_id"] == 1
        )

        harness.send(type="interrupt", last_completed_sentence_id=0)
        harness.send(type="interrupt", last_completed_sentence_id=1)
        settled = harness.barrier()
        cancelled_streams = llm.cancelled

        # Ask again so the next request shows the history the second interrupt
        # left behind.
        harness.send(type="text.input", text="Go on.")
        harness.recv_until(
            lambda message: message["type"] == "transcript.agent" and message["turn_id"] == 2
        )

    assert len(only(settled, "agent.cancelled")) == 1
    assert only(settled, "agent.cancelled")[0]["truncated_at_sentence_id"] == 0
    assert cancelled_streams == 1

    replayed = [message.content for message in llm.calls[1].messages if message.role == "assistant"]
    assert replayed == [f"Sentence 0. Sentence 1. {INTERRUPTED_MARKER}"]


def test_a_stray_interrupt_does_not_rewrite_a_turn_that_was_heard_in_full(
    isolated_env: Any,
) -> None:
    """TC-BE-217: TR-024 -- an interrupt names one turn, and only that turn.

    Keeping a finished turn addressable is what lets a barge-in during playback
    cut it honestly. It also means a stray interrupt -- a VAD misfire, a
    duplicate from a flaky connection -- arriving once the session is listening
    again could rewrite an answer the room heard in full, and tell the model it
    was cut off when it was not. The window is scoped to the turn that was
    actually cancelled, so this one changes nothing.
    """
    llm = FakeLLM(short_script(), delay_s=SHORT_STEP_DELAY_S)

    with connect(llm) as harness:
        harness.send(type="text.input", text="Tell me everything.")
        harness.recv_until(is_type("transcript.agent"))
        interrupted = time.monotonic()
        harness.send(type="interrupt", last_completed_sentence_id=0)
        harness.recv_until(is_state(SessionState.HEARING))

        # A second turn, heard in full, inside the window the first one opened.
        harness.ask("And what else?")
        harness.send(type="interrupt", last_completed_sentence_id=0)
        assert harness.barrier() == []
        elapsed = time.monotonic() - interrupted

        harness.ask("One more.")

    if elapsed >= INTERRUPT_DEBOUNCE_S:
        pytest.skip("machine too slow to land the stray interrupt inside the window")
    answers = [message.content for message in llm.calls[2].messages if message.role == "assistant"]
    assert answers[-1] == " ".join(f"Sentence {index}." for index in range(SHORT_SCRIPT_SENTENCES))
    assert INTERRUPTED_MARKER not in answers[-1]


def test_a_barge_in_on_a_new_turn_is_honoured_inside_the_previous_window(
    isolated_env: Any,
) -> None:
    """TC-BE-203: TR-024 -- the debounce belongs to a turn, not to the wall clock.

    Two questions asked in quick succession, each interrupted, is ordinary
    impatience. Keying the debounce on time alone dropped the second barge-in
    and let the agent talk over the user for the rest of that turn, which is the
    one failure barge-in exists to prevent.
    """
    llm = FakeLLM(long_script(), delay_s=STEP_DELAY_S)

    with connect(llm) as harness:
        harness.send(type="text.input", text="Tell me everything.")
        harness.recv_until(is_type("transcript.agent"))
        first_sent = time.monotonic()
        harness.send(type="interrupt", last_completed_sentence_id=0)
        harness.recv_until(is_state(SessionState.HEARING))

        harness.send(type="text.input", text="Again please.")
        harness.recv_until(is_type("transcript.agent"))
        harness.send(type="interrupt", last_completed_sentence_id=0)
        second = harness.recv_until(is_state(SessionState.HEARING))
        elapsed = time.monotonic() - first_sent
        cancelled_streams = llm.cancelled

    if elapsed >= INTERRUPT_DEBOUNCE_S:
        pytest.skip("machine too slow to land the second interrupt inside the debounce window")
    cancelled = only(second, "agent.cancelled")
    assert len(cancelled) == 1
    assert cancelled[0]["turn_id"] == 2
    # Both streams really stopped; the second turn was not merely ignored.
    assert cancelled_streams == 2


def test_an_interrupt_announces_the_interrupted_state_before_hearing(isolated_env: Any) -> None:
    """TC-BE-204: PRD §7 -- INTERRUPTED is a state the client is told about.

    The orb renders it as the flash that acknowledges a barge-in, and PRD §7
    puts it on the path from SPEAKING back to HEARING. It was in the enum and in
    the frontend, and the server never sent it, so the contract and the code
    disagreed.
    """
    llm = FakeLLM(long_script(), delay_s=STEP_DELAY_S)

    with connect(llm) as harness:
        harness.send(type="text.input", text="Tell me everything.")
        harness.recv_until(is_type("transcript.agent"))
        harness.send(type="interrupt", last_completed_sentence_id=0)
        after = harness.recv_until(is_state(SessionState.HEARING))

    assert [message["value"] for message in only(after, "state")] == [
        SessionState.INTERRUPTED.value,
        SessionState.HEARING.value,
    ]
    # The flash goes out before the cancellation is reported, so the audience
    # sees the agent give way at the moment they spoke rather than once the
    # model stream has finished closing.
    order = types_of(after)
    assert order.index("state") < order.index("agent.cancelled")
    assert only(after, "state")[0]["turn_id"] == 1


def test_a_follow_up_interrupt_refines_the_cut_of_the_turn_it_names(isolated_env: Any) -> None:
    """TC-BE-205: TR-023 -- the precise truncation point still lands after speech.start.

    ``speech.start`` cancels with no sentence id at all, because VAD fires
    before the client has read its playback queue; the explicit ``interrupt``
    that follows carries the id that says what the room actually heard. That
    follow-up always arrives with the session already in HEARING, where the
    "nothing to interrupt" guard used to drop it -- so the only accurate cut the
    client ever sends was the one the server always threw away.
    """
    llm = FakeLLM(long_script(), delay_s=STEP_DELAY_S)

    with connect(llm) as harness:
        harness.send(type="text.input", text="Tell me everything.")
        harness.recv_until(
            lambda message: message["type"] == "transcript.agent" and message["sentence_id"] == 1
        )
        harness.send(type="speech.start")
        harness.recv_until(is_state(SessionState.HEARING))

        harness.send(type="interrupt", last_completed_sentence_id=1)
        # Refining is silent: the turn was already reported as cancelled, and a
        # second agent.cancelled would show the user a second interrupt chip.
        assert harness.barrier() == []

        harness.send(type="text.input", text="Go on.")
        harness.recv_until(
            lambda message: message["type"] == "transcript.agent" and message["turn_id"] == 2
        )

    replayed = [message.content for message in llm.calls[1].messages if message.role == "assistant"]
    assert replayed == [f"Sentence 0. Sentence 1. {INTERRUPTED_MARKER}"]


def test_speech_onset_before_any_sentence_truncates_with_none(isolated_env: Any) -> None:
    """TC-BE-049: TR-023 -- speech.start during THINKING cancels and truncates at None."""
    llm = FakeLLM(long_script(), delay_s=STEP_DELAY_S)

    with connect(llm) as harness:
        harness.send(type="text.input", text="Tell me everything.")
        harness.recv_until(is_type("transcript.user"))
        harness.send(type="speech.start")
        after = harness.recv_until(is_type("agent.cancelled"))
        state = harness.recv()

        harness.send(type="text.input", text="Sorry, go on.")
        harness.recv_until(
            lambda message: message["type"] == "transcript.agent" and message["turn_id"] == 2
        )

    cancelled = after[-1]
    assert cancelled["turn_id"] == 1
    # Nothing was heard, so nothing may be claimed as said (TR-051).
    assert cancelled["truncated_at_sentence_id"] is None
    assert state["value"] == SessionState.HEARING.value

    replayed = [message.content for message in llm.calls[1].messages if message.role == "assistant"]
    assert replayed == ["[interrupted by user before speaking]"]


def test_speech_onset_while_listening_only_moves_to_hearing(isolated_env: Any) -> None:
    """TC-BE-049: TR-023 -- outside a turn, speech.start is turn-taking, not barge-in."""
    llm = FakeLLM()

    with connect(llm) as harness:
        harness.send(type="speech.start")
        moved = harness.barrier()

    assert types_of(moved) == ["state"]
    assert moved[0]["value"] == SessionState.HEARING.value
    # Nothing was cancelled, because nothing was running.
    assert llm.calls == []


def test_a_cancelled_turn_sends_nothing_further_under_its_own_turn_id(isolated_env: Any) -> None:
    """TC-BE-050: TR-021 -- no output of turn n follows its agent.cancelled.

    ``state`` is the documented exception: the transition into HEARING reports
    the turn the session is leaving, and it is the very message a client uses as
    its clock. Everything a *turn* produces -- transcripts, tool calls, metrics
    -- must stop.
    """
    llm = FakeLLM(long_script(), delay_s=STEP_DELAY_S)

    with connect(llm) as harness:
        harness.send(type="text.input", text="Tell me everything.")
        harness.recv_until(is_type("transcript.agent"))
        harness.send(type="interrupt", last_completed_sentence_id=0)
        harness.recv_until(is_type("agent.cancelled"))

        harness.send(type="text.input", text="Different question.")
        later = harness.recv_until(
            lambda message: message["type"] == "transcript.agent" and message["turn_id"] == 2
        )

    stale = [
        message
        for message in later
        if message["type"] in MESSAGES_WITH_TURN_ID and message["turn_id"] == 1
    ]
    assert stale == []
    assert only(later, "transcript.user")[0]["turn_id"] == 2


def test_a_new_turn_cancels_the_one_still_running(isolated_env: Any) -> None:
    """TC-BE-207: TR-022 -- starting a turn cancels and awaits the previous one.

    A user who asks a second question without waiting has not interrupted
    anything -- there is no ``interrupt`` frame -- so the only thing that can
    stop the first turn is ``start_turn`` itself. ``llm.cancelled`` is the
    assertion that kills the mutation: with the cancel removed, both turns write
    to the same history and the first keeps streaming under its own turn id.
    """
    llm = FakeLLM(long_script(), delay_s=STEP_DELAY_S)

    with connect(llm) as harness:
        harness.send(type="text.input", text="Tell me everything.")
        harness.recv_until(is_type("transcript.agent"))

        harness.send(type="text.input", text="Actually, something else.")
        opening = harness.recv_until(is_state(SessionState.THINKING))
        following = harness.recv_until(
            lambda message: message["type"] == "transcript.agent" and message["turn_id"] == 2
        )
        cancelled_streams = llm.cancelled

    assert cancelled_streams == 1
    assert only(opening, "state")[-1]["turn_id"] == 2
    # The first turn was finished unwinding before the second one's id was even
    # allocated, so nothing of it can appear from here on.
    stale = [
        message
        for message in following
        if message["type"] in MESSAGES_WITH_TURN_ID and message["turn_id"] == 1
    ]
    assert stale == []


# --------------------------------------------------------------------------- #
# TC-BE-051 to TC-BE-054 -- failure and empty input
# --------------------------------------------------------------------------- #


def test_a_turn_that_never_finishes_times_out_and_returns_to_listening(
    isolated_env: Any,
) -> None:
    """TC-BE-051: TR-025 -- the watchdog fires, reports turn_timeout, and recovers.

    The timeout is configured down to milliseconds rather than waiting for the
    production twenty seconds; the model is scripted to take far longer than it.
    """
    llm = FakeLLM(long_script(), delay_s=1.0)

    with connect(llm, turn_timeout_s=0.05) as harness:
        turn = harness.ask("Tell me everything.")

        # The session is usable again straight away.
        harness.send(type="interrupt", last_completed_sentence_id=None)
        assert harness.barrier() == []

    error = only(turn, "error")[0]
    assert error["code"] == "turn_timeout"
    assert error["recoverable"] is True
    assert types_of(turn) == ["state", "transcript.user", "error", "state"]
    assert only(turn, "state")[-1]["value"] == SessionState.LISTENING.value
    # The watchdog cancelled the model stream rather than leaving it running.
    assert llm.cancelled == 1


TIMEOUT_WITH_ROOM_S = 0.4
"""Watchdog long enough to hear several sentences of a 30-sentence script.

Eight times :data:`STEP_DELAY_S`, so the turn has certainly spoken by the time
the watchdog fires, and far short of the 1.5 s the whole script needs.
"""


def test_a_timed_out_turn_keeps_the_sentences_the_room_already_heard(
    isolated_env: Any,
) -> None:
    """TC-BE-208: TR-051 -- the watchdog truncates the answer, it does not delete it.

    The sentences already sent were heard. Abandoning the turn without cutting
    history left the model with no record of having spoken at all, so the next
    question was answered from scratch -- and ``begin_assistant_turn`` logged
    ``history.pending_turn_discarded`` about exactly that.
    """
    llm = FakeLLM(long_script(), delay_s=STEP_DELAY_S)

    with connect(llm, turn_timeout_s=TIMEOUT_WITH_ROOM_S) as harness:
        turn = harness.ask("Tell me everything.")
        harness.send(type="text.input", text="Go on.")
        harness.recv_until(
            lambda message: message["type"] == "transcript.agent" and message["turn_id"] == 2
        )

    assert only(turn, "error")[0]["code"] == "turn_timeout"

    replayed = [message.content for message in llm.calls[1].messages if message.role == "assistant"]
    assert len(replayed) == 1
    assert replayed[0].startswith("Sentence 0.")
    assert replayed[0].endswith(INTERRUPTED_MARKER)


def test_a_provider_failure_keeps_the_sentences_the_room_already_heard(
    isolated_env: Any,
) -> None:
    """TC-BE-209: TR-051 -- a rate limit mid-answer must not unsay what was said.

    The realistic failure on a free tier: one sentence is out of the speakers
    when the next request is refused. The user heard it, so the model has to
    know it said it.
    """
    llm = FailingMidAnswerLLM(
        [TokenDelta(text="One. ")],
        ProviderError("groq_llm", "rate limited", retryable=True, retry_after=2.0),
    )

    with connect(llm) as harness:
        turn = harness.ask("Tell me everything.")
        harness.ask("Try again.")

    assert only(turn, "error")[0]["code"] == "rate_limited"
    assert only(turn, "transcript.agent")[0]["text"] == "One."

    replayed = [message.content for message in llm.calls[1].messages if message.role == "assistant"]
    assert replayed == [f"One. {INTERRUPTED_MARKER}"]


def test_an_unexpected_failure_is_reported_and_the_session_recovers(isolated_env: Any) -> None:
    """TC-BE-210: TR-025 -- a bug in the turn must not wedge the client in THINKING.

    Only ``CancelledError``, ``TimeoutError`` and ``ProviderError`` were
    handled, so anything else died inside the turn task: no error frame, no log,
    the exception never retrieved, and the session stuck in THINKING until the
    socket closed. Real sources exist today -- ``json.dumps`` on model-authored
    arguments, a sentence recorded against a turn that is no longer pending.
    """
    llm = ExplodingLLM()

    with structlog.testing.capture_logs() as logged, connect(llm) as harness:
        turn = harness.ask("What is this deck about?")
        # Still listening: the user may simply ask again.
        follow_up = harness.ask("Try again.")

    failures = [entry for entry in logged if entry["event"] == "turn.failed"]
    # Logged with the traceback, not as a bare message: a bug nobody can see is
    # a bug nobody fixes.
    assert [entry["log_level"] for entry in failures] == ["error", "error"]
    assert all(entry["exc_info"] for entry in failures)

    error = only(turn, "error")[0]
    assert error["recoverable"] is True
    assert error["code"] == "internal_error"
    assert types_of(turn) == ["state", "transcript.user", "error", "state"]
    assert only(turn, "state")[-1]["value"] == SessionState.LISTENING.value
    assert llm.calls == 2
    assert only(follow_up, "transcript.user")[0]["turn_id"] == 2


def test_a_provider_failure_is_reported_as_recoverable(isolated_env: Any) -> None:
    """TC-BE-052: TR-170 -- a failing provider yields one error and state LISTENING.

    Partially covered until M3: the row names ``FakeSTT`` and ``stt_failed``,
    and there is no transcription stage yet. The failure is injected at the only
    provider a turn currently uses, which exercises the same handler.
    """
    llm = FailingLLM()

    with connect(llm) as harness:
        turn = harness.ask("What is this deck about?")
        # Still listening: the user may simply ask again.
        follow_up = harness.ask("Try again.")

    error = only(turn, "error")[0]
    assert error["code"] == "llm_failed"
    assert error["recoverable"] is True
    assert error["message"] == "upstream refused"
    assert types_of(turn) == ["state", "transcript.user", "error", "state"]
    assert only(turn, "state")[-1]["value"] == SessionState.LISTENING.value
    assert llm.calls == 2
    assert only(follow_up, "transcript.user")[0]["turn_id"] == 2


@pytest.mark.parametrize(
    "text",
    ["   ", "Thank you.", "..."],
    ids=["whitespace", "denylist", "punctuation"],
)
def test_an_empty_or_filler_question_is_dropped_without_an_answer(
    isolated_env: Any, text: str
) -> None:
    """TC-BE-053/054: TR-172, F4 -- nothing is transcribed, no model call, back to LISTENING.

    Partially covered until M3: both rows describe what STT returns for silence.
    The denylist they name lives in ``run_turn`` and is reached identically from
    ``text.input``, which is what is driven here.
    """
    llm = FakeLLM()

    with connect(llm) as harness:
        turn = harness.ask(text)

    assert types_of(turn) == ["state", "metrics", "state"]
    assert only(turn, "transcript.user") == []
    assert only(turn, "transcript.agent") == []
    assert only(turn, "state")[-1]["value"] == SessionState.LISTENING.value
    # The model was never asked, which is the point: silence is not a question.
    assert llm.calls == []
    assert only(turn, "metrics")[0]["sentences"] == 0


# --------------------------------------------------------------------------- #
# TC-BE-055 to TC-BE-058 -- frames, navigation, disconnect
# --------------------------------------------------------------------------- #


def test_a_binary_frame_is_refused_until_audio_lands(isolated_env: Any) -> None:
    """TC-BE-055: TR-140 -- a binary frame is a protocol violation in this milestone."""
    with connect(FakeLLM()) as harness:
        harness.ws.send_bytes(b"\x00\x01" * 16)
        error = harness.recv()

        # Refusing the frame does not end the session.
        turn = harness.ask("What is this deck about?")

    assert error["type"] == "error"
    assert error["code"] == "unexpected_binary"
    assert error["recoverable"] is True
    assert "M3" in error["message"]
    assert only(turn, "transcript.user")[0]["turn_id"] == 1


@pytest.mark.skip(reason="utterance size limit arrives with milestone M3 (audio in, TR-182)")
def test_an_oversized_utterance_closes_the_socket(isolated_env: Any) -> None:
    """TC-BE-056: TR-182 -- a 3 MB binary frame must close the socket with 1009.

    Deliberately not covered: nothing may upload an utterance before M3, so
    ``max_utterance_bytes`` has no enforcement point yet and every binary frame
    is refused outright by TC-BE-055. Implement with the capture path.
    """


def test_manual_navigation_is_told_to_the_model(isolated_env: Any) -> None:
    """TC-BE-057: F9/TR-063 -- a system note is recorded and the next prompt follows."""
    llm = FakeLLM(sentence_script("Two layers, actually."))

    with connect(llm) as harness:
        harness.send(type="slide.changed", index=4, source="user")
        # The agent is deliberately not interrupted and says nothing about it.
        assert harness.barrier() == []
        harness.ask("What does this slide say?")

    notes = [
        message.content
        for message in llm.calls[0].messages
        if message.role == "system" and message.content.startswith("[User manually moved")
    ]
    assert notes == ["[User manually moved to slide 4: Barge-in: Interrupting Gracefully]"]

    prompt = system_prompt(llm)
    assert "SLIDE 4" in prompt
    # The prompt carries the notes of the slide the room is looking at, and only
    # those: slide one's notes are no longer embedded (TR-071).
    assert "Interruption is handled in two tiers." in prompt
    assert "This is Dynamic Voice Deck" not in prompt


def test_disconnecting_mid_turn_cancels_the_task_and_releases_the_session(
    isolated_env: Any,
) -> None:
    """TC-BE-058: TR-026 -- the socket closing cancels the turn and drops the session."""
    llm = FakeLLM(long_script(), delay_s=STEP_DELAY_S)
    app = build_app(llm)
    client = TestClient(app)
    manager: SessionManager = app.state.app_state.sessions

    with client.websocket_connect(WS_PATH) as ws:
        harness = Harness(app=app, ws=ws, llm=llm)
        harness.start()
        harness.send(type="text.input", text="Tell me everything.")
        harness.recv_until(is_type("transcript.agent"))

        assert len(manager) == 1
        ws.close(1000)

        deadline = time.monotonic() + RECEIVE_TIMEOUT_S
        while len(manager) and time.monotonic() < deadline:
            time.sleep(0.01)

    client.close()

    assert len(manager) == 0
    # The turn was cancelled, not left running against a socket nobody reads.
    assert llm.cancelled == 1


# --------------------------------------------------------------------------- #
# TC-BE-059 to TC-BE-062 -- control and playback
# --------------------------------------------------------------------------- #


def test_start_presentation_switches_mode_and_opens_a_turn(isolated_env: Any) -> None:
    """TC-BE-059: F8 -- control{start_presentation} presents from the cursor.

    Partially covered until M4: the row also asks that a ``go_to_slide(1)``
    advance the presentation cursor. Nothing calls ``advance_cursor`` in this
    milestone -- present mode's unattended walkthrough is M4 -- so the cursor
    stays where it is and only the mode switch and the opening turn are checked.
    """
    llm = FakeLLM(sentence_script("Welcome to the deck."))

    with connect(llm) as harness:
        harness.send(type="control", action="start_presentation")
        turn = harness.recv_until(is_state(SessionState.LISTENING))

    assert only(turn, "transcript.user")[0]["text"] == (
        "Please start presenting from the beginning."
    )
    prompt = system_prompt(llm)
    assert "mode: present" in prompt
    assert "presentation_cursor: 1" in prompt


def test_pause_cancels_the_turn_and_returns_to_listening(isolated_env: Any) -> None:
    """TC-BE-061: F8 -- control{pause} stops the agent without an agent.cancelled."""
    llm = FakeLLM(long_script(), delay_s=STEP_DELAY_S)

    with connect(llm) as harness:
        harness.send(type="text.input", text="Tell me everything.")
        harness.recv_until(is_type("transcript.agent"))
        harness.send(type="control", action="pause")
        settled = harness.barrier()
        cancelled_streams = llm.cancelled

    assert only(settled, "agent.cancelled") == []
    assert only(settled, "state")[-1]["value"] == SessionState.LISTENING.value
    assert cancelled_streams == 1


def test_pause_keeps_the_sentences_the_room_already_heard(isolated_env: Any) -> None:
    """TC-BE-211: TR-051 -- pausing stops the agent without unsaying it.

    ``control{pause}`` is not a barge-in, so it sends no ``agent.cancelled`` --
    but the room still heard whatever was already spoken, and resuming with a
    model that believes it never spoke makes it start the answer again.
    """
    llm = FakeLLM(long_script(), delay_s=STEP_DELAY_S)

    with connect(llm) as harness:
        harness.send(type="text.input", text="Tell me everything.")
        harness.recv_until(is_type("transcript.agent"))
        harness.send(type="control", action="pause")
        harness.recv_until(is_state(SessionState.LISTENING))

        harness.send(type="text.input", text="Go on.")
        harness.recv_until(
            lambda message: message["type"] == "transcript.agent" and message["turn_id"] == 2
        )

    replayed = [message.content for message in llm.calls[1].messages if message.role == "assistant"]
    assert len(replayed) == 1
    assert replayed[0].startswith("Sentence 0.")
    assert replayed[0].endswith(INTERRUPTED_MARKER)


def test_playback_progress_for_another_turn_is_ignored(isolated_env: Any) -> None:
    """TC-BE-062: progress reported against a stale turn changes nothing.

    Partially covered until M2: ``playback.progress`` is what drives the return
    to LISTENING once the client is actually playing audio. Without audio the
    turn has already returned by the time any progress could arrive, so what is
    asserted here is the guard: a report for a turn that is not current, or for
    a session that is not speaking, is silently dropped.
    """
    with connect(FakeLLM()) as harness:
        harness.ask("What is this deck about?")
        harness.send(type="playback.progress", turn_id=99, sentence_id=0)
        harness.send(type="playback.progress", turn_id=1, sentence_id=0)
        quiet = harness.barrier()

    assert quiet == []


# --------------------------------------------------------------------------- #
# TC-BE-206 to TC-BE-215 -- states a real socket cannot reach in this milestone
# --------------------------------------------------------------------------- #
#
# Everything above drives the session over a WebSocket, which is the right way
# to test a protocol. Three behaviours cannot be reached that way in a text-only
# milestone, and each of them is load-bearing for barge-in in M2/M3:
#
# * ``playback.progress`` only does anything while the session is SPEAKING with
#   no turn in flight, and a text turn passes through SPEAKING and out to
#   LISTENING in the same breath. With audio this handler is what ends every
#   turn, and today it could be replaced by ``return`` with nothing noticing.
# * the window in which a finishing turn makes its last two transitions is
#   microseconds wide, so a second turn cannot be started inside it on purpose
#   from the far end of a socket.
# * an interrupt landing between a turn being created and that turn reaching the
#   model finds no assistant entry in history to cut.
#
# They are driven through ``Session.dispatch`` -- the same entry point
# ``session_endpoint`` hands every frame it reads to -- with a stand-in socket,
# and they read and write only the documented public attributes ``state`` and
# ``turn_id``. No private method is called.


class RecordingSocket:
    """Stands in for the WebSocket, keeping the messages the session sent.

    Attributes:
        sent: Every message sent, decoded, in order.
        audio: Every binary audio frame sent, in order.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.audio: list[bytes] = []

    async def send_bytes(self, payload: bytes) -> None:
        """Record one outbound audio frame.

        Args:
            payload: The framed PCM16 the session sent.
        """
        self.audio.append(payload)

    async def send_text(self, raw: str) -> None:
        """Record one outbound frame.

        Args:
            raw: The JSON the session serialised.
        """
        message: dict[str, Any] = json.loads(raw)
        self.sent.append(message)

    def states(self) -> list[tuple[str, int]]:
        """Return the transitions sent so far.

        Returns:
            One ``(value, turn_id)`` pair per ``state`` message, in order.
        """
        return [
            (message["value"], message["turn_id"])
            for message in self.sent
            if message["type"] == "state"
        ]


class GatedSocket(RecordingSocket):
    """A socket that parks the session inside one state message.

    Holding the server still is the only way to make a microsecond-wide window
    wide enough to aim at: the send of the first matching ``state`` blocks until
    the test releases it, so a second turn can be started while the first is
    provably still inside its trailing transitions.

    Args:
        gate_on: The state whose first send is held.

    Attributes:
        reached: Set once the session is parked.
        release: Set by the test to let the parked send finish.
        cancelled_in_send: Whether the parked turn was cancelled while waiting,
            which is what proves it was still cancellable.
    """

    def __init__(self, gate_on: SessionState) -> None:
        super().__init__()
        self._gate_on = gate_on
        self._open = True
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled_in_send = False

    async def send_text(self, raw: str) -> None:
        """Record the frame, parking on the first one the gate names.

        Args:
            raw: The JSON the session serialised.

        Raises:
            asyncio.CancelledError: If the parked turn is cancelled, re-raised
                after recording that it happened.
        """
        await super().send_text(raw)
        message = self.sent[-1]
        gated = message["type"] == "state" and message["value"] == self._gate_on.value
        if not (self._open and gated):
            return
        self._open = False  # the first such message only, so later turns run free
        self.reached.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled_in_send = True
            raise


def make_session(websocket: Any, llm: Any = None) -> Session:
    """Build a session outside any socket, for the manager's own tests.

    Args:
        websocket: Stand-in for the connection; nothing here sends on it.
        llm: Model provider for the session; defaults to a fresh
            :class:`~tests.fakes.FakeLLM`.

    Returns:
        A session in its initial state.
    """
    return Session(
        websocket,
        settings=Settings(),
        providers=Providers(stt=FakeSTT(), llm=llm or FakeLLM(), tts=FakeTTS()),
        decks=DeckRepository(),
        prompts=PromptBuilder(),
    )


async def started_session(socket: RecordingSocket, llm: Any = None) -> Session:
    """Open a session on the shipped deck, dispatching the opening message.

    Args:
        socket: The stand-in socket; its record is cleared once the session is
            admitted, so a test sees only what it caused.
        llm: Model provider for the session.

    Returns:
        A session in LISTENING, ready for the frame under test.
    """
    session = make_session(socket, llm=llm)
    await session.dispatch(SessionStartMsg(deck_id=DECK_ID))
    socket.sent.clear()
    return session


def running_turn(turn_id: int) -> asyncio.Task[None]:
    """Return the task running one turn, found by the name the session gave it.

    Args:
        turn_id: The turn whose task is wanted.

    Returns:
        The single matching task.
    """
    name = f"turn-{turn_id}"
    tasks = [task for task in asyncio.all_tasks() if task.get_name() == name]
    assert len(tasks) == 1, f"expected exactly one {name} task, found {len(tasks)}"
    return tasks[0]


async def test_playback_progress_ends_a_turn_whose_last_sentence_finished(
    isolated_env: Any,
) -> None:
    """TC-BE-212: TR-020 -- playback completion is client-truth and ends the turn.

    Only the browser knows when a sample reached the speakers, so this frame is
    what returns an audio turn to LISTENING (PRD §7). The whole handler could be
    replaced by ``return`` without a single test noticing, which is what this
    and the two cases below fix.
    """
    socket = RecordingSocket()
    session = await started_session(socket)
    session.turn_id = 3
    session.state = SessionState.SPEAKING

    await session.dispatch(PlaybackProgressMsg(turn_id=3, sentence_id=1))

    assert session.state is SessionState.LISTENING
    assert socket.states() == [(SessionState.LISTENING.value, 3)]


@pytest.mark.parametrize(
    ("state", "reported_turn"),
    [
        (SessionState.SPEAKING, 2),
        (SessionState.THINKING, 3),
        (SessionState.LISTENING, 3),
    ],
    ids=["stale-turn", "still-thinking", "already-listening"],
)
async def test_playback_progress_is_ignored_unless_the_current_turn_is_speaking(
    isolated_env: Any,
    state: SessionState,
    reported_turn: int,
) -> None:
    """TC-BE-213: TR-131 -- progress from an interrupted or unfinished turn changes nothing."""
    socket = RecordingSocket()
    session = await started_session(socket)
    session.turn_id = 3
    session.state = state

    await session.dispatch(PlaybackProgressMsg(turn_id=reported_turn, sentence_id=0))

    assert session.state is state
    assert socket.sent == []


async def test_playback_progress_while_the_turn_is_still_running_is_ignored(
    isolated_env: Any,
) -> None:
    """TC-BE-214: a sentence the client finished is not the end of the answer.

    While the turn task is alive the model may still be generating, so a report
    about sentence zero says nothing about whether the turn is over.
    """
    socket = RecordingSocket()
    session = await started_session(socket, llm=FakeLLM(long_script(), delay_s=STEP_DELAY_S))

    await session.dispatch(TextInputMsg(text="Tell me everything."))
    # The client is playing sentence zero while the answer runs on.
    session.state = SessionState.SPEAKING
    socket.sent.clear()

    await session.dispatch(PlaybackProgressMsg(turn_id=session.turn_id, sentence_id=0))

    assert session.state is SessionState.SPEAKING
    assert socket.sent == []

    await session.close()


async def test_a_turn_starting_as_another_finishes_cancels_it_first(isolated_env: Any) -> None:
    """TC-BE-215: TR-021/022 -- a finishing turn must not outlive its own turn id.

    ``_run_turn`` closes with two state transitions, and it used to drop its
    handle on itself before making them. A turn started in that window found
    nothing to cancel, and the turn that was finishing woke up to stamp the new
    turn's id on its own trailing ``listening`` -- dragging the session out of
    the THINKING the new turn had just entered, and telling the client the
    answer it is waiting for is already over.
    """
    socket = GatedSocket(gate_on=SessionState.SPEAKING)
    session = await started_session(socket, llm=FakeLLM(sentence_script("Sure thing.")))

    await session.dispatch(TextInputMsg(text="What is this deck about?"))
    first = running_turn(1)
    await asyncio.wait_for(socket.reached.wait(), timeout=RECEIVE_TIMEOUT_S)

    # A second question, asked while the first turn is parked mid-transition.
    await asyncio.wait_for(
        session.dispatch(TextInputMsg(text="Actually, something else.")),
        timeout=RECEIVE_TIMEOUT_S,
    )
    second = running_turn(2)

    socket.release.set()
    for task in (first, second):
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=RECEIVE_TIMEOUT_S)
    await session.close()

    # The parked turn was cancelled where it stood, rather than left to wake up.
    assert socket.cancelled_in_send is True
    assert socket.states() == [
        (SessionState.THINKING.value, 1),
        (SessionState.SPEAKING.value, 1),
        (SessionState.THINKING.value, 2),
        (SessionState.SPEAKING.value, 2),
        (SessionState.LISTENING.value, 2),
    ]


@pytest.mark.parametrize(
    "last_completed",
    [None, 2],
    ids=["nothing-heard", "client-claims-two-sentences"],
)
async def test_an_interrupt_before_the_answer_reached_history_claims_no_cut(
    isolated_env: Any,
    last_completed: int | None,
) -> None:
    """TC-BE-216: agent.cancelled may only report a truncation that happened.

    An interrupt can land after the turn was created and before it reached the
    model, when history holds no assistant entry for it to rewrite. Reporting a
    sentence id then tells the client, and the event log, that history was cut
    at a sentence that was never recorded.
    """
    socket = RecordingSocket()
    session = await started_session(socket)
    session.turn_id = 1
    session.state = SessionState.THINKING

    await session.dispatch(InterruptMsg(last_completed_sentence_id=last_completed))

    cancelled = [message for message in socket.sent if message["type"] == "agent.cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["truncated_at_sentence_id"] is None
    assert session.state is SessionState.HEARING


async def test_the_session_manager_releases_every_session(isolated_env: Any) -> None:
    """TC-BE-066: TR-026 -- remove drops one session and close_all empties the registry."""
    manager = SessionManager()
    first = make_session(object())
    second = make_session(object())
    manager.add(first)
    manager.add(second)

    assert len(manager) == 2

    await manager.remove(first)

    assert len(manager) == 1

    # Shutdown closes whatever is left, and removing an already-removed session
    # is not an error: a socket can close while the process is shutting down.
    await manager.close_all()
    await manager.remove(second)

    assert len(manager) == 0


@pytest.mark.skip(reason="synthesis arrives with milestone M2 (audio out, TR-173)")
def test_a_failing_sentence_is_skipped_and_logged(isolated_env: Any) -> None:
    """TC-BE-060: TR-173 -- a TTS failure on one sentence must not kill the turn.

    Deliberately not covered: no sentence is synthesised in this milestone, so
    there is no failure to inject. Implement with the Kokoro provider.
    """
