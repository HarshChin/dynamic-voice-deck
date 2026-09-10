"""One conversational turn, from input to spoken answer (TRD §4.4).

A turn is a single cancellable coroutine. That is the whole design: barge-in is
implemented by cancelling this task (TR-034), which propagates into the model's
HTTP stream and closes it, so the agent stops generating rather than merely
being ignored.

Phase 1 runs the text half of the pipeline: input, model stream, tool calls,
sentence chunking, and the per-sentence transcript. Synthesis is wired through
the same sentence loop in a later milestone; the seam is marked below.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

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
from ..providers.base import LLMDone, LLMProvider, TokenDelta, ToolCallDelta
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


def is_filler(text: str) -> bool:
    """Report whether a transcript is empty or a known silence artefact.

    Args:
        text: The transcript to judge.

    Returns:
        ``True`` when the turn should be dropped without answering.
    """
    stripped = text.strip()
    return not stripped or stripped.lower() in FILLER_TRANSCRIPTS


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

    messages = prompts.build(deck, slides.snapshot(), history.to_provider_messages())
    tools = build_tools(len(deck.slides))

    chunker = SentenceChunker()
    spoken: list[str] = []
    called_tool = False

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
        finish_reason = "stop"
        async for event in llm.stream(messages, tools):
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
                called_tool = True
                await _apply_tool_call(
                    turn_id=turn_id,
                    event=event,
                    slides=slides,
                    history=history,
                    result=result,
                    emit=emit,
                )
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

        if finish_reason != "tool_calls" or step == MAX_LLM_STEPS - 1:
            break

        # Rebuild against the moved deck: the snapshot now names the slide the
        # model just navigated to, so the prompt carries that slide's notes,
        # which is precisely what it needs in order to speak about it.
        messages = prompts.build(deck, slides.snapshot(), history.to_provider_messages())

    metrics.mark_llm_done()

    if not spoken and called_tool:
        # The deck moved but the model said nothing. Rare, but silence after a
        # visible slide change reads as a broken app, so say something true and
        # short rather than nothing at all.
        fallback = f"Here's slide {slides.current_slide}."
        await _emit_sentence(
            turn_id=turn_id,
            sentence=fallback,
            spoken=spoken,
            history=history,
            metrics=metrics,
            emit=emit,
        )
        logger.warning("turn.empty_answer_after_tool", turn_id=turn_id, slide=slides.current_slide)

    answer = " ".join(spoken).strip()
    result.text = answer
    result.sentences = spoken
    result.answered = bool(answer)
    metrics.sentences = len(spoken)

    # Fallback routing (TR-062): the model answered about a different slide
    # without calling the tool. Only consult it when no tool fired, so a
    # deliberate "stay here" answer is never overridden by keyword noise.
    if not called_tool and answer:
        action = slides.keyword_fallback(answer)
        if action is not None:
            await _emit_action(
                turn_id=turn_id, action=action, history=history, result=result, emit=emit
            )

    history.add_assistant(answer, spoken)
    logger.info(
        "turn.done",
        turn_id=turn_id,
        sentences=len(spoken),
        tool_called=called_tool,
        slide=slides.current_slide,
    )
    return result


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
) -> None:
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
    """
    history.add_tool_call(event.call_id, event.name, event.arguments)
    action = slides.apply_tool(event.name, event.arguments)

    if action is None:
        reason = slides.last_error or "rejected"
        logger.warning("tool.rejected", turn_id=turn_id, name=event.name, reason=reason)
        history.add_tool_result(event.call_id, event.name, reason)
        return

    history.add_tool_result(
        event.call_id,
        event.name,
        json.dumps({"ok": True, "current_slide": slides.current_slide}),
    )
    await emit(
        ToolCallMsg(
            turn_id=turn_id,
            name=event.name,
            args=event.arguments,
            source=ToolSource.LLM,
        )
    )
    await _send_goto(turn_id, action, emit)
    if result.actions is not None:
        result.actions.append(action)


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
    await emit(
        ToolCallMsg(
            turn_id=turn_id,
            name="go_to_slide",
            args={"slide_index": action.index, "reason": action.reason},
            source=ToolSource.FALLBACK,
        )
    )
    await _send_goto(turn_id, action, emit)
    if result.actions is not None:
        result.actions.append(action)


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
    """
    if task is None or task.done():
        return
    task.cancel()
    # Suppressing CancelledError is correct here and only here: this coroutine
    # issued the cancel, so the exception is the expected acknowledgement rather
    # than a signal that *we* are being cancelled.
    with contextlib.suppress(asyncio.CancelledError):
        await task
