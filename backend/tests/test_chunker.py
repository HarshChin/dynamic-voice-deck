"""Tests for :mod:`app.pipeline.chunker` -- the sentence chunker (TR-040 - TR-045).

The chunker decides when the synthesiser is allowed to start, so these tests are
latency tests wearing the costume of formatting tests (TR-086). Two things are
pinned above all else.

First, the *streaming* contract. A token stream breaks at arbitrary points, so a
rule that only works when the text arrives whole is not a rule at all. Every case
below that could be sensitive to fragmentation is exercised in fragments as well,
and TC-BE-018 states the general form of that as a property.

Second, that nothing is lost. Whatever the split rules do, the words the model
produced must all reach the listener, in order and exactly once -- a chunker that
drops a clause is far worse than one that breathes in the wrong place.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import pytest
from app.pipeline.chunker import (
    EARLY_SPLIT_MIN_CHARS,
    EARLY_SPLIT_MIN_HEAD_CHARS,
    HARD_SPLIT_MAX_CHARS,
    SentenceChunker,
)
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

MARKDOWN_SYMBOLS = "*_#`"
"""Symbols TR-044 requires to be gone before a segment reaches the synthesiser."""

INVISIBLE_CHARACTERS = "\u00ad\u200b\u200c\u200d\u200e\u200f\u2060\u2066\u2069\ufeff"
"""Zero-width and formatting characters that must not reach the synthesiser."""

DASHES = "\u2012\u2013\u2014\u2015"
"""Figure, en, em, and horizontal dash: a pause in print, a comma out loud."""

SMART_PUNCTUATION = {
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u201f": '"',
    "\u2026": "...",
}
"""Typographic punctuation and the ASCII a synthesiser can pronounce."""


def speakable_reference(text: str) -> str:
    """Restate ``_clean_segment``'s character rules independently of the code.

    Written character by character, where the implementation uses regular
    expressions, so the property test checks the rule rather than the spelling
    of the rule. Whitespace is not normalised here; the callers compare with
    whitespace removed.
    """
    out: list[str] = []
    for char in text:
        if char in INVISIBLE_CHARACTERS:
            continue
        if char in DASHES:
            out.append(", ")
        elif char in SMART_PUNCTUATION:
            out.append(SMART_PUNCTUATION[char])
        elif char in MARKDOWN_SYMBOLS:
            out.append(" ")
        else:
            out.append(char)
    return "".join(out)


# 70 characters with no terminator and no clause boundary, so that a boundary
# appended at index 70 is the first one the early-split rule can ever see.
LONG_HEAD = "so " * 23 + "x"
LONG_TAIL = " and then some more"

# 400 words of five characters each: no punctuation anywhere, so only the hard
# limit can break it, and every space sits at a predictable index.
UNPUNCTUATED = "word " * 50

PROSE_ALPHABET = (
    "abcdefgXYZ 0123456789.,;:!?-'\"()/\n\t"
    + MARKDOWN_SYMBOLS
    + DASHES
    + "".join(SMART_PUNCTUATION)
    + INVISIBLE_CHARACTERS
)
"""Characters the property test draws from.

Bracket characters are deliberately absent. Markdown *link* syntax is the one
transformation that is not a per-character filter, so a segment boundary landing
inside ``[label](url)`` would strip differently from the whole text -- a real but
uninteresting edge, pinned by the worked example in TC-BE-015 instead.

Everything ``_clean_segment`` rewrites *is* here, including the dashes, the
curly quotes, and a zero-width space, so the property covers the rewriting and
not only the stripping.
"""


def chunk_whole(text: str) -> list[str]:
    """Feed ``text`` in one call and flush, returning every segment in order."""
    chunker = SentenceChunker()
    return [*chunker.feed(text), *chunker.flush()]


def chunk_fragments(fragments: Iterable[str]) -> list[str]:
    """Feed each fragment in turn and flush, returning every segment in order."""
    chunker = SentenceChunker()
    segments: list[str] = []
    for fragment in fragments:
        segments.extend(chunker.feed(fragment))
    segments.extend(chunker.flush())
    return segments


def without_whitespace(parts: Sequence[str]) -> str:
    """Join segments and drop all whitespace, leaving only their content."""
    return "".join("".join(part.split()) for part in parts)


def test_tokens_are_reassembled_into_whole_sentences() -> None:
    """TC-BE-010: tokens forming two sentences are emitted as those two sentences."""
    tokens = ["Hello", " there.", " How", " are", " you?"]

    assert chunk_fragments(tokens) == ["Hello there.", "How are you?"]
    # The same text arriving in one piece must not chunk differently (TR-040).
    assert chunk_whole("".join(tokens)) == ["Hello there.", "How are you?"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("We use e.g. Whisper. It works.", ["We use e.g. Whisper.", "It works."]),
        ("Ask Dr. Chen about it. She knows.", ["Ask Dr. Chen about it.", "She knows."]),
        ("Groq vs. Ollama here. Both run it.", ["Groq vs. Ollama here.", "Both run it."]),
        ("Mrs. Ali spoke first. Then Mr. Ito.", ["Mrs. Ali spoke first.", "Then Mr. Ito."]),
        ("It is approx. two seconds. Good.", ["It is approx. two seconds.", "Good."]),
        ("Slides, notes, i.e. the deck. Yes.", ["Slides, notes, i.e. the deck.", "Yes."]),
    ],
)
def test_a_known_abbreviation_does_not_end_a_sentence(text: str, expected: list[str]) -> None:
    """TC-BE-011: the full stop of a known abbreviation never splits a segment."""
    assert chunk_whole(text) == expected


def test_an_abbreviation_at_the_end_of_the_buffer_waits_for_more_text() -> None:
    """TC-BE-011: a trailing "e.g." is not judged until the text after it arrives."""
    chunker = SentenceChunker()

    # Nothing may be released here: at this instant "e.g." is indistinguishable
    # from a sentence that happens to end on an abbreviation-shaped word.
    assert chunker.feed("We use e.g.") == []
    assert chunker.feed(" Whisper. ") == ["We use e.g. Whisper."]
    assert chunker.flush() == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Latency is 3.5 seconds. Fine.", ["Latency is 3.5 seconds.", "Fine."]),
        ("Version 1.0.1 shipped. Good.", ["Version 1.0.1 shipped.", "Good."]),
    ],
)
def test_a_decimal_point_does_not_end_a_sentence(text: str, expected: list[str]) -> None:
    """TC-BE-012: a point inside a number is not a terminator."""
    assert chunk_whole(text) == expected


def test_a_decimal_split_across_two_tokens_still_does_not_end_a_sentence() -> None:
    """TC-BE-012: a token boundary inside "3.5" does not turn the point into a split."""
    chunker = SentenceChunker()

    assert chunker.feed("Latency is 3.") == []
    assert chunker.feed("5 seconds. ") == ["Latency is 3.5 seconds."]


@pytest.mark.parametrize(("boundary", "spoken"), [(",", ","), (";", ";"), (":", ":"), ("—", ",")])
def test_a_long_buffer_splits_at_a_clause_boundary(boundary: str, spoken: str) -> None:
    """TC-BE-013: past 60 characters, a clause boundary releases the segment early.

    The em dash splits like the others but is not *spoken* like the others: it
    reaches the listener as the comma it means (TC-BE-020).
    """
    text = LONG_HEAD + boundary + LONG_TAIL
    assert len(LONG_HEAD) == 70
    assert len(text) == 90
    chunker = SentenceChunker()

    # Released the moment the boundary arrives, without waiting for a full stop:
    # this is the split that decides first_audio_ms (TR-042, TR-086).
    assert chunker.feed(text) == [LONG_HEAD + spoken]
    assert chunker.flush() == [LONG_TAIL.strip()]


def test_the_clause_split_uses_the_last_boundary_before_the_limit() -> None:
    """TC-BE-013: with several boundaries in the buffer, the latest one is chosen."""
    text = "ab, " + "c" * 24 + ", " + "e" * 33
    assert len(text) == EARLY_SPLIT_MIN_CHARS + 3

    # The comma at index 28, not the one at index 2 -- and the head that the
    # later comma produces is the only one long enough to speak anyway.
    assert chunk_whole(text) == ["ab, " + "c" * 24 + ",", "e" * 33]


def test_a_leading_connective_is_not_spoken_on_its_own() -> None:
    """TC-BE-013a: a boundary too near the front of the buffer does not release a segment.

    Without a floor on the head this answered ``["So,", "the browser ..."]``:
    two characters synthesised as a finished utterance, then an audible gap in
    the middle of the sentence while the other eighty were still being made.
    """
    text = "So, the browser flushes the playback queue the instant it hears you speak."
    assert len(text) > EARLY_SPLIT_MIN_CHARS

    assert chunk_whole(text) == [text]


def test_the_early_split_needs_a_head_worth_speaking() -> None:
    """TC-BE-013b: the head floor is exact -- one character short and the split is skipped."""
    padding = "y" * 40

    short = "x" * (EARLY_SPLIT_MIN_HEAD_CHARS - 2) + ","
    assert len(short) == EARLY_SPLIT_MIN_HEAD_CHARS - 1
    assert chunk_whole(f"{short} {padding}") == [f"{short} {padding}"]

    exact = "x" * (EARLY_SPLIT_MIN_HEAD_CHARS - 1) + ","
    assert len(exact) == EARLY_SPLIT_MIN_HEAD_CHARS
    assert chunk_whole(f"{exact} {padding}") == [exact, padding]


def test_a_usable_boundary_still_releases_the_first_clause_early() -> None:
    """TC-BE-013c: the floor must not become a way of never splitting early.

    The early split is what buys ``first_audio_ms`` (TR-042, TR-086), so a
    boundary that clears the floor still fires before the sentence ends.
    """
    text = "Endpointing costs six hundred milliseconds, because that is how long I wait."
    chunker = SentenceChunker()

    released = chunker.feed(text)

    assert released == ["Endpointing costs six hundred milliseconds,"]
    assert len(released[0]) >= EARLY_SPLIT_MIN_HEAD_CHARS
    # Released before the full stop arrived, which is the whole point.
    assert len(released[0]) < len(text)
    assert chunker.flush() == ["because that is how long I wait."]


def test_an_unspeakable_boundary_does_not_wedge_the_chunker() -> None:
    """TC-BE-014a: a skipped boundary still leaves the hard limit free to fire."""
    text = "So, " + UNPUNCTUATED
    chunker = SentenceChunker()

    emitted = chunker.feed(text)

    assert len(emitted) == 1
    assert len(emitted[0]) <= HARD_SPLIT_MAX_CHARS
    remainder = chunker.flush()
    assert " ".join([*emitted, *remainder]).split() == ["So,", *["word"] * 50]


def test_a_short_buffer_is_never_split_at_a_boundary() -> None:
    """TC-BE-013: a comma below the limit is a pause, not a segment end."""
    text = "Yes, and also this."
    assert len(text) <= EARLY_SPLIT_MIN_CHARS

    assert chunk_whole(text) == ["Yes, and also this."]


def test_text_with_no_punctuation_splits_at_the_last_whitespace() -> None:
    """TC-BE-014: 250 unpunctuated characters break at the last space before 200."""
    assert len(UNPUNCTUATED) == 250
    chunker = SentenceChunker()

    emitted = chunker.feed(UNPUNCTUATED)

    assert len(emitted) == 1
    # Index 199 is the last space at or below the limit, so the head is 199 long.
    assert len(emitted[0]) == 199
    assert len(emitted[0]) <= HARD_SPLIT_MAX_CHARS
    remainder = chunker.flush()
    # No word was cut in half, and all fifty survived.
    assert " ".join([*emitted, *remainder]).split() == ["word"] * 50


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("**Bold** and `code`", "Bold and code"),
        ("# Heading with _emphasis_", "Heading with emphasis"),
        ("A ***strong*** point", "A strong point"),
        ("See [the docs](https://example.com) now.", "See the docs now."),
        ("Read ![diagram](img.png) closely", "Read diagram closely"),
    ],
)
def test_markdown_is_stripped_before_a_segment_is_emitted(raw: str, expected: str) -> None:
    """TC-BE-015: markdown symbols and link syntax never reach the synthesiser."""
    segments = chunk_whole(raw)

    assert segments == [expected]
    assert not any(symbol in segments[0] for symbol in MARKDOWN_SYMBOLS)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("We don\u2019t wait.", "We don't wait."),
        ("She said \u201cstop\u201d and I did.", 'She said "stop" and I did.'),
        ("Two layers\u2014the browser and the server", "Two layers, the browser and the server"),
        ("Two layers \u2014 the browser", "Two layers, the browser"),
        ("It stops\u2026 eventually", "It stops... eventually"),
        ("A \u2013 B", "A, B"),
    ],
)
def test_typographic_punctuation_is_normalised_for_speech(raw: str, expected: str) -> None:
    """TC-BE-020: curly quotes, dashes, and ellipses reach the synthesiser as ASCII.

    A live run produced curly quotes and an em dash in one answer. None of them
    is a character a grapheme-to-phoneme front end is trained on: the apostrophe
    decides whether "don't" is a contraction or two words.
    """
    assert chunk_whole(raw) == [expected]


def test_stripping_a_symbol_separates_the_words_it_sat_between() -> None:
    """TC-BE-020a: markdown removal must not weld two words into one.

    Deleting the symbol turned ``snake_case`` into one unpronounceable word;
    replacing it with a space says both.
    """
    assert chunk_whole("The snake_case name") == ["The snake case name"]
    assert chunk_whole("a*b*c") == ["a b c"]


def test_invisible_characters_never_reach_the_synthesiser() -> None:
    """TC-BE-020b: a segment of zero-width characters is dropped, not spoken.

    ``gpt-oss-120b`` ended one live turn with 221 characters of zero-width space
    (``docs/EVALS.md``). They are not whitespace to :meth:`str.split`, so the
    segment survived the emptiness check with nothing in it to say.
    """
    assert chunk_whole("\u200b" * 221) == []
    assert chunk_whole("Fine.\u200b\u200b") == ["Fine."]
    assert chunk_whole("soft\u00adhyphen") == ["softhyphen"]


def test_flush_returns_the_partial_sentence_once_and_empties_the_buffer() -> None:
    """TC-BE-016: flush releases the remainder exactly once and leaves nothing behind."""
    chunker = SentenceChunker()
    assert chunker.feed("A complete one. And a partial") == ["A complete one."]

    assert chunker.flush() == ["And a partial"]
    assert chunker.flush() == []
    assert chunker.feed("") == []


def test_flush_on_an_untouched_chunker_returns_nothing() -> None:
    """TC-BE-016: a turn that produced no text flushes to no segments."""
    assert SentenceChunker().flush() == []


def test_segments_arrive_in_order_so_the_caller_can_number_them_from_zero() -> None:
    """TC-BE-017: three segments come back in emission order, giving ids 0, 1, 2."""
    chunker = SentenceChunker()

    segments = [*chunker.feed("One thing. Two things. Three things."), *chunker.flush()]

    # The chunker returns no ids of its own; position *is* the id (TR-045).
    assert list(enumerate(segments)) == [
        (0, "One thing."),
        (1, "Two things."),
        (2, "Three things."),
    ]


@pytest.mark.parametrize(
    "text",
    ["", "   ", "\n\n\t", "**", "`` __ ##", ". ", "  .  ! ? ", "*" * 300, "   " * 90],
)
def test_an_empty_or_whitespace_only_segment_is_never_emitted(text: str) -> None:
    """TC-BE-019: no segment is empty or whitespace-only, whatever the input."""
    segments = chunk_whole(text)

    assert all(segment and segment == segment.strip() for segment in segments)


@st.composite
def _text_and_fragments(draw: st.DrawFn) -> tuple[str, list[str]]:
    """Draw a text together with one arbitrary way of cutting it into fragments."""
    text = draw(st.text(alphabet=PROSE_ALPHABET, max_size=400))
    cuts = sorted(draw(st.lists(st.integers(min_value=0, max_value=len(text)), max_size=12)))
    starts = [0, *cuts]
    ends = [*cuts, len(text)]
    return text, [text[start:end] for start, end in zip(starts, ends, strict=True)]


@given(_text_and_fragments())
@hypothesis_settings(max_examples=400, deadline=None)
def test_chunking_does_not_depend_on_how_the_stream_was_fragmented(
    case: tuple[str, list[str]],
) -> None:
    """TC-BE-018: any fragmentation of a text yields the same segments as feeding it whole."""
    text, fragments = case
    assert "".join(fragments) == text

    # The property that actually matters: the model chooses the words, the
    # network chooses the token boundaries, and only the first may affect audio.
    assert chunk_fragments(fragments) == chunk_whole(text)


@given(st.text(alphabet=PROSE_ALPHABET, max_size=400))
@hypothesis_settings(max_examples=400, deadline=None)
def test_segments_preserve_every_non_whitespace_character_in_order(text: str) -> None:
    """TC-BE-018: concatenated segments equal the speech-normalised input, whitespace aside."""
    segments = chunk_whole(text)

    # Whitespace is normalised and consumed at the splits, so it is removed from
    # both sides; everything else must survive, unduplicated and in order. This
    # is stronger than "the words are preserved": a clause split at "a,b" leaves
    # two segments where there was one word, and no character is lost either way.
    assert without_whitespace(segments) == without_whitespace([speakable_reference(text)])
    assert all(segment and segment == segment.strip() for segment in segments)
