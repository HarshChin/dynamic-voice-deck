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

EARLY_SPLIT_MIN_HEAD_CHARS: int = 24
"""Shortest head the early split may produce (TR-042, TR-161).

The rule fires once the buffer passes 60 characters, so without a floor a clause
boundary near the start -- ``So,`` ``Well,`` ``Two layers:`` -- is released on
its own while the other fifty-odd characters stay buffered. That loses on both
counts the early split is meant to win. The fragment is synthesised as a whole
utterance, so it lands with a falling, finished prosody in the middle of a
sentence; and its playback is far shorter than the time Kokoro needs for what
follows, so the listener hears two words and then a gap.

Twenty-four characters is roughly a second and a half of speech. Because the
rule fires at 61 characters, a head that long leaves at most thirty-seven
characters behind it, and speaking the head covers synthesising them. A boundary
earlier than this is skipped rather than the rule disabled: the next boundary,
the sentence end (TR-041), or the hard limit (TR-043) releases the segment
instead, so first audio is still as early as a *speakable* head allows.
"""

HARD_SPLIT_MAX_CHARS: int = 200
"""Length above which the buffer is broken at whitespace regardless (TR-043)."""

_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
"""Markdown link or image; only the visible label survives."""

_MARKDOWN_SYMBOLS_RE = re.compile(r"[*_#`\[\]]")
"""Markdown punctuation a synthesiser would either read out or mangle.

Replaced with a space rather than deleted: deleting welds the words on either
side together, so ``snake_case`` is spoken as one nonsense word and ``a*b*c``
becomes ``abc``. A space costs nothing -- runs of whitespace are collapsed
immediately afterwards -- and keeps the words apart.
"""

_INVISIBLE_RE = re.compile(r"[\u00ad\u200b-\u200f\u2060\u2066-\u2069\ufeff]")
"""Characters with no width and no sound: zero-width spaces and joiners, the
soft hyphen, directional marks, and the byte-order mark.

They are not whitespace to :meth:`str.split`, so a segment made only of them
survives the emptiness check and reaches the synthesiser as an utterance with
nothing in it. That is not hypothetical: ``gpt-oss-120b`` answered one live turn
with 221 characters of zero-width space (``docs/EVALS.md``). Removing them here
turns that segment into the empty string, which :meth:`SentenceChunker.feed`
already drops.
"""

_DASH_RE = re.compile(r"\s*[\u2012-\u2015]\s*")
"""A figure, en, em, or horizontal dash together with the spaces around it.

Spoken, these mark a pause, which is what a comma already does; the dash
characters themselves are not something a synthesiser can pronounce. The spaces
are swallowed with them so ``layers — the browser`` becomes ``layers, the
browser`` rather than ``layers , the browser``.
"""

_SPEECH_SUBSTITUTIONS: dict[int, str] = {
    0x2018: "'",  # left single quotation mark
    0x2019: "'",  # right single quotation mark, and the apostrophe in "don't"
    0x201A: "'",  # single low-9 quotation mark
    0x201B: "'",  # single high-reversed-9 quotation mark
    0x201C: '"',  # left double quotation mark
    0x201D: '"',  # right double quotation mark
    0x201E: '"',  # double low-9 quotation mark
    0x201F: '"',  # double high-reversed-9 quotation mark
    0x2026: "...",  # horizontal ellipsis
}
"""Typographic punctuation mapped to the ASCII a synthesiser was trained on.

The curly apostrophe is the one that matters most: models write ``don\u2019t``
far more often than ``don't``, and a grapheme-to-phoneme front end that does not
recognise it either drops the contraction or spells the word out. A live run
produced curly quotes and an em dash in the same answer.
"""


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

    This is the last thing that touches an answer before it is spoken, so it
    covers the ways written text fails out loud, not only the markdown the model
    was told not to write. In order: a markdown link keeps its label and loses
    its URL; invisible characters go, because they make a segment that looks
    non-empty and sounds like nothing; curly quotes and ellipses become the
    ASCII a grapheme-to-phoneme front end knows; a dash becomes the comma it
    means out loud; and a markdown symbol becomes a space, which separates the
    words it sat between instead of welding them into one.

    Deliberately *not* done here: numerals, units, and symbols such as ``%``,
    ``$`` and ``~`` are passed through untouched. Reading them aloud correctly
    depends on context that this function cannot see -- ``1.5`` is "one point
    five" but ``1.5 s`` is "a second and a half" -- so a wrong expansion would be
    worse than none. The prompt asks the model to write numbers as spoken words
    instead, and eval suite E4 measures whether it does.

    Args:
        raw: Buffer slice, possibly padded with whitespace or markdown.

    Returns:
        The speakable text, or an empty string when nothing speakable remains.
    """
    text = _LINK_RE.sub(r"\1", raw)
    text = _INVISIBLE_RE.sub("", text)
    text = text.translate(_SPEECH_SUBSTITUTIONS)
    text = _DASH_RE.sub(", ", text)
    text = _MARKDOWN_SYMBOLS_RE.sub(" ", text)
    return " ".join(text.split())


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
        # The head runs up to and includes the boundary, so a boundary at index
        # i yields a head of i + 1 characters; -1 (no boundary) yields 0.
        head_length = self._last_boundary + 1
        if length > EARLY_SPLIT_MIN_CHARS and head_length >= EARLY_SPLIT_MIN_HEAD_CHARS:
            # Breaking at the *last* boundary keeps the release as late as the
            # rule allows, which is still the earliest moment the limit is met.
            return self._cut(self._last_boundary + 1, self._last_boundary + 1)
        if length > HARD_SPLIT_MAX_CHARS and self._last_space >= 0:
            # A boundary may well be pending here -- one too close to the front
            # to speak on its own leaves the early rule waiting indefinitely --
            # so this is the release for prose with no *usable* punctuation.
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
