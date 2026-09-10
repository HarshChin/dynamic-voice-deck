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
import json
import re
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
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
)
from ..providers.base import (
    LLMDone,
    LLMEvent,
    LLMProvider,
    Message,
    TokenDelta,
    ToolCallDelta,
    ToolSpec,
)
from .chunker import SentenceChunker
from .history import ConversationHistory
from .metrics import TurnMetrics
from .prompt import PromptBuilder
from .slides import SlideAction, SlideController
from .tools import build_tools

logger = get_logger(__name__)

Emit = Callable[[ServerMessage], Awaitable[None]]
"""Sends one message to the client. Supplied by the session."""

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
    history: ConversationHistory,
    slides: SlideController,
    prompts: PromptBuilder,
    metrics: TurnMetrics,
    emit: Emit,
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
        history: Conversation history, mutated in place.
        slides: Navigation state, mutated in place.
        prompts: Builder for the system prompt.
        metrics: Timings for this turn, mutated in place.
        emit: Sends a message to the client.

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
    spoken: list[str] = []
    navigated = False

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
            spoken=spoken,
            history=history,
            slides=slides,
            result=result,
            metrics=metrics,
            emit=emit,
        )
        navigated = navigated or applied

        if finish_reason != "tool_calls" or step == MAX_LLM_STEPS - 1:
            break

        # Rebuilt, not reused: the deck has moved and the model has already
        # spoken, and the next request has to reflect both.
        messages = _build_messages(
            deck=deck, slides=slides, prompts=prompts, history=history, spoken=spoken
        )

    metrics.mark_llm_done()

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
        await _emit_sentence(
            turn_id=turn_id,
            sentence=fallback,
            spoken=spoken,
            history=history,
            metrics=metrics,
            emit=emit,
        )
        logger.warning(
            "turn.empty_answer",
            turn_id=turn_id,
            navigated=navigated,
            slide=slides.current_slide,
        )

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
    spoken: list[str],
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
        spoken: Accumulator of segments sent so far, appended to.
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
                    await _emit_sentence(
                        turn_id=turn_id,
                        sentence=sentence,
                        spoken=spoken,
                        history=history,
                        metrics=metrics,
                        emit=emit,
                    )
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
                    await _emit_sentence(
                        turn_id=turn_id,
                        sentence=sentence,
                        spoken=spoken,
                        history=history,
                        metrics=metrics,
                        emit=emit,
                    )
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

    The replayed sentences sit at the end, after the tool call and its result,
    rather than merged into the assistant entry that carried the call. Merging
    them is :mod:`~app.pipeline.history`'s business, not this module's; the
    order still reads correctly to the model, which is what the fix is for.

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
        replayed.append(Message(role="assistant", content=" ".join(spoken)))
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


async def _emit_sentence(
    *,
    turn_id: int,
    sentence: str,
    spoken: list[str],
    history: ConversationHistory,
    metrics: TurnMetrics,
    emit: Emit,
) -> None:
    """Send one sentence of the answer and record it as spoken.

    Recording happens as the sentence is sent, not at the end of the turn,
    because an interrupt must be able to truncate history to exactly what the
    listener heard (TR-051).

    Args:
        turn_id: The turn being answered.
        sentence: The segment to send.
        spoken: Accumulator of segments sent so far, appended to.
        history: History whose in-progress turn records the segment.
        metrics: Timings, marked on the first segment.
        emit: Sends a message to the client.
    """
    sentence_id = history.record_sentence(sentence)
    spoken.append(sentence)
    metrics.mark_tts_request()
    # Milestone M2 wires synthesis here: hand `sentence` to the TTS provider and
    # stream its audio frames, calling `metrics.mark_first_audio()` on the first
    # chunk. The transcript deliberately precedes the audio (TR-033).
    metrics.mark_first_audio()
    await emit(TranscriptAgentMsg(turn_id=turn_id, sentence_id=sentence_id, text=sentence))


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
