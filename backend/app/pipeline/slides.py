"""Slide state and navigation decisions (TRD §4.7, PRD §F5).

The :class:`SlideController` is the only thing allowed to move the deck. It
exists because the two ways a slide can change are both untrustworthy:

* The model calls ``go_to_slide`` with a slide number it invented. A hallucinated
  index must be rejected rather than forwarded, or the audience sees a different
  slide from the one the agent is describing and the session never recovers
  (TR-061). Rejection is deliberately *soft*: the caller gets a sentence to hand
  back as the tool result, and the model corrects itself on the next step.
* The model answers about a different slide without calling the tool at all. The
  keyword fallback recovers the intent from the answer text (TR-062), which is
  why every slide in a deck carries aliases.

Both paths return the same :class:`SlideAction`, tagged with its
:class:`~app.protocol.ToolSource`, so the UI can show which one fired.

The fallback has a failure mode of its own, and the scoring below is shaped
around it. The same word is worth three points to the slide that owns it as an
alias and one to the slide that merely prints it in a bullet, so an agent
describing the slide on screen, in that slide's own words, can hand another
slide a winning score and be dragged away mid-explanation. Evidence the slide on
screen already shows is therefore not counted for anybody else; see
:meth:`SlideController.keyword_fallback`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, NamedTuple

from pydantic import BaseModel, Field

from ..decks.models import Deck, Slide
from ..logging_setup import get_logger
from ..protocol import SessionMode, ToolSource
from .tools import GO_TO_SLIDE, HIGHLIGHT_BULLET

logger = get_logger(__name__)

ALIAS_WEIGHT: Final[int] = 3
"""Score awarded for one of a slide's alias phrases appearing in the answer."""

TITLE_WEIGHT: Final[int] = 2
"""Score awarded for one word of a slide's title appearing in the answer."""

BULLET_WEIGHT: Final[int] = 1
"""Score awarded for one word of a slide's bullets appearing in the answer."""

MIN_FALLBACK_SCORE: Final[int] = 4
"""Lowest score that may move the deck without a tool call (TR-062).

Four is one alias hit plus a title word, or two title words: enough evidence
that the answer is *about* another slide rather than merely mentioning it.
"""

MIN_FALLBACK_MARGIN: Final[int] = 2
"""How far the best slide must beat the runner-up before the deck moves.

Without a margin the fallback would flip the deck on a coin toss whenever an
answer compares two slides, which is worse than not moving at all.
"""

MIN_TOKEN_CHARS: Final[int] = 2
"""Shortest token that may contribute a title or bullet word match."""

MAX_REASON_CHARS: Final[int] = 120
"""Longest reason kept from a tool call; the event log shows it verbatim."""

_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")
"""Words are runs of lower-case letters and digits; everything else separates."""

STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "about",
        "after",
        "again",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "back",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "done",
        "down",
        "during",
        "each",
        "even",
        "ever",
        "every",
        "few",
        "for",
        "from",
        "further",
        "get",
        "gets",
        "go",
        "goes",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "herself",
        "him",
        "himself",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "let",
        "like",
        "me",
        "more",
        "most",
        "much",
        "must",
        "my",
        "myself",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "one",
        "only",
        "or",
        "other",
        "our",
        "ours",
        "ourselves",
        "out",
        "over",
        "own",
        "re",
        "really",
        "said",
        "same",
        "say",
        "says",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "us",
        "use",
        "used",
        "uses",
        "very",
        "was",
        "we",
        "well",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
    }
)
"""Function words that carry no routing signal.

Only closed-class English words are listed. Nothing here can be a topic, so
dropping them cannot cost a slide a legitimate match -- and keeping them would
let a long, waffly answer clear the threshold on filler alone.
"""


class SlideAction(BaseModel):
    """A validated instruction to move or annotate the deck.

    Maps one-to-one onto a ``slide.goto`` message.

    Attributes:
        index: Target slide, 1-based. For a highlight this is the slide the
            deck is already on.
        highlight: Zero-based bullet to emphasise, or ``None`` for none.
        reason: Short clause shown in the event log.
        source: Whether the model asked for this or the keyword fallback
            inferred it.
    """

    index: int = Field(ge=1)
    highlight: int | None = Field(default=None, ge=0)
    reason: str
    source: ToolSource = ToolSource.LLM


class _Score(NamedTuple):
    """One slide's fallback score and the evidence behind it.

    Attributes:
        score: Weighted total of alias, title, and bullet matches.
        slide_index: The slide scored, 1-based. Not ``index``, which would
            shadow ``tuple.index``.
        evidence: The strongest matching term, for the reason string.
    """

    score: int
    slide_index: int
    evidence: str


@dataclass(frozen=True)
class _SlideTerms:
    """A slide's routing vocabulary, tokenised once at construction.

    Attributes:
        index: The slide's 1-based index.
        aliases: Each alias paired with its tokens, for phrase matching.
        title_words: Content words of the title.
        bullet_words: Content words of the bullets, minus any already counted
            as title words so a repeated word scores once at its higher weight.
        shown_words: Every content word this slide puts on screen, title and
            bullets together.
        shown_lines: The title and each bullet as its own token sequence, kept
            in order so another slide's alias can be tested for occurrence in
            this slide's on-screen text. One sequence per line, because a phrase
            spanning the end of the title and the start of a bullet is not a
            phrase this slide shows.
    """

    index: int
    aliases: tuple[tuple[str, tuple[str, ...]], ...]
    title_words: frozenset[str]
    bullet_words: frozenset[str]
    shown_words: frozenset[str]
    shown_lines: tuple[tuple[str, ...], ...]

    @classmethod
    def build(cls, slide: Slide) -> _SlideTerms:
        """Tokenise one slide's title, bullets, and aliases.

        Args:
            slide: The slide to index.

        Returns:
            The slide's routing vocabulary.
        """
        aliases: list[tuple[str, tuple[str, ...]]] = []
        for alias in slide.aliases:
            alias_tokens = tuple(_tokenize(alias))
            if alias_tokens:  # An alias of pure punctuation can never match.
                aliases.append((alias, alias_tokens))

        title_tokens = _tokenize(slide.title)
        bullet_lines = [_tokenize(bullet) for bullet in slide.bullets]
        title_words = _content_words(title_tokens)
        bullet_words = (
            _content_words([token for line in bullet_lines for token in line]) - title_words
        )
        return cls(
            index=slide.index,
            aliases=tuple(aliases),
            title_words=title_words,
            bullet_words=bullet_words,
            shown_words=title_words | bullet_words,
            shown_lines=tuple(tuple(line) for line in [title_tokens, *bullet_lines]),
        )

    def shows(self, phrase: tuple[str, ...]) -> bool:
        """Report whether this slide's own title or bullets contain a phrase.

        Args:
            phrase: Another slide's alias, tokenised.

        Returns:
            ``True`` when the phrase appears in this slide's on-screen text.
        """
        return any(_contains_phrase(line, phrase) for line in self.shown_lines)


def _tokenize(text: str) -> list[str]:
    """Split text into lower-case word tokens.

    Args:
        text: Any human text.

    Returns:
        The tokens, in order, with punctuation discarded.
    """
    return _TOKEN_RE.findall(text.lower())


def _content_words(tokens: list[str]) -> frozenset[str]:
    """Reduce tokens to the distinct words worth scoring.

    Args:
        tokens: Tokens from :func:`_tokenize`.

    Returns:
        The tokens with stopwords and one-character fragments removed.
    """
    return frozenset(
        token for token in tokens if len(token) >= MIN_TOKEN_CHARS and token not in STOPWORDS
    )


def _contains_phrase(haystack: Sequence[str], needle: tuple[str, ...]) -> bool:
    """Report whether a token sequence appears contiguously in another.

    Aliases are matched as phrases rather than as loose bags of words: an answer
    mentioning "first" and "token" in unrelated clauses does not mean the alias
    "time to first token", and treating it as one would move the deck for no
    reason.

    Args:
        haystack: The answer's tokens, in order.
        needle: The alias's tokens, in order.

    Returns:
        ``True`` if ``needle`` occurs as a contiguous run inside ``haystack``.
    """
    span = len(needle)
    if span == 0 or span > len(haystack):
        return False
    return any(
        tuple(haystack[start : start + span]) == needle
        for start, token in enumerate(haystack)
        if token == needle[0]
    )


def _coerce_index(value: Any) -> int | None:
    """Read a tool argument as an integer index.

    Tool arguments are JSON the model wrote, so the declared schema is a request
    rather than a guarantee: a model asked for an integer routinely sends
    ``"4"``. Accepting a digit string here saves a whole self-correction
    round-trip, which the user would hear as dead air.

    Args:
        value: The raw argument value.

    Returns:
        The integer, or ``None`` if the value is not one. ``bool`` is rejected
        even though it subclasses ``int``, because ``True`` is not slide one.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


class SlideController:
    """Owns the deck's position and guards every attempt to change it.

    The controller is per-session state driven by ``Session``: the turn pipeline
    calls :meth:`apply_tool` for each tool call the model makes, then
    :meth:`keyword_fallback` once on the finished answer if nothing navigated.
    Both mutate :attr:`current_slide` on success, so the controller is always the
    single source of truth for where the deck is.

    Args:
        deck: The deck being presented.
        current_slide: Slide to start on, 1-based. Clamped into the deck.
        mode: Whether the session answers questions or walks the deck.

    Attributes:
        deck: The deck being presented.
        current_slide: The slide on screen, 1-based (TR-060).
        presentation_cursor: How far an auto-presentation has walked. Moves only
            through :meth:`advance_cursor`, so a question that jumps the deck
            does not lose the presenter's place (TR-063, TR-064).
        mode: The session mode.
        last_error: Why the most recent :meth:`apply_tool` call was rejected, or
            ``None`` if it was accepted. The caller sends this back to the model
            as the ``tool`` result so it can correct itself (TR-061).
    """

    def __init__(
        self,
        deck: Deck,
        current_slide: int = 1,
        mode: SessionMode = SessionMode.QA,
    ) -> None:
        self.deck = deck
        self.current_slide = self._clamp(current_slide)
        self.presentation_cursor = self.current_slide
        self.mode = mode
        self.last_error: str | None = None
        self._terms: tuple[_SlideTerms, ...] = tuple(
            _SlideTerms.build(slide) for slide in deck.slides
        )

    def apply_tool(self, name: str, arguments: dict[str, Any]) -> SlideAction | None:
        """Validate one tool call and apply it if it is sound (TR-061).

        Never raises. An unknown tool, a missing or non-integer argument, a slide
        outside the deck, or a bullet outside the current slide all leave the
        deck untouched and set :attr:`last_error` to a sentence written for the
        model to read.

        Args:
            name: Tool the model called.
            arguments: Decoded JSON arguments, exactly as the model sent them.

        Returns:
            The action to broadcast, or ``None`` if the call was rejected.
        """
        self.last_error = None
        if name == GO_TO_SLIDE:
            return self._apply_go_to_slide(arguments)
        if name == HIGHLIGHT_BULLET:
            return self._apply_highlight_bullet(arguments)
        self._reject(
            f"Unknown tool {name!r}. The only tools available are "
            f"{GO_TO_SLIDE} and {HIGHLIGHT_BULLET}.",
            tool=name,
        )
        return None

    def keyword_fallback(self, answer_text: str) -> SlideAction | None:
        """Infer a slide from an answer that navigated nothing (TR-062).

        Every slide is scored by weighted term hits and the winner moves the deck
        only if it is convincing on all three counts: at least
        :data:`MIN_FALLBACK_SCORE`, ahead of the runner-up by at least
        :data:`MIN_FALLBACK_MARGIN`, and not the slide already on screen. The
        current slide stays in the ranking rather than being excluded, so an
        answer that is equally about here and there keeps the deck still.

        Words the slide on screen already shows are struck out of every *other*
        slide's evidence first. Without that, a word can be worth one point to
        the slide being described, as a bullet word, and three to a rival that
        happens to own it as an alias: an answer about the latency budget that
        says "endpointing" and "silence" -- both printed on the latency slide's
        own bullet -- scored 10 for the hearing slide against 8 for the slide the
        audience was looking at, and the deck jumped mid-explanation. An answer
        made of the current slide's own words is evidence that the agent is on
        topic, so it may not be read as evidence of somewhere else.

        Args:
            answer_text: The assistant's full answer for the turn.

        Returns:
            The action to broadcast, or ``None`` when the evidence is thin,
            split, or points at the current slide.
        """
        tokens = _tokenize(answer_text)
        if not tokens:
            return None

        content = _content_words(tokens)
        # Deck validation guarantees indices are 1..n in order (TR-150), so the
        # slide on screen sits at this position.
        on_screen = self._terms[self.current_slide - 1]
        ranked = sorted(
            (
                self._score_slide(
                    terms,
                    tokens,
                    content,
                    # The slide on screen keeps its full score: it is the
                    # incumbent, and discounting it would only make it easier
                    # for a rival to clear the margin.
                    on_screen=None if terms.index == self.current_slide else on_screen,
                )
                for terms in self._terms
            ),
            key=lambda scored: (-scored.score, scored.slide_index),
        )
        best = ranked[0]
        runner_up_score = ranked[1].score if len(ranked) > 1 else 0

        if best.score < MIN_FALLBACK_SCORE:
            return None
        if best.score - runner_up_score < MIN_FALLBACK_MARGIN:
            logger.debug(
                "slides.fallback_ambiguous",
                best=best.slide_index,
                score=best.score,
                runner_up=runner_up_score,
            )
            return None
        if best.slide_index == self.current_slide:
            return None

        self.current_slide = best.slide_index
        action = SlideAction(
            index=best.slide_index,
            reason=f"Keyword match: {best.evidence}",
            source=ToolSource.FALLBACK,
        )
        logger.info(
            "slides.fallback_matched",
            index=best.slide_index,
            score=best.score,
            runner_up=runner_up_score,
            evidence=best.evidence,
        )
        return action

    def on_user_navigation(self, index: int) -> str:
        """Follow the deck after the user moved it themselves (TR-063).

        The cursor is untouched: a manual detour during an auto-presentation must
        not cost the presenter its place.

        Args:
            index: Slide the user moved to, 1-based. Clamped into the deck to
                mirror the frontend's own clamping (TR-133).

        Returns:
            A system note naming the slide and its title, for the history.
        """
        target = self._clamp(index)
        self.current_slide = target
        title = self.deck.slide(target).title
        logger.info("slides.user_navigation", index=target, requested=index)
        return f"[User manually moved to slide {target}: {title}]"

    def advance_cursor(self) -> bool:
        """Step the auto-presentation on by one slide (TR-064).

        Only meaningful in ``present`` mode; in ``qa`` mode there is no
        presentation to advance and the cursor stays put.

        Returns:
            ``True`` if the cursor moved, ``False`` at the end of the deck or
            outside ``present`` mode.
        """
        if self.mode is not SessionMode.PRESENT:
            return False
        if self.presentation_cursor >= self.deck.last_index:
            logger.info("slides.cursor_at_end", cursor=self.presentation_cursor)
            return False
        self.presentation_cursor += 1
        return True

    def snapshot(self) -> dict[str, int | str]:
        """Return the position facts the system prompt needs (TR-064, TR-070).

        The current slide's TITLE is included, not just its number. "What's this
        slide about?" was answered with "This is the intro slide." while the deck
        was on slide 4, with a correct position block and slide 4's notes in the
        prompt -- the model simply failed to connect the bare number to the deck
        entry. Naming it removes that inference.

        Returns:
            The current slide and its title, the presentation cursor and its
            title, the mode, and the number of slides in the deck.
        """
        return {
            "current_slide": self.current_slide,
            "current_slide_title": self.deck.slide(self.current_slide).title,
            "presentation_cursor": self.presentation_cursor,
            "presentation_cursor_title": self.deck.slide(self.presentation_cursor).title,
            "mode": self.mode.value,
            "slide_count": self.deck.last_index,
        }

    # -- internals ---------------------------------------------------------- #

    def _clamp(self, index: int) -> int:
        """Pull a slide index inside the deck.

        Args:
            index: A possibly out-of-range 1-based index.

        Returns:
            The index, bounded by the first and last slide.
        """
        return max(1, min(index, self.deck.last_index))

    def _reject(self, message: str, **fields: Any) -> None:
        """Record a rejected tool call and log it.

        Args:
            message: Explanation written for the model to read.
            **fields: Extra structured log fields.
        """
        self.last_error = message
        logger.warning("slides.tool_rejected", reason=message, **fields)

    def _apply_go_to_slide(self, arguments: dict[str, Any]) -> SlideAction | None:
        """Validate and apply a ``go_to_slide`` call.

        Args:
            arguments: The call's decoded arguments.

        Returns:
            The action, or ``None`` if the call was rejected.
        """
        raw = arguments.get("slide_index")
        index = _coerce_index(raw)
        if index is None:
            self._reject(
                f"{GO_TO_SLIDE} needs an integer slide_index; got {raw!r}.",
                tool=GO_TO_SLIDE,
            )
            return None

        last = self.deck.last_index
        if not 1 <= index <= last:
            self._reject(
                f"Slide {index} does not exist. This deck has slides 1 to {last}; "
                f"call {GO_TO_SLIDE} again with a slide in that range.",
                tool=GO_TO_SLIDE,
                requested=index,
            )
            return None

        reason = str(arguments.get("reason") or "").strip()[:MAX_REASON_CHARS]
        action = SlideAction(
            index=index,
            reason=reason or f"Model navigated to slide {index}",
            source=ToolSource.LLM,
        )
        self.current_slide = index
        logger.info("slides.tool_applied", tool=GO_TO_SLIDE, index=index, reason=action.reason)
        return action

    def _apply_highlight_bullet(self, arguments: dict[str, Any]) -> SlideAction | None:
        """Validate and apply a ``highlight_bullet`` call.

        The bullet is checked against the slide currently on screen, not against
        the deck's widest slide, because the model is told to highlight only
        where the audience is looking.

        Args:
            arguments: The call's decoded arguments.

        Returns:
            The action, or ``None`` if the call was rejected.
        """
        raw = arguments.get("bullet_index")
        bullet = _coerce_index(raw)
        if bullet is None:
            self._reject(
                f"{HIGHLIGHT_BULLET} needs an integer bullet_index; got {raw!r}.",
                tool=HIGHLIGHT_BULLET,
            )
            return None

        bullet_count = len(self.deck.slide(self.current_slide).bullets)
        if not 0 <= bullet < bullet_count:
            self._reject(
                f"Bullet {bullet} does not exist on slide {self.current_slide}, which has "
                f"bullets 0 to {bullet_count - 1}.",
                tool=HIGHLIGHT_BULLET,
                requested=bullet,
                slide=self.current_slide,
            )
            return None

        logger.info(
            "slides.tool_applied",
            tool=HIGHLIGHT_BULLET,
            index=self.current_slide,
            bullet=bullet,
        )
        return SlideAction(
            index=self.current_slide,
            highlight=bullet,
            reason=f"Emphasising bullet {bullet}",
            source=ToolSource.LLM,
        )

    def _score_slide(
        self,
        terms: _SlideTerms,
        tokens: list[str],
        content: frozenset[str],
        on_screen: _SlideTerms | None = None,
    ) -> _Score:
        """Score one slide against a tokenised answer.

        Each distinct term counts once. Repetition is not evidence -- an answer
        that says "latency" six times is no more about the latency slide than one
        that says it twice -- and letting it accumulate would push any wordy
        answer past the threshold.

        Args:
            terms: The slide's pre-tokenised vocabulary.
            tokens: The answer's tokens, in order, for alias phrase matching.
            content: The answer's distinct content words.
            on_screen: Vocabulary of the slide the audience is looking at, when
                scoring some *other* slide. Terms it already shows are struck
                out, so an on-topic answer cannot be read as evidence of
                elsewhere. ``None`` when scoring the slide on screen itself.

        Returns:
            The slide's score and its strongest matching term.
        """
        matched_aliases = [
            alias
            for alias, alias_tokens in terms.aliases
            if _contains_phrase(tokens, alias_tokens)
            and (on_screen is None or not on_screen.shows(alias_tokens))
        ]
        shown: frozenset[str] = on_screen.shown_words if on_screen is not None else frozenset()
        matched_title = (terms.title_words & content) - shown
        matched_bullets = (terms.bullet_words & content) - shown
        score = (
            ALIAS_WEIGHT * len(matched_aliases)
            + TITLE_WEIGHT * len(matched_title)
            + BULLET_WEIGHT * len(matched_bullets)
        )

        if matched_aliases:
            evidence = max(matched_aliases, key=len)
        elif matched_title:
            evidence = " ".join(sorted(matched_title))
        elif matched_bullets:
            evidence = " ".join(sorted(matched_bullets))
        else:
            evidence = ""
        return _Score(score=score, slide_index=terms.index, evidence=evidence)
