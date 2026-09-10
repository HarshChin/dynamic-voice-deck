"""Split a streaming answer into speakable segments (TRD TR-040 - TR-045, TR-086).

Kokoro synthesises a whole utterance in one blocking call (TR-083), so nothing
reaches the listener until a segment is *complete*: the first segment a turn
produces is very nearly the whole of ``first_audio_ms``. That makes this module
a latency component rather than a formatting one (TR-086), and it is why a long
clause is broken at a comma (TR-042) instead of waiting for the full stop.

The chunker is a streaming transducer. Every decision is taken from the text
seen so far, one character at a time, and never from lookahead, so the segments
depend only on *what* was streamed and not on *how* the stream was cut into
fragments. That invariance is what makes the behaviour testable at all
(TC-BE-018): a real token stream breaks at arbitrary points, so feeding
``"Hello there. "`` must agree with feeding ``"He"``, ``"llo the"``, ``"re. "``.

Segment identifiers are the caller's job (TR-045): segments come back in
emission order and the turn numbers them ``0, 1, 2, ...``.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from ..logging_setup import get_logger

logger = get_logger(__name__)

SENTENCE_TERMINATORS: frozenset[str] = frozenset(".!?")
"""Characters that end a sentence when whitespace follows them (TR-041)."""

EARLY_SPLIT_BOUNDARIES: frozenset[str] = frozenset(",;:—")
"""Clause boundaries -- comma, semicolon, colon, em dash -- used by TR-042."""

# The two escapes are the curly closing double and single quotes; they are
# spelled out because a literal curly quote is easy to confuse in source.
CLOSING_PUNCTUATION: frozenset[str] = frozenset("\"')]}\u201d\u2019")
"""Characters allowed between a terminator and the whitespace that follows it.

Written English closes a quotation *after* the full stop (``he said "no." Then
we moved on``), so a terminator still ends a sentence when only closers stand
between it and the space.
"""

# Curly opening double and single quotes, escaped for the same reason.
OPENING_PUNCTUATION: str = "(\"'[{\u201c\u2018"
"""Characters stripped from the front of a word before the abbreviation test."""

ABBREVIATIONS: frozenset[str] = frozenset(
    {"approx.", "dr.", "e.g.", "etc.", "i.e.", "mr.", "mrs.", "vs."}
)
"""Lower-cased words whose trailing full stop never ends a sentence (TR-041)."""

EARLY_SPLIT_MIN_CHARS: int = 60
"""Length above which a clause boundary is good enough to speak on (TR-042)."""

HARD_SPLIT_MAX_CHARS: int = 200
"""Length above which the buffer is broken at whitespace regardless (TR-043)."""

_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
"""Markdown link or image; only the visible label survives."""

_MARKDOWN_SYMBOLS_RE = re.compile(r"[*_#`\[\]]")
"""Markdown punctuation a synthesiser would either read out or mangle."""


def _last_index(text: str, matches: Callable[[str], bool]) -> int:
    """Return the index of the last character satisfying a predicate.

    Args:
        text: Text to scan.
        matches: Predicate applied to a single character.

    Returns:
        The highest index whose character matches, or ``-1`` if none does.
    """
    for offset in range(len(text) - 1, -1, -1):
        if matches(text[offset]):
            return offset
    return -1


def _clean_segment(raw: str) -> str:
    """Turn a raw slice of the buffer into text fit for a synthesiser.

    Markdown is removed and whitespace collapsed, because the segment is spoken
    rather than displayed: asterisks and backticks are either read aloud or
    swallow the words around them, and a stray newline becomes an odd pause.

    Args:
        raw: Buffer slice, possibly padded with whitespace or markdown.

    Returns:
        The speakable text, or an empty string when nothing speakable remains.
    """
    return " ".join(_MARKDOWN_SYMBOLS_RE.sub("", _LINK_RE.sub(r"\1", raw)).split())


class SentenceChunker:
    """Accumulate streamed text and release it as complete speakable segments.

    One instance serves one turn. It holds a private buffer of the text fed so
    far that has not yet been released, and releases a segment as soon as one of
    three rules fires: a sentence ends (TR-041), the buffer is long enough that
    a clause boundary is worth speaking on (TR-042), or it has grown past the
    hard limit with nowhere better to break (TR-043).

    Example:
        >>> chunker = SentenceChunker()
        >>> chunker.feed("Slide three covers barge-in. ")
        ['Slide three covers barge-in.']
        >>> chunker.flush()
        []
    """

    def __init__(self) -> None:
        self._buffer: str = ""
        # Both indices are maintained as characters arrive, so that neither
        # split rule has to rescan the buffer on every single character.
        self._last_boundary: int = -1
        self._last_space: int = -1

    def feed(self, text: str) -> list[str]:
        """Accept the next fragment of streamed text and release what is ready.

        Args:
            text: The next fragment. It may be empty, a single character, or
                several sentences; the result does not depend on where the
                stream happened to be cut.

        Returns:
            Zero or more segments, cleaned, non-empty, and in emission order.
            The caller numbers them ``0, 1, 2, ...`` across the turn (TR-045).
        """
        segments: list[str] = []
        for char in text:
            raw = self._consume(char)
            if raw is None:
                continue
            cleaned = _clean_segment(raw)
            # A slice that was only whitespace or only markdown is dropped
            # rather than sent to the synthesiser as silence (TR-044).
            if cleaned:
                segments.append(cleaned)
        return segments

    def flush(self) -> list[str]:
        """Release whatever is still buffered and leave the buffer empty.

        Called once the model has finished, when the trailing text will never be
        completed by a terminator. The remainder holds at most one segment: any
        earlier sentence end would already have released everything before it.

        Returns:
            A single-item list with the remaining text, or an empty list when
            nothing speakable remains.
        """
        cleaned = _clean_segment(self._buffer)
        self._buffer = ""
        self._last_boundary = -1
        self._last_space = -1
        return [cleaned] if cleaned else []

    def _consume(self, char: str) -> str | None:
        """Append one character and release a segment if it now completes one.

        Args:
            char: The character to append.

        Returns:
            The raw text of a completed segment, or ``None`` when the buffer is
            not ready to release one.
        """
        index = len(self._buffer)
        self._buffer += char
        if char in EARLY_SPLIT_BOUNDARIES:
            self._last_boundary = index
        if char.isspace():
            self._last_space = index
            if self._ends_sentence(index):
                # The whitespace separates two sentences; it is not content.
                return self._cut(index, index + 1)

        length = index + 1
        if length > EARLY_SPLIT_MIN_CHARS and self._last_boundary >= 0:
            # Breaking at the *last* boundary keeps the release as late as the
            # rule allows, which is still the earliest moment the limit is met.
            return self._cut(self._last_boundary + 1, self._last_boundary + 1)
        if length > HARD_SPLIT_MAX_CHARS and self._last_space >= 0:
            # No boundary can be pending here: one would have fired the rule
            # above at 61 characters. This is prose with no punctuation at all.
            logger.debug("chunker.hard_split", length=length)
            return self._cut(self._last_space, self._last_space + 1)
        return None

    def _cut(self, end: int, resume: int) -> str:
        """Split the buffer, keeping the tail and returning the head.

        Args:
            end: Exclusive end of the segment being released.
            resume: Index the retained buffer restarts from; characters between
                ``end`` and ``resume`` are separators and are discarded.

        Returns:
            The released slice, uncleaned.
        """
        segment = self._buffer[:end]
        self._buffer = self._buffer[resume:]
        self._last_boundary = _last_index(self._buffer, EARLY_SPLIT_BOUNDARIES.__contains__)
        self._last_space = _last_index(self._buffer, str.isspace)
        return segment

    def _ends_sentence(self, space_index: int) -> bool:
        """Report whether the text before a whitespace character ends a sentence.

        Args:
            space_index: Index of the whitespace character just appended.

        Returns:
            ``True`` when the preceding text closes a sentence (TR-041).
        """
        end = space_index - 1
        while end >= 0 and self._buffer[end] in CLOSING_PUNCTUATION:
            end -= 1
        if end < 0 or self._buffer[end] not in SENTENCE_TERMINATORS:
            return False
        return not self._is_abbreviation(end)

    def _is_abbreviation(self, terminator_index: int) -> bool:
        """Report whether a full stop belongs to a known abbreviation.

        A decimal such as ``3.5`` needs no special case: its point is followed
        by a digit rather than whitespace, so it never reaches this test. An
        abbreviation at the very end of the buffer needs none either, because a
        terminator is only ever judged once the whitespace after it has arrived.

        Args:
            terminator_index: Index of the terminator being judged.

        Returns:
            ``True`` when the word ending at that terminator is an abbreviation.
        """
        if self._buffer[terminator_index] != ".":
            return False
        start = _last_index(self._buffer[:terminator_index], str.isspace) + 1
        word = self._buffer[start : terminator_index + 1].lstrip(OPENING_PUNCTUATION)
        return word.lower() in ABBREVIATIONS
