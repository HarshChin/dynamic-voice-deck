"""Conversation history for the presenter agent (TRD TR-050 to TR-054).

Barge-in is the reason this is not a plain list of messages. When the user cuts
the agent off, every sentence the model generated after the last one the room
actually heard never happened as far as the audience is concerned. Leaving that
unheard text in history makes the model believe it already made the point, so
when the user asks again it answers "as I mentioned" and says nothing new.
:meth:`ConversationHistory.truncate_current` therefore rewrites the interrupted
turn to hold only the spoken sentences plus an explicit marker.

Tool calls are the exception: a ``go_to_slide`` that already ran moved the deck
on screen, and pretending otherwise would leave the model describing a slide the
audience is no longer looking at. Truncation keeps them (TR-051).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..config import get_settings
from ..logging_setup import get_logger
from ..providers.base import Message

logger = get_logger(__name__)

INTERRUPTED_MARKER = "[interrupted by user]"
"""Appended to the sentences that were spoken before a barge-in (TR-051)."""

INTERRUPTED_BEFORE_SPEAKING = "[interrupted by user before speaking]"
"""Whole content of a turn interrupted before its first sentence was heard."""

Role = Literal["system", "user", "assistant", "tool"]
"""Roles a history entry may carry; mirrors :attr:`app.providers.base.Message.role`."""


class HistoryMessage(BaseModel):
    """One retained entry of the conversation (TR-050).

    This is the internal record, a superset of the provider's
    :class:`~app.providers.base.Message`. The extra fields exist so that a turn
    can be rewritten after the fact; they are dropped on the way out (TR-054).

    Attributes:
        role: Who produced the entry.
        content: The text. Empty for an assistant entry that only called tools.
        tool_calls: Calls the assistant made, in provider wire format.
        tool_call_id: For a ``tool`` entry, the call it answers.
        name: For a ``tool`` entry, the tool's name.
        sentences: For an assistant entry, the segments as spoken. Internal
            bookkeeping for truncation; never sent to a provider.
        turn_id: Turn the entry belongs to, when it was produced inside one.
    """

    role: Role
    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    sentences: list[str] = Field(default_factory=list)
    turn_id: int | None = None

    def to_provider(self) -> Message:
        """Render this entry in the shape the provider accepts.

        Returns:
            An equivalent :class:`~app.providers.base.Message` without the
            ``sentences`` and ``turn_id`` bookkeeping (TR-054).
        """
        return Message(
            role=self.role,
            content=self.content,
            tool_calls=self.tool_calls,
            tool_call_id=self.tool_call_id,
            name=self.name,
        )


@dataclass
class _PendingTurn:
    """The assistant turn currently being generated and spoken.

    Attributes:
        turn_id: Identifier of the turn, matching ``Session.turn_id``.
        sentences: Segments handed to TTS so far, in order.
        message: The entry appended for this turn, once one exists.
        truncated: Whether the turn was cut short by the user.
    """

    turn_id: int
    sentences: list[str] = field(default_factory=list)
    message: HistoryMessage | None = None
    truncated: bool = False


def _render_interrupted(
    sentences: Sequence[str],
    last_completed_sentence_id: int | None,
) -> tuple[str, list[str]]:
    """Build the content of an interrupted assistant turn (TR-051).

    Args:
        sentences: Every segment the turn produced, in order.
        last_completed_sentence_id: Id of the last segment the user heard in
            full, or ``None`` when playback had not started.

    Returns:
        A pair of the replacement content and the segments it covers.
    """
    if last_completed_sentence_id is None:
        return INTERRUPTED_BEFORE_SPEAKING, []
    # A slice, not an index lookup: an id past the end simply means every
    # recorded sentence was heard, which happens when the cut lands in the gap
    # between two sentences. A negative id degrades to "nothing was heard".
    spoken = list(sentences[: last_completed_sentence_id + 1])
    if not spoken:
        return INTERRUPTED_BEFORE_SPEAKING, []
    return f"{' '.join(spoken)} {INTERRUPTED_MARKER}", spoken


class ConversationHistory:
    """The message log for one session, with truncation and capping.

    The system prompt is pinned and never dropped; everything else is grouped
    into exchanges that start at a ``user`` message, and only the most recent
    ``max_turns`` exchanges are kept (TR-052). Because an exchange is dropped
    whole, a ``tool`` entry can never outlive the assistant entry that called
    it -- an orphaned ``tool`` message is a hard error at every provider.

    Args:
        max_turns: Exchanges to retain. Defaults to
            :attr:`~app.config.Settings.max_history_turns`.

    Raises:
        ValueError: If ``max_turns`` is less than one.
    """

    def __init__(self, max_turns: int | None = None) -> None:
        resolved = get_settings().max_history_turns if max_turns is None else max_turns
        if resolved < 1:
            msg = f"max_turns must be at least 1, got {resolved}"
            raise ValueError(msg)
        self._max_turns = resolved
        self._system: list[HistoryMessage] = []
        self._body: list[HistoryMessage] = []
        self._pending: _PendingTurn | None = None

    # --- Inspection ---------------------------------------------------------

    @property
    def max_turns(self) -> int:
        """Number of user/assistant exchanges retained beyond the system prompt."""
        return self._max_turns

    @property
    def current_turn_id(self) -> int | None:
        """Turn id of the answer most recently begun, or ``None`` when none was.

        Survives :meth:`truncate_current` so that a second, more precise
        interrupt for the same turn is still recognised as current.
        """
        return self._pending.turn_id if self._pending is not None else None

    @property
    def messages(self) -> tuple[HistoryMessage, ...]:
        """Return a snapshot of every retained entry, system prompt first.

        Returns:
            Deep copies, so a caller inspecting history cannot corrupt it.
        """
        return tuple(m.model_copy(deep=True) for m in (*self._system, *self._body))

    def to_provider_messages(self) -> list[Message]:
        """Serialise the history for an LLM request (TR-054).

        Returns:
            The retained entries oldest first, system prompt included, with the
            internal ``sentences`` bookkeeping removed.
        """
        return [m.to_provider() for m in (*self._system, *self._body)]

    # --- Appending ----------------------------------------------------------

    def add_system(self, content: str) -> None:
        """Pin a system message that capping will never drop (TR-052).

        Optional, and each call pins another message. A session driven through
        :class:`~app.pipeline.prompt.PromptBuilder` should leave this unused,
        because that builder re-renders the system prompt for every request
        (TR-070) and prepends it; pinning one here as well would send two.

        Args:
            content: The system prompt.
        """
        self._system.append(HistoryMessage(role="system", content=content))

    def add_user(self, text: str) -> None:
        """Append the user's turn and drop any exchange beyond the cap.

        Args:
            text: What the user said or typed.
        """
        self._body.append(HistoryMessage(role="user", content=text))
        # Capping runs here rather than on read: a user message is the only
        # boundary at which an exchange can be dropped without splitting one.
        self._cap()

    def add_assistant(self, text: str, sentences: Sequence[str] | None = None) -> None:
        """Append the assistant's completed answer.

        A turn that was already truncated is left alone: the user interrupted it,
        and re-adding the full text would restore exactly the sentences nobody
        heard. This is the race between an in-flight interrupt and a turn that
        finishes a few milliseconds later.

        Args:
            text: The full answer as generated.
            sentences: The segments as spoken. Defaults to those recorded by
                :meth:`record_sentence` for the turn in progress.
        """
        pending = self._pending
        if pending is not None and pending.truncated:
            logger.warning("history.assistant_after_truncation", turn_id=pending.turn_id)
            return
        spoken = list(sentences) if sentences is not None else self._pending_sentences()
        message = HistoryMessage(
            role="assistant",
            content=text,
            sentences=spoken,
            turn_id=pending.turn_id if pending is not None else None,
        )
        self._body.append(message)
        self._pending = None

    def add_tool_call(self, call_id: str, name: str, arguments: dict[str, Any]) -> None:
        """Append the assistant entry recording one tool call.

        Each call gets its own assistant entry so that it is always followed
        immediately by its own result, which is the ordering every
        OpenAI-compatible provider validates.

        Args:
            call_id: Provider identifier, echoed back by the matching result.
            name: Tool that was called.
            arguments: Parsed arguments, re-encoded as the wire format's string.
        """
        self._body.append(
            HistoryMessage(
                role="assistant",
                tool_calls=[
                    {
                        "id": call_id,
                        "type": "function",
                        # Compact separators: this string is replayed on every
                        # later request, so its tokens are paid for repeatedly.
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments, separators=(",", ":")),
                        },
                    }
                ],
                turn_id=self.current_turn_id,
            )
        )

    def add_tool_result(self, call_id: str, name: str, content: str) -> None:
        """Append the result of a tool call.

        Args:
            call_id: The call this answers.
            name: Tool that was called.
            content: What to tell the model, including validation errors so it
                can correct itself (TR-061).
        """
        self._body.append(
            HistoryMessage(
                role="tool",
                content=content,
                tool_call_id=call_id,
                name=name,
                turn_id=self.current_turn_id,
            )
        )

    def add_system_note(self, text: str) -> None:
        """Append an out-of-band note as a system message (TR-053).

        Used for things that happened to the deck without the agent doing them,
        e.g. ``"[User manually moved to slide 4: Barge-in]"``. Unlike the pinned
        system prompt, a note ages out with the exchange it sits in.

        Args:
            text: The note, already formatted for the model.
        """
        self._body.append(HistoryMessage(role="system", content=text))

    # --- The turn in progress -----------------------------------------------

    def begin_assistant_turn(self, turn_id: int) -> None:
        """Start tracking the sentences of a new assistant answer.

        Args:
            turn_id: Identifier of the turn about to be generated.
        """
        stale = self._pending
        if stale is not None and stale.message is None and stale.sentences:
            # Neither completed nor truncated: the turn died some other way, and
            # what it spoke is about to be lost. Loud, because it means the
            # caller skipped truncate_current on an error path.
            logger.warning(
                "history.pending_turn_discarded",
                turn_id=stale.turn_id,
                sentences=len(stale.sentences),
            )
        self._pending = _PendingTurn(turn_id=turn_id)

    def record_sentence(self, text: str) -> int:
        """Record a segment as it is handed to TTS.

        Truncation can only be honest if history knows what was said before the
        cut, and that is known sentence by sentence as they are sent.

        Args:
            text: The segment, already stripped of markdown.

        Returns:
            The segment's id within the turn, counting from zero (TR-045).

        Raises:
            RuntimeError: If no assistant turn is in progress.
        """
        if self._pending is None:
            msg = "record_sentence called with no assistant turn in progress"
            raise RuntimeError(msg)
        self._pending.sentences.append(text)
        return len(self._pending.sentences) - 1

    def truncate_current(self, turn_id: int, last_completed_sentence_id: int | None) -> bool:
        """Rewrite the interrupted turn to only what the user heard (TR-051).

        Tool calls already recorded for the turn are left in place: the slide
        really did move. Calling this again for the same turn refines the cut,
        which is what happens when the client follows a VAD-triggered interrupt
        with a more precise ``last_completed_sentence_id`` (TR-023).

        Args:
            turn_id: The turn the interrupt refers to.
            last_completed_sentence_id: Id of the last segment heard in full, or
                ``None`` when playback had not started.

        Returns:
            ``True`` if the turn was rewritten. ``False`` when ``turn_id`` is not
            the turn in progress, in which case nothing changes: a duplicate or
            late interrupt is a routine race (TR-024), not an error.
        """
        pending = self._pending
        if pending is None or pending.turn_id != turn_id:
            logger.warning(
                "history.truncate_ignored",
                turn_id=turn_id,
                current_turn_id=self.current_turn_id,
            )
            return False

        content, spoken = _render_interrupted(pending.sentences, last_completed_sentence_id)
        if pending.message is None:
            pending.message = HistoryMessage(
                role="assistant",
                content=content,
                sentences=spoken,
                turn_id=turn_id,
            )
            self._body.append(pending.message)
        else:
            pending.message.content = content
            pending.message.sentences = spoken
        pending.truncated = True
        logger.info(
            "history.truncated",
            turn_id=turn_id,
            last_completed_sentence_id=last_completed_sentence_id,
            spoken=len(spoken),
            generated=len(pending.sentences),
        )
        return True

    # --- Internals ----------------------------------------------------------

    def _pending_sentences(self) -> list[str]:
        """Return the segments recorded for the turn in progress."""
        return list(self._pending.sentences) if self._pending is not None else []

    def _cap(self) -> None:
        """Drop the oldest exchanges until only ``max_turns`` remain (TR-052).

        An exchange runs from a ``user`` message up to the next one, so cutting
        at a ``user`` boundary removes each assistant entry together with the
        tool entries that answer it.
        """
        starts = [i for i, message in enumerate(self._body) if message.role == "user"]
        if len(starts) <= self._max_turns:
            return
        cut = starts[len(starts) - self._max_turns]
        del self._body[:cut]
        logger.debug("history.capped", dropped=cut, kept_turns=self._max_turns)
