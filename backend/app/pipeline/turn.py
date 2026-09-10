"""One conversational turn, from input to spoken answer (TRD §4.4).

A turn is a single cancellable coroutine. That is the whole design: barge-in is
implemented by cancelling this task (TR-034), which propagates into the model's
HTTP stream and closes it, so the agent stops generating rather than merely
being ignored. Every ``await`` here is therefore a place a cut can land, and the
two that must not be cut in half -- the model stream and the pair of messages
that moves the deck -- say so explicitly.

Phase 1 runs the text half of the pipeline: input, model stream, tool calls,
sentence chunking, and the per-sentence transcript. Synthesis is wired through
the same sentence loop in a later milestone; the seam is marked below.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Final

from ..decks.models import Deck
from ..errors import ProviderError
from ..logging_setup import get_logger
from ..protocol import (
    ErrorCode,
    ErrorMsg,
    ServerMessage,
    SlideGotoMsg,
    ToolCallMsg,
    ToolSource,
    TranscriptAgentMsg,
    TranscriptUserMsg,
    encode_audio_frame,
)
from ..providers.base import (
    LLMDone,
    LLMEvent,
    LLMProvider,
    Message,
    TokenDelta,
    ToolCallDelta,
    ToolSpec,
    TTSProvider,
)
from .chunker import SentenceChunker
from .history import ConversationHistory
from .metrics import TurnMetrics
from .prompt import PromptBuilder
from .slides import SlideAction, SlideController
from .tools import build_tools

logger = get_logger(__name__)

Emit = Callable[[ServerMessage], Awaitable[None]]
"""Sends one JSON message to the client. Supplied by the session."""

SendAudio = Callable[[bytes], Awaitable[None]]
"""Sends one binary audio frame to the client. Supplied by the session."""

MAX_LLM_STEPS = 2
"""Model round trips per turn: one that may call tools, one that speaks.

Two is a deliberate ceiling rather than a loop-until-done. Each step costs a
full prompt against a tight free-tier token budget, and a deck this size never
needs to navigate twice to answer one question.
"""

NO_ANSWER_FALLBACK: Final[str] = "Sorry, I lost that one. Could you ask me again?"
"""Spoken when a turn produces no content at all and moved nothing.

Silence is the one outcome a listener cannot tell apart from a crash: the client
went to ``thinking``, and then nothing ever came back. A short apology is a poor
answer and a far better outcome than none, and it also keeps an empty assistant
entry -- which every later request would replay -- out of history.
"""

FILLER_TRANSCRIPTS: frozenset[str] = frozenset(
    {
        "thank you.",
        "thanks for watching!",
        "you",
        "bye.",
        ".",
        "...",
    }
)
"""Whisper's known hallucinations on silence, dropped rather than answered.

Transcribing near-silence reliably produces one of these, and answering them
makes the agent look like it is talking to itself.
"""

_APOSTROPHE_RE: Final[re.Pattern[str]] = re.compile("['\u2019]")
"""Straight and curly apostrophes, removed so "doesn't" and "doesnt" match alike."""

_DECK: Final[str] = r"(?:this|the|my|our) deck"
"""How the agent refers to the deck when declining a question."""

OFF_TOPIC_PATTERNS: Final[tuple[str, ...]] = (
    rf"(?:outside|beyond)(?: of)? {_DECK}",
    rf"(?:not|isnt|arent) (?:in|on|part of|covered in|covered by) {_DECK}",
    rf"not (?:something|a topic) {_DECK}",
    rf"{_DECK} (?:doesnt|does not|cant|cannot|wont|will not) "
    r"(?:cover|include|have|go into|get into|touch|mention|talk about)",
)
"""Ways the presenter declines a question as outside the deck (PRD §8).

Matched against the answer with apostrophes stripped and whitespace collapsed,
so both "doesn't cover" and "does not cover" are one pattern's business.
"""

_OFF_TOPIC_RE: Final[re.Pattern[str]] = re.compile("|".join(OFF_TOPIC_PATTERNS))


def is_filler(text: str) -> bool:
    """Report whether a transcript is empty or a known silence artefact.

    Args:
        text: The transcript to judge.

    Returns:
        ``True`` when the turn should be dropped without answering.
    """
    stripped = text.strip()
    return not stripped or stripped.lower() in FILLER_TRANSCRIPTS


def is_off_topic_redirect(answer: str) -> bool:
    """Report whether an answer declined the question as outside the deck.

    The prompt has the agent answer an off-topic question with one sentence
    turning back to the deck and *no* tool call, and that sentence names another
    slide by design: "that's outside this deck, but I can show you the slide on
    tool calling". Handed to the keyword fallback, the offer scores as strong
    evidence for the slide it merely mentioned, so the deck moves on precisely
    the answer that promised not to move it.

    Args:
        answer: The assistant's full answer for the turn.

    Returns:
        ``True`` when the answer declines rather than describes, and so must not
        be scored for navigation.
    """
    normalised = " ".join(_APOSTROPHE_RE.sub("", answer.lower()).split())
    return _OFF_TOPIC_RE.search(normalised) is not None


@dataclass(slots=True)
class TurnResult:
    """What a completed turn produced.

    Attributes:
        answered: Whether the agent actually produced an answer.
        text: The full answer text, as spoken.
        sentences: The answer split into the segments that were emitted.
        actions: Navigation actions applied during the turn.
    """

    answered: bool = False
    text: str = ""
    sentences: list[str] | None = None
    actions: list[SlideAction] | None = None


async def run_turn(
    *,
    turn_id: int,
    text: str,
    deck: Deck,
    llm: LLMProvider,
    tts: TTSProvider,
    voice: str | None,
    history: ConversationHistory,
    slides: SlideController,
    prompts: PromptBuilder,
    metrics: TurnMetrics,
    emit: Emit,
    send_audio: SendAudio,
) -> TurnResult:
    """Run one turn and stream its answer to the client.

    The caller owns cancellation: cancelling the task running this coroutine is
    how barge-in stops the agent. Nothing here suppresses
    :class:`asyncio.CancelledError`.

    Args:
        turn_id: Monotonic id for this turn; stamped on every message so the
            client can discard output from a turn it already interrupted.
        text: The user's words, already transcribed or typed.
        deck: The deck being presented.
        llm: Model provider.
        tts: Synthesis provider.
        voice: Voice identifier, or ``None`` for the provider's default.
        history: Conversation history, mutated in place.
        slides: Navigation state, mutated in place.
        prompts: Builder for the system prompt.
        metrics: Timings for this turn, mutated in place.
        emit: Sends a message to the client.
        send_audio: Sends one binary audio frame to the client.

    Returns:
        A summary of what the turn produced.

    Raises:
        ProviderError: If the model fails. The session converts this into an
            ``error`` message; it is not handled here because the session also
            owns the state transition back to listening.
    """
    result = TurnResult(sentences=[], actions=[])

    if is_filler(text):
        logger.info("turn.dropped_filler", turn_id=turn_id, text=text)
        return result

    await emit(TranscriptUserMsg(turn_id=turn_id, text=text))
    history.add_user(text)
    history.begin_assistant_turn(turn_id)

    chunker = SentenceChunker()
    speech = SpeechSender(
        turn_id=turn_id,
        tts=tts,
        voice=voice,
        history=history,
        metrics=metrics,
        emit=emit,
        send_audio=send_audio,
    )
    spoken = speech.spoken
    navigated = False

    async with speech:
        messages = _build_messages(
            deck=deck, slides=slides, prompts=prompts, history=history, spoken=spoken
        )
        tools = build_tools(len(deck.slides))

        metrics.mark_llm_start()

        # Tool calling takes two round trips. The model answers a navigation
        # question by emitting go_to_slide and stopping, with no spoken content at
        # all -- `finish_reason` is "tool_calls" and the content is empty. The tool
        # results must go back so it can say something. Without this loop the deck
        # moves and the agent stays silent, which is exactly what the first
        # end-to-end run did.
        #
        # Both steps offer the tools, which is the ordinary OpenAI-style exchange:
        # the model calls, it receives the results, and then it speaks.
        #
        # Three cheaper-looking variants were each tried against the live API and
        # each failed, so the plain pattern is a deliberate choice rather than the
        # first thing that worked. Omitting the tools on the second call makes the
        # server default `tool_choice` to none, and a model that has just called a
        # tool calls again, which is rejected with "Tool choice is none, but model
        # called a tool". Declaring the tools with `tool_choice="none"` fails the
        # same way: the setting does not stop the model emitting a call, it only
        # makes the server reject the response. Flattening the tool exchange into a
        # system note removed the API error but produced worse output still -- once,
        # silence, and once the literal text "highlightbullet(1)" spoken aloud,
        # because the model wanted a tool, could not see one, and typed it instead.
        for step in range(MAX_LLM_STEPS):
            finish_reason, applied = await _run_step(
                turn_id=turn_id,
                messages=messages,
                tools=tools,
                llm=llm,
                chunker=chunker,
                speech=speech,
                history=history,
                slides=slides,
                result=result,
                metrics=metrics,
                emit=emit,
            )
            navigated = navigated or applied

            if finish_reason != "tool_calls" or step == MAX_LLM_STEPS - 1:
                break

            # Drain first. `spoken` is the sender's record of what actually
            # reached the client, and the rebuild below replays it so the model
            # does not say it twice. Reading it while sentences were still
            # queued produced exactly that: a live turn answered "Two layers,
            # actually. Two layers, actually." because step two was told nothing
            # had been said yet.
            await speech.drain()

            # Rebuilt, not reused: the deck has moved and the model has already
            # spoken, and the next request has to reflect both.
            messages = _build_messages(
                deck=deck, slides=slides, prompts=prompts, history=history, spoken=spoken
            )

        metrics.mark_llm_done()
        # Everything the model produced has been queued; wait for it to reach the
        # wire before judging what was said. `spoken` is the sender's own record, so
        # it only grows as sentences actually go out.
        await speech.drain()

        # Captured before any sentence of this module's own is added, because the
        # keyword fallback must judge what the *model* said and never a line written
        # here to cover for it.
        model_answer = " ".join(spoken).strip()

        if not spoken:
            # A turn that says nothing is indistinguishable from a crash, and if the
            # deck moved it is worse than that: the room watched a slide change and
            # heard silence. Saying something short and true also keeps an empty
            # assistant entry out of history, which would otherwise be replayed on
            # every later request as an answer that consisted of nothing.
            fallback = f"Here's slide {slides.current_slide}." if navigated else NO_ANSWER_FALLBACK
            await speech.submit(fallback)
            await speech.drain()
            logger.warning(
                "turn.empty_answer",
                turn_id=turn_id,
                navigated=navigated,
                slide=slides.current_slide,
            )

        await speech.finish()
        answer = " ".join(spoken).strip()
        result.text = answer
        result.sentences = spoken
        result.answered = bool(answer)
        metrics.sentences = len(spoken)

        # Fallback routing (TR-062): the model answered about a different slide
        # without calling the tool. Two conditions gate it, and neither is "a tool
        # call was seen". Only a call the deck *applied* counts, so a rejected call
        # no longer suppresses the fallback -- nothing moved, and something still
        # has to route the answer. And an answer that declines the question as
        # outside the deck is never scored at all: the prompt has it name a slide it
        # is offering rather than describing, and scoring that offer would move the
        # deck on the one answer that must leave it alone (PRD §8).
        if not navigated and model_answer and not is_off_topic_redirect(model_answer):
            action = slides.keyword_fallback(model_answer)
            if action is not None:
                await _emit_action(
                    turn_id=turn_id, action=action, history=history, result=result, emit=emit
                )

        history.add_assistant(answer, spoken)
        logger.info(
            "turn.done",
            turn_id=turn_id,
            sentences=len(spoken),
            navigated=navigated,
            slide=slides.current_slide,
        )
        return result


async def _run_step(
    *,
    turn_id: int,
    messages: list[Message],
    tools: list[ToolSpec],
    llm: LLMProvider,
    chunker: SentenceChunker,
    speech: SpeechSender,
    history: ConversationHistory,
    slides: SlideController,
    result: TurnResult,
    metrics: TurnMetrics,
    emit: Emit,
) -> tuple[str, bool]:
    """Consume one model response, speaking and navigating as it arrives.

    Args:
        turn_id: The turn being answered.
        messages: The request for this step.
        tools: Tools declared on the request.
        llm: Model provider.
        chunker: Sentence chunker for the turn, carried across both steps so a
            sentence split by the step boundary is still spoken once.
        speech: Sender that turns segments into transcript messages and audio.
        history: Conversation history, mutated in place.
        slides: Navigation state, mutated in place.
        result: Turn summary, appended to when an action is applied.
        metrics: Timings for this turn, mutated in place.
        emit: Sends a message to the client.

    Returns:
        The reason generation stopped, and whether any tool call in this step
        was actually applied to the deck.
    """
    finish_reason = "stop"
    navigated = False
    stream = llm.stream(messages, tools)
    try:
        async for event in stream:
            if isinstance(event, TokenDelta):
                metrics.mark_first_token()
                for sentence in chunker.feed(event.text):
                    await speech.submit(sentence)
            elif isinstance(event, ToolCallDelta):
                applied = await _apply_tool_call(
                    turn_id=turn_id,
                    event=event,
                    slides=slides,
                    history=history,
                    result=result,
                    emit=emit,
                )
                # Only a call the deck honoured counts as "the model navigated".
                # A rejected call left the deck exactly where it was, so
                # treating it as navigation would silence the keyword fallback
                # for a turn in which nothing moved at all.
                navigated = navigated or applied
            elif isinstance(event, LLMDone):
                finish_reason = event.finish_reason
                for sentence in chunker.flush():
                    await speech.submit(sentence)
                break
    finally:
        await _aclose(stream)
    return finish_reason, navigated


def _build_messages(
    *,
    deck: Deck,
    slides: SlideController,
    prompts: PromptBuilder,
    history: ConversationHistory,
    spoken: Sequence[str],
) -> list[Message]:
    """Build the message list for one model request.

    Two things make the second request of a turn differ from the first. The
    snapshot names the slide the model just navigated to, so the prompt carries
    that slide's notes, which is precisely what it needs in order to speak about
    it. And anything already spoken this turn is replayed as an assistant
    message: history only learns the answer text once the turn ends, so without
    this the model cannot see the sentence it opened with, says it again, and
    the room hears it twice.

    The spoken text is merged INTO the assistant entry that carried the tool
    call, rather than appended after the tool result. That is the shape the API
    documents -- one assistant message holding both content and ``tool_calls``,
    then the result -- and the shape matters more than it looks. Appending the
    sentences as a trailing assistant message instead leaves the conversation
    ending on the assistant's own turn, and a model asked to continue from there
    starts its reply again: a live turn answered "Two layers, actually. Two
    layers, actually." Merging leaves the tool result last, which is a request
    to continue, and the repetition stops.

    Args:
        deck: The deck being presented.
        slides: Navigation state, for the position snapshot.
        prompts: Builder for the system prompt.
        history: Conversation history.
        spoken: Segments already sent to the client during this turn.

    Returns:
        The system prompt followed by the history for this request.
    """
    replayed = history.to_provider_messages()
    if spoken:
        text = " ".join(spoken)
        merged = False
        for index in range(len(replayed) - 1, -1, -1):
            message = replayed[index]
            if message.role == "assistant" and message.tool_calls and not message.content.strip():
                replayed[index] = message.model_copy(update={"content": text})
                merged = True
                break
        if not merged:
            replayed.append(Message(role="assistant", content=text))
    return prompts.build(deck, slides.snapshot(), replayed)


async def _aclose(stream: AsyncIterator[LLMEvent]) -> None:
    """Close a model stream now rather than when the collector notices it.

    :func:`contextlib.aclosing` says exactly this in one line, but
    :meth:`~app.providers.base.LLMProvider.stream` is declared to return a plain
    ``AsyncIterator``, which is not statically known to have ``aclose``. Every
    implementation is in fact an async generator; the check degrades to a no-op
    for one that is not.

    Determinism is the whole point (TR-031, TR-034). Cancelling a turn while the
    consumer is awaiting inside its own loop body leaves the generator suspended
    at its ``yield``, and the provider's raw ``httpx`` stream then stays open
    until the garbage collector runs the finaliser -- which is the very
    connection barge-in needs closed at once. The ordinary path needs it too:
    the loop leaves the generator suspended one statement from its end when it
    breaks on ``LLMDone``.

    Args:
        stream: The iterator the provider returned.
    """
    if isinstance(stream, AsyncGenerator):
        await stream.aclose()


SPEECH_QUEUE_DEPTH = 2
"""Sentences allowed to wait for synthesis at once (TR-034).

Deliberately shallow. A deep queue would let the model run far ahead of the
voice, so an interrupt would have to throw away work already paid for, and the
memory held would grow with the answer. Two keeps synthesis busy without letting
it get ahead of what the listener could plausibly still hear.
"""


class SpeechSender:
    """Turns finished sentences into transcript messages and audio frames.

    Runs as its own task so synthesis never blocks the model stream: tokens keep
    arriving, and an interrupt keeps being noticed, while a sentence is being
    spoken. Sentences are handled strictly in order, because they are one answer
    rather than independent utterances.

    Attributes:
        spoken: Segments actually sent, in order. This is the honest record of
            what reached the client, which is what history truncation needs
            after a barge-in (TR-051).
    """

    def __init__(
        self,
        *,
        turn_id: int,
        tts: TTSProvider,
        voice: str | None,
        history: ConversationHistory,
        metrics: TurnMetrics,
        emit: Emit,
        send_audio: SendAudio,
    ) -> None:
        """Start the sender for one turn.

        Args:
            turn_id: The turn being answered.
            tts: Synthesis provider.
            voice: Voice identifier, or ``None`` for the provider default.
            history: History that records each segment as it is sent.
            metrics: Timings for this turn.
            emit: Sends a JSON message to the client.
            send_audio: Sends one binary audio frame to the client.
        """
        self._turn_id = turn_id
        self._tts = tts
        self._voice = voice
        self._history = history
        self._metrics = metrics
        self._emit = emit
        self._send_audio = send_audio
        self._queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=SPEECH_QUEUE_DEPTH)
        self.spoken: list[str] = []
        # Set before the task exists: `create_task` can schedule `_run` before
        # the rest of this constructor finishes, and `_run` reads this flag.
        self._stopped = False
        self._task: asyncio.Task[None] = asyncio.create_task(self._run(), name=f"speech-{turn_id}")
        self._task.add_done_callback(self._log_result)

    async def submit(self, sentence: str) -> None:
        """Queue one sentence to be spoken.

        Blocks while the queue is full, which is the backpressure that keeps the
        model from running ahead of the voice.

        Args:
            sentence: The segment to speak.

        Raises:
            asyncio.CancelledError: If the sender was cancelled, which is what a
                barge-in landing inside synthesis looks like from here.
            Exception: Whatever killed the sender, re-raised so the turn fails
                loudly instead of blocking.
        """
        await self._raise_if_finished()
        await self._queue.put(sentence)

    async def drain(self) -> None:
        """Wait until every queued sentence has been sent.

        Used before deciding whether the turn said anything, so the decision is
        made against what actually reached the client rather than against what
        happened to have been queued at that instant.
        """
        # Racing the sender's own completion is not defensive padding: a queue
        # whose consumer has died never calls `task_done`, so a plain `join()`
        # would wait for ever. Every failure of the sender has to surface here,
        # because this is where the turn waits for it.
        join: asyncio.Task[None] = asyncio.ensure_future(self._queue.join())
        try:
            await asyncio.wait({join, self._task}, return_when=asyncio.FIRST_COMPLETED)
            if join.done():
                await join
                return
        finally:
            if not join.done():
                join.cancel()
        await self._raise_if_finished()

    async def _raise_if_finished(self) -> None:
        """Re-raise whatever ended the sender, if it has ended.

        Raises:
            asyncio.CancelledError: If the sender was cancelled.
            Exception: The sender's own failure.
        """
        if not self._task.done():
            return
        if self._task.cancelled():
            raise asyncio.CancelledError
        error = self._task.exception()
        if error is not None:
            raise error

    async def finish(self) -> None:
        """Wait for every queued sentence to be spoken."""
        await self._raise_if_finished()
        await self._queue.put(None)
        await self._task

    async def __aenter__(self) -> SpeechSender:
        """Enter the sender's scope.

        Returns:
            This sender.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Decide what happens to queued speech when the turn ends.

        The two failure modes deserve opposite treatment. A cancellation is a
        barge-in: the user is already talking, so anything still queued is
        unwanted by definition and is dropped. Any other failure -- the model
        dropping its connection, a rate limit part-way through -- leaves a real
        partial answer behind, and speaking it beats silence followed by an
        error.

        Args:
            exc_type: The exception class leaving the block, if any.
            exc: The exception instance, if any.
            tb: The traceback, if any.
        """
        if exc_type is not None and not issubclass(exc_type, asyncio.CancelledError):
            with contextlib.suppress(Exception):
                await self.drain()
        self.stop()

    def stop(self) -> None:
        """Stop speaking, without cancelling a send already in flight.

        Deliberately not ``task.cancel()``. Cancelling mid-write leaves the
        WebSocket half-written, which in testing killed the connection rather
        than the turn. Instead a flag is set and a sentinel unblocks the queue,
        so the sender finishes at most the frame it is already sending -- about
        a hundred milliseconds -- and then exits without sending anything more.
        That bound is what barge-in actually needs: no further audio, not a
        thread stopped mid-syscall.

        Also deliberately synchronous. It is called from a ``finally`` block on
        a coroutine that may itself be being cancelled, and awaiting there is
        how cleanup gets interrupted half-done.
        """
        self._stopped = True
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)

    def _log_result(self, task: asyncio.Task[None]) -> None:
        """Log why the sender ended, so a failure is never silent.

        Args:
            task: The finished sender task.
        """
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("speech.failed", turn_id=self._turn_id, error=repr(error))

    async def _run(self) -> None:
        """Speak queued sentences in order until the sentinel arrives."""
        while True:
            sentence = await self._queue.get()
            try:
                if sentence is None or self._stopped:
                    return
                await self._speak(sentence)
            finally:
                self._queue.task_done()

    async def _speak(self, sentence: str) -> None:
        """Send one sentence's transcript and then its audio.

        The transcript goes first (TR-033) so captions never lag the voice.

        A synthesis failure skips this sentence rather than ending the turn
        (TR-173): losing one sentence of an answer is a much smaller harm than
        cutting the answer off, and the transcript still shows what was meant.

        Args:
            sentence: The segment to speak.
        """
        sentence_id = self._history.record_sentence(sentence)
        self.spoken.append(sentence)
        await self._emit(
            TranscriptAgentMsg(turn_id=self._turn_id, sentence_id=sentence_id, text=sentence)
        )

        self._metrics.mark_tts_request()
        seq = 0
        try:
            async for frame in self._tts.synthesize(sentence, self._voice):
                if self._stopped:
                    # Barge-in: the rest of this sentence is no longer wanted.
                    return
                self._metrics.mark_first_audio()
                await self._send_audio(encode_audio_frame(sentence_id, seq, frame))
                seq += 1
        except ProviderError as exc:
            logger.error(
                "tts.sentence_failed",
                turn_id=self._turn_id,
                sentence_id=sentence_id,
                provider=exc.provider,
                message=exc.message,
            )


async def _apply_tool_call(
    *,
    turn_id: int,
    event: ToolCallDelta,
    slides: SlideController,
    history: ConversationHistory,
    result: TurnResult,
    emit: Emit,
) -> bool:
    """Validate a tool call, apply it, and tell both the client and the model.

    An invalid call is not an error: the model is told why it was rejected so it
    can correct itself on the next step, and the deck is left alone (TR-061).

    Args:
        turn_id: The turn being answered.
        event: The reassembled tool call.
        slides: Navigation state.
        history: History, which records the call and its result.
        result: Turn summary, appended to when an action is applied.
        emit: Sends a message to the client.

    Returns:
        ``True`` when the deck acted on the call, ``False`` when it was
        rejected. The caller needs the difference: a rejected call moved
        nothing, so the turn has not navigated and the keyword fallback is still
        the only thing that can route the answer.
    """
    history.add_tool_call(event.call_id, event.name, event.arguments)
    action = slides.apply_tool(event.name, event.arguments)

    if action is None:
        reason = slides.last_error or "rejected"
        logger.warning("tool.rejected", turn_id=turn_id, name=event.name, reason=reason)
        history.add_tool_result(event.call_id, event.name, reason)
        return False

    history.add_tool_result(
        event.call_id,
        event.name,
        json.dumps({"ok": True, "current_slide": slides.current_slide}),
    )
    await _announce_navigation(
        turn_id=turn_id,
        call=ToolCallMsg(
            turn_id=turn_id,
            name=event.name,
            args=event.arguments,
            source=ToolSource.LLM,
        ),
        action=action,
        emit=emit,
    )
    if result.actions is not None:
        result.actions.append(action)
    return True


async def _emit_action(
    *,
    turn_id: int,
    action: SlideAction,
    history: ConversationHistory,
    result: TurnResult,
    emit: Emit,
) -> None:
    """Announce a navigation the keyword fallback inferred.

    Args:
        turn_id: The turn being answered.
        action: The inferred navigation.
        history: History, which records a note so the model knows the deck moved.
        result: Turn summary, appended to.
        emit: Sends a message to the client.
    """
    logger.info("tool.fallback", turn_id=turn_id, slide=action.index, reason=action.reason)
    history.add_system_note(f"[Deck moved to slide {action.index} by keyword match]")
    await _announce_navigation(
        turn_id=turn_id,
        call=ToolCallMsg(
            turn_id=turn_id,
            name="go_to_slide",
            args={"slide_index": action.index, "reason": action.reason},
            source=ToolSource.FALLBACK,
        ),
        action=action,
        emit=emit,
    )
    if result.actions is not None:
        result.actions.append(action)


async def _announce_navigation(
    *,
    turn_id: int,
    call: ToolCallMsg,
    action: SlideAction,
    emit: Emit,
) -> None:
    """Send the event-log entry and the slide change as one indivisible pair.

    The controller has already moved by the time this runs, so a client told
    that a tool fired but never told where the deck went shows a different slide
    from the one the agent is describing -- for the rest of the session, with
    nothing to reconcile the two. Cancellation is delivered at an ``await``, and
    the ``await`` between these two sends is the one place it must not land;
    ``finally`` moves it to after the pair.

    A cut arriving *before* the pair degrades to sending the ``slide.goto``
    alone, which costs a line in the event log and keeps the deck honest. That
    is the right way round: the audience sees slides, not logs.

    Args:
        turn_id: The turn this navigation belongs to.
        call: The event-log entry naming who asked for the move.
        action: The navigation the controller applied.
        emit: Sends a message to the client.
    """
    try:
        await emit(call)
    finally:
        await _send_goto(turn_id, action, emit)


async def _send_goto(turn_id: int, action: SlideAction, emit: Emit) -> None:
    """Send the navigation message for an applied action.

    Args:
        turn_id: The turn this navigation belongs to, so the client can drop it
            if that turn has already been interrupted.
        action: The navigation to send.
        emit: Sends a message to the client.
    """
    await emit(
        SlideGotoMsg(
            turn_id=turn_id,
            index=action.index,
            highlight=action.highlight,
            reason=action.reason,
        )
    )


def provider_error_message(exc: ProviderError) -> ErrorMsg:
    """Translate a provider failure into the client-facing error message.

    Args:
        exc: The failure raised by a provider.

    Returns:
        The ``error`` message to send. Provider failures are recoverable: the
        session returns to listening and the user may simply ask again.
    """
    code = {
        "groq_llm": ErrorCode.LLM_FAILED,
        "ollama": ErrorCode.LLM_FAILED,
        "groq_stt": ErrorCode.STT_FAILED,
        "kokoro": ErrorCode.TTS_FAILED,
    }.get(exc.provider, ErrorCode.LLM_FAILED)
    if exc.retryable and exc.retry_after is not None:
        code = ErrorCode.RATE_LIMITED
    return ErrorMsg(code=code, message=exc.message, recoverable=True)


async def cancel_task(task: asyncio.Task[None] | None) -> None:
    """Cancel a turn task and wait for it to finish unwinding.

    Awaiting the cancellation matters: without it a new turn could start while
    the old one is still inside the model stream, and both would write to the
    same history.

    Args:
        task: The task to cancel, or ``None``.

    Raises:
        asyncio.CancelledError: If *this* coroutine is cancelled while waiting.
            Two different cancellations surface at the same ``await``: the turn
            acknowledging the one issued here, which is the expected reply and
            is swallowed, and one aimed at this coroutine, which must be allowed
            to keep unwinding. :func:`contextlib.suppress` cannot tell them
            apart and swallowed both, which silently stranded a caller that had
            itself been cancelled.
    """
    if task is None or task.done():
        return
    caller = asyncio.current_task()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        # `cancelling()` counts cancel requests made against *this* task, so it
        # is non-zero only when somebody asked this coroutine to stop. The turn
        # acknowledging its own cancellation never touches that counter.
        if caller is not None and caller.cancelling() > 0:
            raise
