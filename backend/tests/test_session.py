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

Rows that name STT or TTS are covered two ways. Where the behaviour is
identical whichever way the question arrived, the cheaper ``text.input`` path
drives it (``TC-BE-053``, ``TC-BE-054``); where the audio path is the point,
the fakes are steered into the failure -- an utterance past the cap
(``TC-BE-056``) or a sentence whose synthesis refuses (``TC-BE-060``).
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
from app.providers.fallback import FallbackLLM
from app.providers.groq_stt import PROVIDER_NAME as GROQ_STT_NAME
from app.session import INTERRUPT_DEBOUNCE_S, Session, SessionManager
from fastapi import FastAPI, WebSocketDisconnect
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
to completion would take -- which is the regression it exists to catch. The
number that describes the product is measured against the real providers, not
here: 1.9 ms from the interrupt leaving the client to ``agent.cancelled``
arriving, recorded in the engineering log for 2026-09-11.
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


def build_app(
    llm: Any, *, tts_fail_on: int | None = None, stt: Any | None = None, **overrides: Any
) -> FastAPI:
    """Build an application wired to fake providers, without running startup.

    Args:
        llm: The model provider this application's sessions will use.
        tts_fail_on: Index of a sentence whose synthesis should fail, so the
            skip-and-continue behaviour of TR-173 can be exercised.
        stt: Transcriber to use, for the tests that drive the audio path;
            defaults to one that transcribes anything to a fixed sentence.
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
        providers=Providers(stt=stt or FakeSTT(), llm=llm, tts=FakeTTS(fail_on=tts_fail_on)),
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
    # Every frame in arrival order, JSON and binary together: ``("state", None)`` for a message,
    # ``("audio", sentence_id)`` for a wire frame. The two lists above lose the interleaving, and
    # the interleaving is the contract for anything asserting that a caption precedes its sound.
    order: list[tuple[str, int | None]] = field(default_factory=list)

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
            WebSocketDisconnect: If the server closed the socket. The raw
                transport reports a close as a frame rather than an exception,
                so it is re-raised here the way a browser would see it: as the
                end of the socket, carrying the close code.
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
        if frame.get("type") == "websocket.close":
            raise WebSocketDisconnect(frame.get("code", 1000), frame.get("reason"))
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
                sentence_id, _, _ = decode_audio_frame(payload)
                self.order.append(("audio", sentence_id))
                continue
            assert "text" in frame, f"expected a text or binary frame, got {frame!r}"
            message: dict[str, Any] = json.loads(frame["text"])
            self.received.append(message)
            self.order.append((message["type"], message.get("sentence_id")))
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

    def say(self, audio: bytes = b"\x00\x01" * 8_000, *, duration_ms: int = 1_000) -> list[dict]:
        """Speak: announce the end of an utterance, upload it, and read the turn.

        This is the audio path a browser drives, and the only difference from
        :meth:`ask` is how the words arrive. What the audio contains is
        irrelevant -- the transcriber is a fake -- but it is sent as real bytes
        so the size guard and the ``expecting binary`` handshake are exercised.

        Args:
            audio: The utterance's samples.
            duration_ms: What the client claims the utterance lasted.

        Returns:
            Every message up to the ``state listening`` that closes the turn.
        """
        self.send(type="speech.end", duration_ms=duration_ms)
        self.ws.send_bytes(audio)
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
def connect(
    llm: Any,
    *,
    start: bool = True,
    tts_fail_on: int | None = None,
    stt: Any | None = None,
    **overrides: Any,
) -> Iterator[Harness]:
    """Open a session against a freshly built application.

    Args:
        llm: The model provider the session will use.
        start: Whether to send ``session.start`` and consume its two replies.
        **overrides: Settings overrides, passed to :func:`build_app`.

    Yields:
        The connected harness.
    """
    app = build_app(llm, tts_fail_on=tts_fail_on, stt=stt, **overrides)
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
    """TC-BE-041: F13 -- text.input drives THINKING, SPEAKING with the sound, LISTENING.

    SPEAKING lands after the first sentence, not after the last: the state
    changes when audio actually starts, because that is when there is something
    for an interrupt to cut short.
    """
    llm = FakeLLM(sentence_script("Sure thing.", "Let me answer that."))
    question = "How do you handle interruptions?"

    with connect(llm) as harness:
        turn = harness.ask(question)

    assert types_of(turn) == [
        "state",
        "transcript.user",
        "transcript.agent",
        "state",
        "transcript.agent",
        "metrics",
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
    """TC-BE-044: TR-033 -- transcript.agent arrives per sentence, before that sentence is heard.

    The ordering is the half that matters to a listener: the caption for a
    sentence has to be on screen before the sound of it arrives, or the
    transcript reads as lagging behind the voice.
    """
    llm = FakeLLM(sentence_script("Sure thing.", "Let me answer that."))

    with connect(llm) as harness:
        turn = harness.ask("How does barge-in work?")
        order = list(harness.order)

    sentences = only(turn, "transcript.agent")
    assert [message["sentence_id"] for message in sentences] == [0, 1]
    assert [message["text"] for message in sentences] == ["Sure thing.", "Let me answer that."]

    for sentence_id in (0, 1):
        announced = order.index(("transcript.agent", sentence_id))
        first_frame = order.index(("audio", sentence_id))
        assert announced < first_frame, f"sentence {sentence_id} was heard before it was announced"


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

    The turn is interrupted once it is genuinely SPEAKING, which is the state
    the row names and the only one a listener can barge in on.
    """
    llm = FakeLLM(long_script(), delay_s=STEP_DELAY_S)

    with connect(llm) as harness:
        harness.send(type="text.input", text="Tell me everything.")
        # SPEAKING is announced when the first audio frame leaves, so waiting for it means the
        # interrupt lands on a turn that is genuinely being heard.
        harness.recv_until(is_state(SessionState.SPEAKING))

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
    cut it honestly (TR-090). It also means a stray interrupt -- a VAD misfire, a
    duplicate from a flaky connection -- arriving once the answer has actually
    been heard could rewrite it, and tell the model it was cut off when it was
    not. The gate is whether the room is still listening, which the client
    reports, so this one changes nothing.
    """
    llm = FakeLLM(short_script(), delay_s=SHORT_STEP_DELAY_S)

    with connect(llm) as harness:
        harness.send(type="text.input", text="Tell me everything.")
        harness.recv_until(is_type("transcript.agent"))
        interrupted = time.monotonic()
        harness.send(type="interrupt", last_completed_sentence_id=0)
        harness.recv_until(is_state(SessionState.HEARING))

        # A second turn, heard in full, inside the window the first one opened. "Heard in full"
        # is a claim the client makes rather than one the server can infer, so the test makes it
        # the way a browser does: progress for the last sentence, which is what tells the server
        # the room has stopped listening (TR-090).
        turn = harness.ask("And what else?")
        last = only(turn, "transcript.agent")[-1]["sentence_id"]
        harness.send(type="playback.progress", turn_id=2, sentence_id=last)
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

    # SPEAKING is in this window now: it is announced as the first frame leaves,
    # which is also what makes the interrupt that follows meaningful.
    assert [message["value"] for message in only(after, "state")] == [
        SessionState.SPEAKING.value,
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


def test_speech_onset_while_thinking_does_not_cancel_the_turn(isolated_env: Any) -> None:
    """TC-BE-049: TR-023 -- onset before any sound is turn-taking, not barge-in.

    Cancelling here throws away work the user is still waiting for, and nothing
    has been said that an interrupt could cut short. Seen live: the tail of the
    user's own sentence arrived a fraction of a second after their turn started
    and cancelled it twice, showing an interrupt chip for something nobody
    interrupted.

    Nothing is lost by waiting. If the onset really is a new question, its
    utterance supersedes the running turn a moment later.
    """
    # Delayed, so the onset lands while the turn is still thinking rather than
    # after it has begun speaking -- which would be a genuine barge-in.
    llm = FakeLLM(sentence_script("Sure thing.", "Let me answer that."), delay_s=STEP_DELAY_S)

    with connect(llm) as harness:
        harness.send(type="text.input", text="Tell me everything.")
        harness.recv_until(is_type("transcript.user"))
        harness.send(type="speech.start")
        turn = harness.recv_until(is_state(SessionState.LISTENING))

    # The turn ran to completion; no cancellation was announced.
    assert only(turn, "agent.cancelled") == []
    assert [message["text"] for message in only(turn, "transcript.agent")] == [
        "Sure thing.",
        "Let me answer that.",
    ]


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

    Injected at the model here, and at the transcriber in
    ``test_a_transcriber_failure_is_reported_and_the_session_survives``; both
    reach the same handler, and between them they cover the two codes it maps.
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
    """TC-BE-053/054: TR-172, F4 -- an empty or filler question costs the model nothing.

    Driven by typing here, because the denylist lives in ``run_turn`` and both
    paths reach it. What silence does when it arrives as *audio* is asserted
    separately, in the audio-path tests at the end of this file, since that
    branch belongs to ``handle_utterance`` and never runs for a typed question.
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


def test_a_binary_frame_without_speech_end_is_refused(isolated_env: Any) -> None:
    """TC-BE-055: TR-140 -- an utterance may only follow the message that announces it.

    The guard is what tells a real upload apart from a stray frame. Without it a
    misbehaving client could hand the transcriber arbitrary bytes at any moment.
    """
    llm = FakeLLM(sentence_script("Sure thing."))

    with connect(llm) as harness:
        harness.ws.send_bytes(b"\x00\x01" * 16)
        error = harness.recv_until(is_type("error"))[-1]

        # The session survives: the next question is answered normally.
        harness.ask("What is this deck about?")

    assert error["code"] == "unexpected_binary"
    assert error["recoverable"] is True


def test_an_oversized_utterance_closes_the_socket(isolated_env: Any) -> None:
    """TC-BE-056: TR-182 -- an utterance past the cap closes the socket rather than being sent.

    Closing rather than answering is deliberate. A frame this size is a bug or an
    attack, and either way it must not reach the transcriber, where it would be
    billed and would occupy the turn for as long as it took to fail.
    """
    llm = FakeLLM(sentence_script("Sure thing."))
    cap = 4096

    with pytest.raises(WebSocketDisconnect) as raised, connect(llm, max_utterance_bytes=cap) as h:
        h.send(type="speech.end", duration_ms=90_000)
        h.ws.send_bytes(b"\x00\x01" * cap)
        # The socket is gone, so the next read raises rather than returning.
        h.recv()

    assert raised.value.code == 1009


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


def test_start_presentation_walks_the_whole_deck_without_the_model(isolated_env: Any) -> None:
    """TC-BE-059: F8 -- a walkthrough speaks the deck's own notes, slide by slide.

    The model is deliberately not consulted. Speaker notes are already written to
    be spoken, so presenting reads them; asking a model to paraphrase them would
    cost roughly 21,000 input tokens against a 7,000-a-minute ceiling and add
    nothing but the chance of drifting from the source.
    """
    llm = FakeLLM(sentence_script("this should never be requested"))

    with connect(llm) as harness:
        harness.send(type="control", action="start_presentation")
        turn = harness.recv_until(is_state(SessionState.LISTENING))

    assert llm.calls == [], "a walkthrough must not consult the model"

    visited = [message["index"] for message in only(turn, "slide.goto")]
    assert visited == [1, 2, 3, 4, 5, 6], "every slide, in order"

    spoken = " ".join(message["text"] for message in only(turn, "transcript.agent"))
    assert spoken, "the walkthrough must actually say something"
    # The words come from the deck, not from a model.
    assert "Dynamic Voice Deck" in spoken


def test_a_spoken_request_to_walk_through_starts_the_presentation(isolated_env: Any) -> None:
    """TC-BE-250: F8 -- "walk me through it" starts a walkthrough, spoken or typed.

    Slide 1 tells the listener to say exactly this, so it has to work every time,
    and the walkthrough it starts involves no model at all. Matched on the shared
    turn path so the same words do the same thing however they arrive.
    """
    llm = FakeLLM(sentence_script("this should never be requested"))

    with connect(llm) as harness:
        harness.send(type="text.input", text="Walk me through it, please.")
        turn = harness.recv_until(is_state(SessionState.LISTENING))

    assert llm.calls == []
    assert [message["index"] for message in only(turn, "slide.goto")] == [1, 2, 3, 4, 5, 6]


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

    What is asserted is the guard: a report for a turn that is not current, or
    for a session that is no longer speaking, is silently dropped rather than
    moving the state machine on someone else's behalf.
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
    # Gated on SPEAKING, which since audio landed arrives with the first frame,
    # in the middle of a turn. Parking there is what this test needs: a turn
    # suspended mid-send, which the next turn must cancel rather than leave to
    # wake up and stamp its own id on the new turn's transitions.
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

    # The parked send is no longer cancelled where it stands: cancelling mid-write
    # corrupted the socket when audio landed, so a send is allowed to finish. The
    # guarantee that replaced it is stronger and is what this row is really about
    # -- a transition belonging to the abandoned turn must never be stamped with
    # the id of the turn that replaced it.
    stamped = [
        turn_id for value, turn_id in socket.states() if value == SessionState.SPEAKING.value
    ]
    assert stamped == [1, 2], "each SPEAKING must carry the id of the turn that produced it"
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


def test_a_failing_sentence_is_skipped_and_logged(isolated_env: Any) -> None:
    """TC-BE-060: TR-173 -- synthesis failing on one sentence must not kill the turn.

    Losing one sentence of an answer is a far smaller harm than cutting the
    answer off, and the transcript still shows what was meant, so the listener
    hears a gap rather than silence.
    """
    llm = FakeLLM(sentence_script("First point.", "Second point.", "Third point."))

    with connect(llm, tts_fail_on=1) as harness:
        turn = harness.ask("Tell me about barge-in.")

    # Every sentence was announced, including the one that could not be spoken.
    assert [message["text"] for message in only(turn, "transcript.agent")] == [
        "First point.",
        "Second point.",
        "Third point.",
    ]
    # Audio arrived for the two that worked, and the turn finished normally.
    assert harness.audio, "the surviving sentences must still be heard"
    assert only(turn, "error") == []


# --------------------------------------------------------------------------- #
# The audio path                                                              #
# --------------------------------------------------------------------------- #
#
# Every other test here asks by typing, because for most of the state machine
# the two paths are the same code and typing is cheaper to read. These are the
# rows where they are not: what happens between the bytes arriving and the turn
# opening belongs to `handle_utterance` alone.


def test_a_spoken_question_is_transcribed_and_answered(isolated_env: Any) -> None:
    """TC-BE-281: TR-030/TR-140 -- an uploaded utterance becomes a turn, with its cost reported.

    The transcription latency is asserted because it is the one number in the
    metrics that no other path can produce: a typed question reports ``None``
    for it, so a regression that dropped it would be invisible everywhere else.
    """
    audio = b"\x00\x01" * 8_000
    stt = FakeSTT({audio: "How do you handle interruptions?"}, latency_ms=265)
    llm = FakeLLM(sentence_script("Two layers, actually."))

    with connect(llm, stt=stt) as harness:
        messages = harness.say(audio)

    user = [m for m in messages if m["type"] == "transcript.user"]
    assert [m["text"] for m in user] == ["How do you handle interruptions?"]
    # The words the transcriber heard are the words the model is asked about.
    assert llm.calls[0].messages[-1].content.endswith("How do you handle interruptions?")
    assert stt.calls[0].pcm16 == audio
    assert stt.calls[0].sample_rate == 16_000
    metrics = [m for m in messages if m["type"] == "metrics"]
    assert metrics[-1]["stt_ms"] == 265


def test_the_session_stays_in_hearing_while_the_transcriber_works(isolated_env: Any) -> None:
    """TC-BE-282: TR-020 -- transcription belongs to the utterance, not to a turn.

    Announcing THINKING here would carry the *previous* turn's id, so a client
    would see two THINKING transitions with different ids for one question.
    """
    llm = FakeLLM(sentence_script("Two layers, actually."))

    with connect(llm) as harness:
        harness.send(type="speech.start")
        assert harness.recv()["value"] == SessionState.HEARING
        harness.send(type="speech.end", duration_ms=900)
        harness.ws.send_bytes(b"\x00\x01" * 8_000)
        states = [
            m for m in harness.recv_until(is_state(SessionState.LISTENING)) if m["type"] == "state"
        ]

    assert [m["value"] for m in states] == [
        SessionState.THINKING,
        SessionState.SPEAKING,
        SessionState.LISTENING,
    ]
    # One THINKING, and it belongs to the turn the utterance opened.
    thinking = [m for m in states if m["value"] == SessionState.THINKING]
    assert [m["turn_id"] for m in thinking] == [1]


def test_an_utterance_that_transcribes_to_nothing_is_dropped(isolated_env: Any) -> None:
    """TC-BE-053: TR-172 -- silence reaching the transcriber costs the model nothing."""
    stt = FakeSTT(default_text="   ")
    llm = FakeLLM(sentence_script("Never asked."))

    with connect(llm, stt=stt) as harness:
        harness.send(type="speech.end", duration_ms=400)
        harness.ws.send_bytes(b"\x00\x01" * 8_000)
        assert harness.recv()["value"] == SessionState.LISTENING
        quiet = harness.barrier()

    assert quiet == []
    assert llm.calls == []


def test_a_filler_transcript_is_dropped_the_same_way(isolated_env: Any) -> None:
    """TC-BE-054: F4 -- Whisper's favourite hallucination on silence never becomes a turn."""
    stt = FakeSTT(default_text="Thank you.")
    llm = FakeLLM(sentence_script("Never asked."))

    with connect(llm, stt=stt) as harness:
        harness.send(type="speech.end", duration_ms=400)
        harness.ws.send_bytes(b"\x00\x01" * 8_000)
        harness.recv_until(is_state(SessionState.LISTENING))

    assert llm.calls == []


def test_a_transcriber_failure_is_reported_and_the_session_survives(isolated_env: Any) -> None:
    """TC-BE-052: TR-170 -- an upstream failure is recoverable and named for what it was."""
    stt = FakeSTT(error=ProviderError(GROQ_STT_NAME, "upstream error: 503"))
    llm = FakeLLM(sentence_script("Two layers, actually."))

    with connect(llm, stt=stt) as harness:
        harness.send(type="speech.end", duration_ms=900)
        harness.ws.send_bytes(b"\x00\x01" * 8_000)
        error = harness.recv_until(is_type("error"))[-1]
        assert harness.recv()["value"] == SessionState.LISTENING
        # The next question is answered normally: the failure cost one turn, not the session.
        harness.ask("What is this deck about?")

    assert error["code"] == "stt_failed"
    assert error["recoverable"] is True
    assert llm.calls, "the session refused to work after a transcription failure"


def test_a_rate_limit_reports_how_long_to_wait(isolated_env: Any) -> None:
    """TC-BE-283: TR-171 -- the wait travels as a number the client can count down.

    As prose it would have to be parsed back out of an upstream error string
    that reads differently for every provider. The free tier's ceiling is
    reachable in ordinary use, so this is a status the user sees, not an edge
    case: the difference between "it broke" and "it is ready in twelve seconds".
    """
    llm = FailingLLM(
        ProviderError(
            "groq_llm",
            "rate limit reached; try again in 13.9s",
            retryable=True,
            retry_after=13.9,
        )
    )

    with connect(llm) as harness:
        turn = harness.ask("What is this deck about?")

    error = only(turn, "error")[0]
    assert error["code"] == "rate_limited"
    assert error["retry_after_s"] == pytest.approx(13.9)
    assert error["recoverable"] is True
    # The upstream body is not forwarded: Groq's 429 names the organisation id, the billing page
    # and the exact token counts, and the event log is exportable.
    assert error["message"] == (
        "the free tier is out of capacity for a moment; ready again in about 14s"
    )
    assert "http" not in error["message"]
    assert "organization" not in error["message"]


def test_a_long_wait_is_stated_in_minutes(isolated_env: Any) -> None:
    """TC-BE-289: TR-171 -- "ready in 877s" is a number; "ready in 15 min" is an answer.

    The question a rate-limited user is actually asking is whether to wait or to
    go and do something else, and a four-figure count of seconds does not answer
    it.
    """
    llm = FailingLLM(
        ProviderError("groq_llm", "rate limit reached", retryable=True, retry_after=877.0)
    )

    with connect(llm) as harness:
        turn = harness.ask("What is this deck about?")

    error = only(turn, "error")[0]
    assert error["message"].endswith("about 15 min")
    assert error["retry_after_s"] == pytest.approx(877.0)


def test_an_ordinary_failure_carries_no_wait(isolated_env: Any) -> None:
    """TC-BE-284: TR-171 -- only a rate limit sets the countdown.

    A chip that appears for every failure would be telling the user to wait for
    something that is not going to fix itself.
    """
    llm = FailingLLM(ProviderError("groq_llm", "upstream refused", retryable=False))

    with connect(llm) as harness:
        turn = harness.ask("What is this deck about?")

    error = only(turn, "error")[0]
    assert error["code"] == "llm_failed"
    assert error["retry_after_s"] is None


def test_carry_on_resumes_the_walkthrough_where_it_was_cut(isolated_env: Any) -> None:
    """TC-BE-287: F8 -- "carry on" continues the tour from the slide it was interrupted on.

    Starting over would be the wrong answer to those words, and it is what
    ``walk me through it`` already does. The cursor is what separates them.
    """
    with connect(FakeLLM()) as harness:
        harness.send(type="control", action="start_presentation")
        harness.recv_until(lambda m: m["type"] == "slide.goto" and m["index"] > 1)
        harness.send(type="interrupt", last_completed_sentence_id=0)
        harness.recv_until(is_type("agent.cancelled"))
        seen_before_the_cut = [m["index"] for m in harness.received if m["type"] == "slide.goto"][
            -1
        ]

        harness.send(type="text.input", text="Carry on.")
        resumed = harness.recv_until(lambda m: m["type"] == "slide.goto")

    # The tour picks up where it was, and specifically not back at slide one.
    #
    # The assertion is a floor rather than an equality, and deliberately so: the cursor advances
    # before the slide it advanced to is announced, so an interrupt landing inside that window
    # leaves the server one slide ahead of anything the client has been told. Asserting equality
    # here would be asserting that the cut cannot land in that window, which is a race, not a
    # contract. What the feature promises is that the cursor survives, and that is what is checked.
    resumed_at = only(resumed, "slide.goto")[0]["index"]
    assert seen_before_the_cut > 1
    assert resumed_at >= seen_before_the_cut
    assert only(resumed, "transcript.user")[0]["text"] == "Carry on."


def test_carry_on_outside_a_walkthrough_is_an_ordinary_question(isolated_env: Any) -> None:
    """TC-BE-288: F8 -- in conversation those words mean "say more", which is the model's job."""
    llm = FakeLLM(sentence_script("There is more to it."))

    with connect(llm) as harness:
        turn = harness.ask("Carry on.")

    assert only(turn, "slide.goto") == []
    assert llm.calls, "the model was never asked"
    assert [m["text"] for m in only(turn, "transcript.agent")] == ["There is more to it."]


def test_a_substituted_model_is_announced_to_the_client(isolated_env: Any) -> None:
    """TC-BE-319: TR-085 -- the listener is told which model answered, before it speaks.

    The whole path: the wrapper reports the switch through the stream, the
    pipeline turns it into a protocol message, and it arrives ahead of the
    answer so the transcript explains why the voice that follows is slower.
    """
    primary = FailingLLM(ProviderError("groq_llm", "HTTP 429", retryable=True, retry_after=12.0))
    local = FakeLLM(sentence_script("Answering locally."))
    llm = FallbackLLM(
        primary=primary,
        fallback=local,
        primary_model="qwen/qwen3.8-27b",
        fallback_model="qwen2.5:7b",
    )

    with connect(llm) as harness:
        turn = harness.ask("How do you handle interruptions?")

    announced = only(turn, "provider.fallback")
    assert len(announced) == 1
    assert announced[0]["from_model"] == "qwen/qwen3.8-27b"
    assert announced[0]["to_model"] == "qwen2.5:7b"
    assert announced[0]["retry_after_s"] == pytest.approx(12.0)
    assert announced[0]["stage"] == "llm"

    # It arrives before the answer, and the answer is the local model's.
    types = types_of(turn)
    assert types.index("provider.fallback") < types.index("transcript.agent")
    assert [m["text"] for m in only(turn, "transcript.agent")] == ["Answering locally."]
    # And no error: the turn succeeded, so nothing should claim otherwise.
    assert only(turn, "error") == []


def test_a_substitution_is_announced_once_however_many_requests_the_turn_makes(
    isolated_env: Any,
) -> None:
    """TC-BE-336: TR-085 -- a navigating turn makes two model calls, not two announcements.

    Each request is refused separately by a rate limit, so without a per-turn
    guard the listener is told the same thing twice and the log fills with it.
    Observed three times in one turn during a live session.
    """
    primary = FailingLLM(ProviderError("groq_llm", "HTTP 429", retryable=True, retry_after=12.0))
    # A call and nothing said, which is exactly what costs a second model request: the tool
    # result goes back and the model is asked again for the words.
    local = FakeLLM(
        [
            ToolCallDelta(call_id="c1", name="go_to_slide", arguments={"slide_index": 4}),
            LLMDone(finish_reason="tool_calls"),
        ]
    )
    llm = FallbackLLM(
        primary=primary,
        fallback=local,
        primary_model="qwen/qwen3.8-27b",
        fallback_model="qwen2.5:7b",
    )

    with connect(llm) as harness:
        turn = harness.ask("How do you handle interruptions?")

    assert len(only(turn, "provider.fallback")) == 1
    # The turn really did make more than one request, which is what makes the count meaningful.
    assert primary.calls > 1


def test_a_cough_during_an_answer_does_not_kill_the_answer(isolated_env: Any) -> None:
    """TC-BE-337: TR-172 -- filler is recognised before the running turn is cancelled.

    Starting a turn cancels whatever was running, so judging filler inside
    ``run_turn`` meant a stray noise during an answer destroyed that answer and
    replaced it with nothing. Observed in a live session: a question was cut off
    by an utterance that transcribed to a hallucinated "Thank you.", and the
    listener had to ask again.
    """
    stt = FakeSTT(default_text="Thank you.")
    llm = FakeLLM(long_script(), delay_s=STEP_DELAY_S)

    with connect(llm, stt=stt) as harness:
        harness.send(type="text.input", text="Tell me everything.")
        harness.recv_until(is_type("transcript.agent"))

        # A cough, mid-answer. It transcribes to filler, so it must change nothing.
        harness.send(type="speech.end", duration_ms=400)
        harness.ws.send_bytes(b"\x00\x01" * 8_000)

        # The answer reaches its own end rather than being cut short. Waited for by the turn's
        # metrics, which only a turn that finished produces.
        rest = harness.recv_until(is_type("metrics"))

    assert only(rest, "agent.cancelled") == []
    assert llm.cancelled == 0
    # And the filler never became a turn of its own.
    assert [m["text"] for m in only(rest, "transcript.user")] == []


def test_talking_over_an_answer_that_has_finished_generating_still_cuts_it(
    isolated_env: Any,
) -> None:
    """TC-BE-338: TR-090 -- the room is still listening after the server has stopped writing.

    Synthesis is streamed several seconds faster than it can be heard, so a turn
    is routinely finished here while the listener is mid-sentence. Talking over
    that is a real barge-in: it has to be announced, and above all the history
    has to be cut, or the agent believes it said sentences nobody received --
    the exact failure TR-051 exists to prevent.
    """
    llm = FakeLLM(sentence_script("One.", "Two.", "Three."))

    with connect(llm) as harness:
        # Let the turn finish completely: metrics, then LISTENING.
        harness.ask("Tell me everything.")
        # The client is still playing sentence two when the listener speaks.
        harness.send(type="interrupt", last_completed_sentence_id=1)
        cancelled = harness.recv_until(is_type("agent.cancelled"))[-1]
        harness.recv()  # the state that follows
        harness.ask("Something else.")

    assert cancelled["turn_id"] == 1
    assert cancelled["truncated_at_sentence_id"] == 1
    # The agent's memory holds the two sentences that were heard, and not the third.
    answers = [message.content for message in llm.calls[-1].messages if message.role == "assistant"]
    assert answers[0] == f"One. Two. {INTERRUPTED_MARKER}"
    assert "Three." not in answers[0]


def test_a_second_interrupt_after_playback_finished_changes_nothing(isolated_env: Any) -> None:
    """TC-BE-339: TR-090/TR-024 -- once the room has heard it all, a late message is stray."""
    llm = FakeLLM(sentence_script("One.", "Two."))

    with connect(llm) as harness:
        turn = harness.ask("Tell me everything.")
        last = only(turn, "transcript.agent")[-1]["sentence_id"]
        harness.send(type="playback.progress", turn_id=1, sentence_id=last)
        harness.send(type="interrupt", last_completed_sentence_id=0)

        assert harness.barrier() == []
        harness.ask("Something else.")

    answers = [message.content for message in llm.calls[-1].messages if message.role == "assistant"]
    assert answers[0] == "One. Two."
    assert INTERRUPTED_MARKER not in answers[0]
