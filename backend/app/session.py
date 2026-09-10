"""Session state machine and WebSocket message handling (TRD §4.3).

One :class:`Session` exists per open socket. It owns the turn-taking state, the
conversation history, the deck position, and at most one in-flight turn task.

The single-task rule is what makes barge-in correct: starting a turn cancels and
awaits the previous one (TR-022), so two turns can never write to the same
history concurrently. Every outbound message carries the ``turn_id`` it belongs
to, letting the client discard output from a turn it has already interrupted
(TR-021).

The second rule is that no turn ever just disappears. However a turn ends --
barge-in, the watchdog, a provider failure, an unexpected exception, a pause --
history is cut to the sentences the room actually heard before the session goes
back to listening (TR-051). Dropping the turn instead leaves the model with no
record of having spoken, so the next question is answered from the top and the
audience hears the same two sentences twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
import uuid
from typing import Any

from fastapi import WebSocket
from pydantic import ValidationError

from .config import Settings
from .decks.repository import DeckRepository
from .errors import DeckError, ProviderError
from .logging_setup import get_logger
from .pipeline.history import ConversationHistory
from .pipeline.metrics import TurnMetrics
from .pipeline.prompt import PromptBuilder
from .pipeline.slides import SlideController
from .pipeline.turn import (
    cancel_task,
    provider_error_message,
    run_presentation,
    run_turn,
)
from .protocol import (
    CLIENT_MESSAGE_ADAPTER,
    AgentCancelledMsg,
    ControlAction,
    ControlMsg,
    ErrorCode,
    ErrorMsg,
    InterruptMsg,
    PlaybackProgressMsg,
    ProviderNames,
    ServerMessage,
    SessionMode,
    SessionReadyMsg,
    SessionStartMsg,
    SessionState,
    SlideChangedMsg,
    SpeechEndMsg,
    SpeechStartMsg,
    StateMsg,
    TextInputMsg,
    TranscriptUserMsg,
)
from .providers.base import Providers

logger = get_logger(__name__)

PRESENTATION_REQUEST = re.compile(
    r"\b(walk me through|give me the tour|present the deck|start the presentation"
    r"|run through the deck|take me through)\b",
    re.IGNORECASE,
)
"""Spoken phrases that start a walkthrough rather than asking a question.

Matched here rather than left to the model, for three reasons. Slide 1 tells the
listener to say exactly this, so it has to work every time. It costs nothing,
where a model round trip to reach the same conclusion costs a fifth of a
free-tier minute. And the walkthrough that follows involves no model at all, so
routing it through one only to be told what we already know would be the only
model call in the feature.
"""

INTERRUPT_DEBOUNCE_S = 0.5
"""How long a turn stays interruptible after it was already interrupted.

Scoped to the turn, not to the clock (TR-024). A second interrupt naming the
turn just cancelled is absorbed -- refining where history was cut if it carries
a better sentence id (TR-023), and otherwise doing nothing -- while a barge-in
on a *newly started* turn is always honoured, however soon it arrives. Keying
this on wall-clock time alone would let the agent talk over a user who
interrupts twice in quick succession, which is the one thing barge-in exists to
prevent.
"""


class Session:
    """One client connection and its conversation.

    Attributes:
        id: Short identifier used in logs.
        deck: The deck being presented.
        history: Conversation history for this session.
        slides: Deck position and navigation rules.
        state: Current turn-taking state.
        turn_id: Id of the most recently started turn.
    """

    def __init__(
        self,
        websocket: WebSocket,
        *,
        settings: Settings,
        providers: Providers,
        decks: DeckRepository,
        prompts: PromptBuilder,
    ) -> None:
        """Create a session bound to an accepted WebSocket.

        Args:
            websocket: The accepted connection.
            settings: Runtime configuration.
            providers: Provider implementations for this process.
            decks: Deck repository.
            prompts: System prompt builder.
        """
        self.id = uuid.uuid4().hex[:8]
        self._ws = websocket
        self._settings = settings
        self._providers = providers
        self._decks = decks
        self._prompts = prompts

        self.deck: Any = None
        self.history = ConversationHistory(max_turns=settings.max_history_turns)
        self.slides: SlideController | None = None
        self.state = SessionState.CONNECTING
        self.turn_id = 0

        self._task: asyncio.Task[None] | None = None
        self._interrupted_turn: int | None = None
        self._interrupted_at = 0.0
        self._expecting_binary = False
        self._started = False

    # ----------------------------------------------------------------- send

    async def send(self, message: ServerMessage) -> None:
        """Send one message, tolerating a client that has already gone.

        Args:
            message: The message to send.
        """
        try:
            await self._ws.send_text(message.model_dump_json())
        except (RuntimeError, ConnectionError):  # pragma: no cover - race on close
            logger.info("session.send_after_close", session_id=self.id)

    async def send_audio(self, frame: bytes) -> None:
        """Send one binary audio frame, tolerating a client that has gone.

        Args:
            frame: A framed chunk of PCM16 (see ``protocol.encode_audio_frame``).
        """
        try:
            await self._ws.send_bytes(frame)
        except (RuntimeError, ConnectionError):  # pragma: no cover - race on close
            logger.info("session.audio_after_close", session_id=self.id)

    async def set_state(self, value: SessionState) -> None:
        """Transition to a state and tell the client (TR-020).

        Args:
            value: The new state.
        """
        self.state = value
        await self.send(StateMsg(value=value, turn_id=self.turn_id, server_ts=time.time()))

    async def send_error(self, code: ErrorCode, message: str, *, recoverable: bool = True) -> None:
        """Report a failure to the client.

        Args:
            code: Stable error code.
            message: Human-readable explanation.
            recoverable: Whether the session continues.
        """
        logger.warning("session.error", session_id=self.id, code=code.value, message=message)
        await self.send(ErrorMsg(code=code, message=message, recoverable=recoverable))

    # -------------------------------------------------------------- receive

    async def handle_raw_text(self, raw: str) -> None:
        """Validate and dispatch one inbound text frame (TR-142).

        Args:
            raw: The frame's contents.
        """
        if len(raw.encode()) > self._settings.max_json_message_bytes:
            await self.send_error(ErrorCode.BAD_MESSAGE, "message too large")
            return
        try:
            message = CLIENT_MESSAGE_ADAPTER.validate_json(raw)
        except ValidationError as exc:
            await self.send_error(ErrorCode.BAD_MESSAGE, _first_error(exc))
            return
        await self.dispatch(message)

    async def dispatch(self, message: Any) -> None:
        """Route a validated client message to its handler.

        Args:
            message: The validated message.
        """
        if isinstance(message, SessionStartMsg):
            await self._on_session_start(message)
            return
        if not self._started:
            await self.send_error(ErrorCode.BAD_MESSAGE, "session.start must come first")
            return

        if isinstance(message, TextInputMsg):
            await self.start_turn(message.text)
        elif isinstance(message, SpeechStartMsg):
            await self._on_speech_start()
        elif isinstance(message, SpeechEndMsg):
            # The utterance itself follows as one binary frame (TR-140). Arming
            # here is what lets a stray binary frame be told apart from a real
            # upload.
            self._expecting_binary = True
        elif isinstance(message, InterruptMsg):
            await self._on_interrupt(message.last_completed_sentence_id)
        elif isinstance(message, SlideChangedMsg):
            self._on_user_navigation(message.index)
        elif isinstance(message, PlaybackProgressMsg):
            await self._on_playback_progress(message)
        elif isinstance(message, ControlMsg):
            await self._on_control(message.action)
        # interrupt.cancel needs no action: the turn is already cancelled and the
        # session is listening, which is exactly where a misfire should leave it.

    # -------------------------------------------------------------- handlers

    async def _on_session_start(self, message: SessionStartMsg) -> None:
        """Load the deck and admit the client.

        Args:
            message: The opening message naming the deck and mode.
        """
        try:
            self.deck = self._decks.get(message.deck_id)
        except DeckError as exc:
            await self.send_error(ErrorCode.DECK_NOT_FOUND, str(exc), recoverable=False)
            return

        self.slides = SlideController(self.deck, mode=message.mode)
        self._started = True
        names = self._providers.names
        await self.send(
            SessionReadyMsg(
                session_id=self.id,
                deck=self.deck,
                providers=ProviderNames(stt=names["stt"], llm=names["llm"], tts=names["tts"]),
            )
        )
        logger.info(
            "session.opened", session_id=self.id, deck=self.deck.id, mode=message.mode.value
        )
        await self.set_state(SessionState.LISTENING)

    async def _on_speech_start(self) -> None:
        """Handle voice onset, which is barge-in only while the agent is speaking.

        Onset while merely THINKING is deliberately *not* an interrupt. Nothing
        has been said yet, so there is nothing to cut short, and cancelling
        throws away work the user is still waiting for. Observed in a live
        session: the tail of the user's own sentence arrived a fraction of a
        second after their turn started and cancelled it, twice, producing an
        interrupt chip for something nobody interrupted.

        Nothing is lost by waiting. If the onset really is a new question, its
        utterance arrives a moment later and :meth:`start_turn` supersedes the
        running turn anyway -- which is the same cancellation, taken at the point
        where it is known to be wanted.
        """
        if self.state is SessionState.SPEAKING:
            await self._on_interrupt(None)
            return
        if self.state is not SessionState.THINKING:
            await self.set_state(SessionState.HEARING)

    async def _on_interrupt(self, last_completed: int | None) -> None:
        """Cancel the in-flight turn, or refine the cut of the one just cancelled.

        Args:
            last_completed: Id of the last sentence the user actually heard, or
                ``None`` if playback had not begun.
        """
        if self.state in (SessionState.THINKING, SessionState.SPEAKING):
            await self._cancel_turn(last_completed)
            return
        # TR-023: `speech.start` cancels with no sentence id at all, and the
        # client follows it with the precise one it read off the playback queue.
        # That follow-up always arrives with the session already in HEARING, so
        # the guard above would drop the only accurate truncation point the
        # client ever sends. Accepting it for the turn just cancelled is also
        # what makes a repeated interrupt idempotent rather than an error
        # (TR-024): re-cutting at the same sentence changes nothing.
        if last_completed is None or not self._recently_interrupted(self.turn_id):
            return  # TR-024: nothing to interrupt, and nothing more precise to say
        if self.history.truncate_current(self.turn_id, last_completed):
            logger.info(
                "turn.truncation_refined",
                session_id=self.id,
                turn_id=self.turn_id,
                truncated_at=last_completed,
            )

    def _recently_interrupted(self, turn_id: int) -> bool:
        """Report whether this turn was interrupted within the debounce window.

        Args:
            turn_id: The turn a follow-up interrupt names.

        Returns:
            ``True`` while a further interrupt for that turn should still be
            absorbed rather than treated as a barge-in on something new.
        """
        if self._interrupted_turn != turn_id:
            return False
        return time.monotonic() - self._interrupted_at < INTERRUPT_DEBOUNCE_S

    async def _cancel_turn(self, last_completed: int | None) -> None:
        """Stop the running turn and cut history to what the room actually heard.

        Args:
            last_completed: Id of the last sentence the user heard in full, or
                ``None`` when playback had not begun.
        """
        # PRD §7 names INTERRUPTED as a state of its own, and the orb renders it
        # as the flash that acknowledges the barge-in. Sent before the cancel so
        # the audience sees the agent give way at the moment they spoke, rather
        # than once the model stream has finished closing.
        await self.set_state(SessionState.INTERRUPTED)
        await cancel_task(self._task)
        self._task = None
        self._interrupted_turn = self.turn_id
        self._interrupted_at = time.monotonic()

        truncated = self.history.truncate_current(self.turn_id, last_completed)
        await self.send(
            AgentCancelledMsg(
                turn_id=self.turn_id,
                # Only claim a cut that actually happened: when history holds no
                # entry for this turn, nothing was rewritten and nothing kept.
                truncated_at_sentence_id=last_completed if truncated else None,
            )
        )
        if truncated:
            logger.info(
                "turn.cancelled",
                session_id=self.id,
                turn_id=self.turn_id,
                truncated_at=last_completed,
            )
        elif last_completed is None:
            # The interrupt beat the turn to the model: there is no assistant
            # entry yet and nothing was heard, so there is nothing to lose.
            logger.info("turn.cancelled_before_answer", session_id=self.id, turn_id=self.turn_id)
        else:
            # The client heard sentences that history has no entry to hold, so
            # the model will not learn they were said. Loud on purpose.
            logger.error(
                "turn.truncation_refused",
                session_id=self.id,
                turn_id=self.turn_id,
                history_turn_id=self.history.current_turn_id,
                last_completed=last_completed,
            )
        await self.set_state(SessionState.HEARING)

    def _on_user_navigation(self, index: int) -> None:
        """Record that the user moved the deck by hand (TR-063, F9).

        The agent is deliberately not interrupted: the user may just be looking
        ahead. Its next answer reflects the new position.

        Args:
            index: The slide the user moved to.
        """
        if self.slides is None:
            return
        note = self.slides.on_user_navigation(index)
        self.history.add_system_note(note)
        logger.info("slide.user_navigation", session_id=self.id, slide=index)

    async def _on_playback_progress(self, message: PlaybackProgressMsg) -> None:
        """Return to listening once the client finishes the last sentence.

        Playback completion is client-truth: only the browser knows when a
        sample actually reached the speakers, so the server does not guess.

        Args:
            message: Which sentence of which turn finished.
        """
        if message.turn_id != self.turn_id:
            return  # TR-131: progress for a turn that is no longer current
        # ``_task is None`` means this turn is done generating. Progress
        # reported while it is still running names a sentence the client has
        # finished but the answer has not, so it is not the end of the turn.
        if self.state is SessionState.SPEAKING and self._task is None:
            await self.set_state(SessionState.LISTENING)

    async def _on_control(self, action: ControlAction) -> None:
        """Apply a session-level command.

        Args:
            action: The requested change.
        """
        if self.slides is None:
            return
        if action is ControlAction.START_PRESENTATION:
            await self.start_presentation()
        elif action in (ControlAction.PAUSE, ControlAction.MUTE):
            await cancel_task(self._task)
            await self._end_turn_abnormally()

    async def handle_utterance(self, pcm16: bytes) -> None:
        """Transcribe an uploaded utterance and answer it (TR-030, TR-140).

        Args:
            pcm16: Little-endian 16-bit mono samples at 16 kHz, as captured by
                the browser.
        """
        if not self._expecting_binary:
            await self.send_error(
                ErrorCode.UNEXPECTED_BINARY,
                "a binary frame must follow speech.end",
            )
            return
        self._expecting_binary = False

        if len(pcm16) > self._settings.max_utterance_bytes:
            # TR-182. Closing rather than answering: a frame this size is either
            # a bug or an attack, and neither deserves a transcription bill.
            logger.warning("session.utterance_too_large", session_id=self.id, bytes=len(pcm16))
            await self._ws.close(code=1009, reason="utterance too large")
            return

        # Deliberately no state change here. Transcription takes a few hundred
        # milliseconds and belongs to the utterance, not to a turn: emitting
        # THINKING now would carry the *previous* turn's id, and the client would
        # see two THINKING transitions with different ids for one question. The
        # session stays in HEARING, which is honest -- it is still working out
        # what was said -- until `start_turn` opens the turn properly.
        try:
            transcript = await self._providers.stt.transcribe(pcm16)
        except ProviderError as exc:
            await self.send(provider_error_message(exc))
            await self.set_state(SessionState.LISTENING)
            return

        if not transcript.text.strip():
            # Silence, a cough, a keyboard. Not an error: just go back to
            # listening without troubling the model or the user (TR-172).
            logger.info("session.empty_transcript", session_id=self.id)
            await self.set_state(SessionState.LISTENING)
            return

        await self.start_turn(transcript.text, stt_ms=transcript.latency_ms)

    # ------------------------------------------------------------------ turn

    async def start_presentation(self, *, from_start: bool = True) -> None:
        """Walk the deck from the cursor, speaking its notes (F8).

        Args:
            from_start: Whether to begin at slide one. ``False`` resumes from
                wherever the walkthrough was interrupted, which is what
                "carry on" should do.
        """
        if self.slides is None or self.deck is None:
            return
        await cancel_task(self._task)
        self.slides.mode = SessionMode.PRESENT
        if from_start:
            self.slides.presentation_cursor = 1
        self.slides.begin_turn()
        self.turn_id += 1
        await self.set_state(SessionState.THINKING)
        self._task = asyncio.create_task(self._run_presentation(), name=f"present-{self.turn_id}")

    async def _run_presentation(self) -> None:
        """Execute a walkthrough and hand the floor back when it ends."""
        assert self.slides is not None  # noqa: S101 - guarded by start_presentation
        turn_id = self.turn_id
        metrics = TurnMetrics(turn_id=turn_id)
        try:
            await run_presentation(
                turn_id=turn_id,
                deck=self.deck,
                tts=self._providers.tts,
                voice=self.deck.voice,
                history=self.history,
                slides=self.slides,
                metrics=metrics,
                emit=self.send,
                send_audio=self.send_audio,
                on_speaking=self._on_speaking,
            )
        except asyncio.CancelledError:
            raise
        except ProviderError as exc:
            await self.send(provider_error_message(exc))
            await self._end_turn_abnormally()
            return
        except Exception:
            logger.exception("presentation.failed", session_id=self.id, turn_id=turn_id)
            await self.send_error(ErrorCode.INTERNAL_ERROR, "the walkthrough stopped unexpectedly")
            await self._end_turn_abnormally()
            return

        await self.send(metrics.to_message())
        # The walkthrough is over, so the deck is no longer presenting itself.
        self.slides.mode = SessionMode.QA
        self._task = None
        await self.set_state(SessionState.LISTENING)

    async def start_turn(self, text: str, *, stt_ms: int | None = None) -> None:
        """Begin a turn, cancelling any turn already running (TR-022).

        Args:
            text: The user's words.
            stt_ms: Milliseconds transcription took, when the words were spoken
                rather than typed, so the latency panel can show the whole chain.
        """
        if self.slides is None or self.deck is None:
            return
        # Awaited, not fired and forgotten: the previous turn must be finished
        # unwinding before ``turn_id`` moves on, or the two would write to the
        # same history at once and the outgoing one would stamp the new id on
        # its own last messages (TR-022).
        if PRESENTATION_REQUEST.search(text):
            # On the shared path, so the same words do the same thing whether
            # they were spoken or typed.
            await self.send(TranscriptUserMsg(turn_id=self.turn_id, text=text))
            await self.start_presentation()
            return

        await cancel_task(self._task)
        self.slides.begin_turn()
        self.turn_id += 1
        await self.set_state(SessionState.THINKING)
        self._task = asyncio.create_task(
            self._run_turn(text, stt_ms=stt_ms), name=f"turn-{self.turn_id}"
        )

    async def _run_turn(self, text: str, *, stt_ms: int | None = None) -> None:
        """Execute a turn under the watchdog and report its outcome.

        Args:
            text: The user's words.
            stt_ms: Transcription cost to report, when there was one.
        """
        assert self.slides is not None  # noqa: S101 - guarded by start_turn
        turn_id = self.turn_id
        metrics = TurnMetrics(turn_id=turn_id)
        if stt_ms is not None:
            metrics.set_stt_ms(stt_ms)
        try:
            async with asyncio.timeout(self._settings.turn_timeout_s):
                await run_turn(
                    turn_id=turn_id,
                    text=text,
                    deck=self.deck,
                    llm=self._providers.llm,
                    tts=self._providers.tts,
                    voice=self.deck.voice,
                    history=self.history,
                    slides=self.slides,
                    prompts=self._prompts,
                    metrics=metrics,
                    emit=self.send,
                    send_audio=self.send_audio,
                    on_speaking=self._on_speaking,
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            # TR-025: never leave the client stuck in `thinking`.
            await self.send_error(ErrorCode.TURN_TIMEOUT, "the agent took too long to answer")
            await self._end_turn_abnormally()
            return
        except ProviderError as exc:
            await self.send(provider_error_message(exc))
            await self._end_turn_abnormally()
            return
        except Exception:
            # Everything the pipeline did not anticipate ends here: a
            # model-authored tool argument that will not serialise, a sentence
            # recorded against a turn that is no longer pending, the chunker fed
            # something strange. Without this the exception dies inside the task,
            # never retrieved and never logged, and the client sits in THINKING
            # until the socket closes. `CancelledError` derives from
            # `BaseException` and is re-raised above, so barge-in still
            # propagates (CLAUDE.md §4.4).
            logger.exception("turn.failed", session_id=self.id, turn_id=turn_id)
            await self.send_error(ErrorCode.INTERNAL_ERROR, "the turn failed unexpectedly")
            await self._end_turn_abnormally()
            return

        await self.send(metrics.to_message())
        # SPEAKING was announced when the first frame left, so the turn only has
        # to hand the floor back. With audio the client's `playback.progress`
        # decides when that really is; this is the safety net for a turn that
        # produced no sound at all.
        await self.set_state(SessionState.LISTENING)
        # Cleared last. While this task is still reachable a turn starting in
        # the window above cancels it (TR-022) instead of racing it, so these
        # trailing transitions can never be stamped with the next turn's id nor
        # drag the session out of the state that turn just entered.
        self._task = None

    async def _end_turn_abnormally(self) -> None:
        """Abandon the running turn while keeping what the room already heard.

        The watchdog, a provider failure, an unexpected exception, and
        ``control{pause}``/``control{mute}`` all end a turn that the user did not
        interrupt. Each one used to drop the turn from history entirely, so the
        model never learned it had already said the sentences the audience heard
        and repeated them on the next question. Cutting to the last sentence
        actually sent is the same repair a barge-in makes (TR-051), with ``None``
        -- nothing was heard -- when the turn had not spoken yet.

        The caller cancels the task first when it is not the task itself.
        """
        if self.state in (SessionState.THINKING, SessionState.SPEAKING):
            self.history.truncate_current(self.turn_id, self.history.last_recorded_sentence_id)
        await self.set_state(SessionState.LISTENING)
        self._task = None

    async def _on_speaking(self, turn_id: int) -> None:
        """Enter SPEAKING as the turn's first audio frame leaves.

        The state has to change here rather than when the turn finishes, because
        this is when there is finally something for an interrupt to cut short.
        Announcing it at the end would leave the session in THINKING for the
        whole time it was talking, and barge-in would never fire.

        Args:
            turn_id: The turn whose audio started. Checked against the current
                turn because this runs on the sender's task, which can be parked
                inside a send when the turn is cancelled; waking up afterwards
                must not stamp this transition on whatever turn is running now.
        """
        if turn_id == self.turn_id and self.state is SessionState.THINKING:
            await self.set_state(SessionState.SPEAKING)

    async def close(self) -> None:
        """Cancel any in-flight turn and release the session (TR-026)."""
        await cancel_task(self._task)
        self._task = None
        logger.info("session.closed", session_id=self.id)


class SessionManager:
    """Tracks live sessions so shutdown can close them all."""

    def __init__(self) -> None:
        """Create an empty manager."""
        self._sessions: dict[str, Session] = {}

    def add(self, session: Session) -> None:
        """Register a session.

        Args:
            session: The session to track.
        """
        self._sessions[session.id] = session

    async def remove(self, session: Session) -> None:
        """Close and forget a session.

        Args:
            session: The session to release.
        """
        self._sessions.pop(session.id, None)
        await session.close()

    async def close_all(self) -> None:
        """Close every live session, used during application shutdown."""
        for session in list(self._sessions.values()):
            with contextlib.suppress(Exception):
                await session.close()
        self._sessions.clear()

    def __len__(self) -> int:
        """Return how many sessions are live.

        Returns:
            The session count.
        """
        return len(self._sessions)


def _first_error(exc: ValidationError) -> str:
    """Summarise a validation failure in one line for the client.

    Args:
        exc: The validation error.

    Returns:
        A short description naming the offending field where possible.
    """
    errors = exc.errors()
    if not errors:  # pragma: no cover - pydantic always reports at least one
        return "invalid message"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "function-after")
    return f"{location or 'message'}: {first.get('msg', 'invalid')}"
