"""The six eval suites (TRD §13.1).

Each suite is a coroutine taking a :class:`Context` and returning a :class:`SuiteResult`: a headline
metric, the thresholds it is judged against, and one record per item so a failure can be read rather
than guessed at. Nothing here asserts; a suite reports, and the runner decides whether the numbers
clear the release bar.
"""

from __future__ import annotations

import asyncio
import re
import statistics
from dataclasses import dataclass, field
from typing import Any

from app.decks.models import Deck
from app.pipeline.history import ConversationHistory
from app.pipeline.prompt import PromptBuilder
from app.providers.base import LLMProvider, TTSProvider

from .budget import NOT_ATTEMPTED
from .harness import TurnTrace, read_dataset, run_one
from .judge import judge, rubric

MAX_CONCURRENCY: int = 2
"""Items in flight at once. Module state so ``--concurrency`` can lower it.

Two rather than one because a suite of forty items is otherwise several minutes of waiting, and not
more than two because the free tier's per-minute ceiling is the binding constraint: a wider fan-out
simply converts into rate-limit errors.
"""

STYLE_MAX_SENTENCES = 5
"""Longest an answer may be before it stops being speech and starts being a document (E4)."""

STYLE_MAX_WORDS = 90
"""Longest an answer may be in words (E4)."""

MARKUP = re.compile(
    r"[*_#`|]|^\s*[-•]\s|\[[^\]]*\]\([^)]*\)|https?://|[\U0001f300-\U0001faff]", re.MULTILINE
)
"""Anything that reads as written rather than spoken: markdown, bullets, links, emoji."""

FULLY_GROUNDED = 2
"""The judge's top score: every claim supported by the notes."""

CALIBRATION_THRESHOLD = 0.9
"""Agreement with the hand-labelled set below which a run's judged suites are not believed."""


@dataclass(slots=True)
class Context:
    """Everything a suite needs to run.

    Attributes:
        deck: The deck under evaluation.
        llm: The model being evaluated.
        judge_llm: The model doing the grading, at temperature zero.
        tts: Synthesiser; faked except for the latency suite.
        prompts: The shipped prompt builder.
    """

    deck: Deck
    llm: LLMProvider
    judge_llm: LLMProvider
    tts: TTSProvider
    prompts: PromptBuilder


@dataclass(slots=True)
class SuiteResult:
    """What one suite measured.

    Attributes:
        suite: Its identifier, ``E1`` to ``E6``.
        title: A human name for the summary table.
        metrics: Headline numbers, named as they appear in the thresholds.
        thresholds: The release bar, as ``name -> (comparison, value)``.
        items: One record per evaluated item.
        note: Anything that qualifies the numbers.
    """

    suite: str
    title: str
    metrics: dict[str, float] = field(default_factory=dict)
    thresholds: dict[str, tuple[str, float]] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""

    @property
    def passed(self) -> bool:
        """Whether every threshold is met."""
        return all(self.meets(name) for name in self.thresholds)

    def meets(self, name: str) -> bool:
        """Whether one metric clears its threshold.

        Args:
            name: The metric to check.

        Returns:
            ``True`` when it clears, or when there is no threshold for it.
        """
        if name not in self.thresholds:
            return True
        comparison, value = self.thresholds[name]
        actual = self.metrics.get(name)
        if actual is None:
            return False
        return actual >= value if comparison == ">=" else actual <= value


async def _map(items: list[Any], work: Any) -> list[Any]:
    """Run a coroutine over items with a bounded fan-out.

    Args:
        items: The items to process.
        work: A coroutine function taking one item.

    Returns:
        The results, in the order of ``items``.
    """
    limiter = asyncio.Semaphore(MAX_CONCURRENCY)

    async def bounded(item: Any) -> Any:
        async with limiter:
            return await work(item)

    return list(await asyncio.gather(*(bounded(item) for item in items)))


def _exclusions(items: list[dict[str, Any]]) -> str:
    """Describe the items a suite could not count, for its note.

    Args:
        items: Every record the suite produced.

    Returns:
        A clause ending in a full stop: just the stop when every item answered, otherwise how
        many were excluded and how many of those were never attempted because the day's budget
        had run out (TR-205). A reader has to be able to tell a spent budget from a flaky provider.
    """
    failed = [row for row in items if row.get("error") is not None]
    if not failed:
        return "."
    skipped = sum(1 for row in failed if row["error"] == NOT_ATTEMPTED)
    text = f"; {len(failed)} excluded after a provider failure"
    if skipped:
        text += f", {skipped} of them not attempted once the daily budget had run out"
    return text + "."


def _style_failures(answer: str, sentences: list[str]) -> list[str]:
    """List the ways an answer is unspeakable (E4).

    Args:
        answer: The whole answer.
        sentences: How it was split for speech.

    Returns:
        One short reason per failure; empty when the answer is fine.
    """
    problems: list[str] = []
    if len(sentences) > STYLE_MAX_SENTENCES:
        problems.append(f"{len(sentences)} sentences")
    words = len(answer.split())
    if words > STYLE_MAX_WORDS:
        problems.append(f"{words} words")
    if MARKUP.search(answer):
        problems.append("markup, a link or an emoji")
    return problems


async def e1_routing(context: Context) -> SuiteResult:
    """E1: does the agent land on the right slide, and stay put when it should?

    Args:
        context: Providers and deck.

    Returns:
        Accuracy over every item, and the false-navigation rate over the items that should not
        have moved the deck at all.
    """
    dataset = read_dataset("routing.jsonl")

    async def one(item: dict[str, Any]) -> dict[str, Any]:
        trace = await run_one(
            utterance=item["utterance"],
            deck=context.deck,
            llm=context.llm,
            tts=context.tts,
            prompts=context.prompts,
            current_slide=item["current_slide"],
        )
        expected = item["expected_slide"]
        landed = trace.final_slide
        correct = landed == (expected if expected is not None else item["current_slide"])
        return {
            "id": item["id"],
            "category": item["category"],
            "utterance": item["utterance"],
            "current_slide": item["current_slide"],
            "expected_slide": expected,
            "landed_on": landed,
            "correct": correct,
            "moved": trace.navigated,
            "sources": [action.source for action in trace.actions],
            "answer": trace.answer,
            "sentences": trace.sentences,
            "error": trace.error,
        }

    items = await _map(dataset, one)
    # An item the provider refused says nothing about routing, so it is excluded rather than
    # counted wrong: otherwise a run during a rate limit measures the free tier, not the agent.
    answered = [row for row in items if row["error"] is None]
    stay = [row for row in answered if row["expected_slide"] is None]
    return SuiteResult(
        suite="E1",
        title="Slide routing",
        metrics={
            "accuracy": _ratio(sum(1 for row in answered if row["correct"]), len(answered)),
            "false_navigation": _ratio(sum(1 for row in stay if row["moved"]), len(stay)),
        },
        thresholds={"accuracy": (">=", 0.90), "false_navigation": ("<=", 0.05)},
        items=items,
        note=f"{len(answered)} of {len(items)} items answered" + _exclusions(items),
    )


async def e2_interruption(context: Context) -> SuiteResult:
    """E2: after being cut off, does the agent remember only what was heard?

    Args:
        context: Providers and deck.

    Returns:
        Repetition and phantom-reference rates as judged against the rubric.
    """
    dataset = read_dataset("interruption.jsonl")
    instructions = rubric("interruption")

    async def one(item: dict[str, Any]) -> dict[str, Any]:
        # Built the way a real interruption builds it: every sentence is recorded as the model
        # generates it, and the cut then rewrites the turn to what was actually heard. Assembling
        # the truncated history directly would test a fixture rather than `truncate_current`.
        history = ConversationHistory()
        history.add_user(item["question"])
        history.begin_assistant_turn(1)
        for sentence in [*item["heard"], *item["unheard"]]:
            history.record_sentence(sentence)
        history.truncate_current(1, len(item["heard"]) - 1)
        trace = await run_one(
            utterance=item["follow_up"],
            deck=context.deck,
            llm=context.llm,
            tts=context.tts,
            prompts=context.prompts,
            current_slide=item["current_slide"],
            history=history,
        )
        heard = "\n- ".join(item["heard"])
        unheard = "\n- ".join(item["unheard"])
        case = (
            f"HEARD:\n- {heard}\n\n"
            f"NEVER SPOKEN:\n- {unheard}\n\n"
            f"THE LISTENER THEN SAID: {item['follow_up']}\n\n"
            f"THE AGENT REPLIED: {trace.answer}"
        )
        verdict = await judge(context.judge_llm, instructions, case)
        return {
            "id": item["id"],
            "follow_up": item["follow_up"],
            "answer": trace.answer,
            "sentences": trace.sentences,
            "repeats": bool(verdict.payload.get("repeats")),
            "phantom": bool(verdict.payload.get("phantom")),
            "rationale": verdict.payload.get("rationale", ""),
            "judge_error": verdict.error,
            "error": trace.error,
        }

    items = await _map(dataset, one)
    scored = [row for row in items if row["judge_error"] is None and row["error"] is None]
    return SuiteResult(
        suite="E2",
        title="Interruption memory",
        metrics={
            "repetition": _ratio(sum(1 for row in scored if row["repeats"]), len(scored)),
            "phantom_reference": _ratio(sum(1 for row in scored if row["phantom"]), len(scored)),
        },
        thresholds={"repetition": ("<=", 0.10), "phantom_reference": ("<=", 0.0)},
        items=items,
        note=f"{len(scored)} of {len(items)} items were gradeable" + _exclusions(items),
    )


async def e3_grounded(context: Context) -> SuiteResult:
    """E3: are answers faithful to the notes, and are unanswerable questions declined?

    Args:
        context: Providers and deck.

    Returns:
        Mean faithfulness over answerable items and the decline rate over the rest.
    """
    dataset = read_dataset("grounded.jsonl")
    instructions = rubric("grounded")

    async def one(item: dict[str, Any]) -> dict[str, Any]:
        trace = await run_one(
            utterance=item["question"],
            deck=context.deck,
            llm=context.llm,
            tts=context.tts,
            prompts=context.prompts,
            current_slide=item["slide"],
        )
        notes = context.deck.slide(item["slide"]).notes
        case = (
            f"SLIDE NOTES:\n{notes}\n\n"
            f"QUESTION: {item['question']}\n\n"
            f"ANSWER: {trace.answer}\n\n"
            f"The notes {'do' if item['answerable'] else 'do not'} contain the answer."
        )
        verdict = await judge(context.judge_llm, instructions, case)
        score = verdict.payload.get("score")
        return {
            "id": item["id"],
            "slide": item["slide"],
            "question": item["question"],
            "answerable": item["answerable"],
            "answer": trace.answer,
            "sentences": trace.sentences,
            "score": score if isinstance(score, int) else None,
            "rationale": verdict.payload.get("rationale", ""),
            "judge_error": verdict.error,
            "error": trace.error,
        }

    items = await _map(dataset, one)
    answerable = [row for row in items if row["answerable"] and row["score"] is not None]
    unanswerable = [row for row in items if not row["answerable"] and row["score"] is not None]
    return SuiteResult(
        suite="E3",
        title="Groundedness",
        metrics={
            "mean_score": (
                statistics.fmean(row["score"] for row in answerable) if answerable else 0.0
            ),
            "decline_rate": _ratio(
                sum(1 for row in unanswerable if row["score"] == FULLY_GROUNDED), len(unanswerable)
            ),
        },
        thresholds={"mean_score": (">=", 1.7), "decline_rate": (">=", 0.80)},
        items=items,
        note=(
            f"{len(answerable)} answerable and {len(unanswerable)} unanswerable items graded"
            + _exclusions(items)
        ),
    )


def e4_style(results: list[SuiteResult]) -> SuiteResult:
    """E4: is the output speakable? Derived from whatever E1 to E3 already produced.

    Args:
        results: The suites run so far.

    Returns:
        The share of answers that read as speech rather than as a document.
    """
    items: list[dict[str, Any]] = []
    for result in results:
        for row in result.items:
            answer = row.get("answer") or ""
            if not answer:
                continue
            problems = _style_failures(answer, row.get("sentences") or [])
            items.append(
                {
                    "id": f"{result.suite}:{row['id']}",
                    "answer": answer,
                    "problems": problems,
                    "ok": not problems,
                }
            )
    return SuiteResult(
        suite="E4",
        title="Spoken style",
        metrics={"pass_rate": _ratio(sum(1 for row in items if row["ok"]), len(items))},
        thresholds={"pass_rate": (">=", 0.95)},
        items=items,
        note=f"Derived from {len(items)} answers across the suites that ran.",
    )


def e6_tools(routing: SuiteResult) -> SuiteResult:
    """E6: are tool calls valid, and are they kept away from off-topic input?

    Args:
        routing: The E1 result, whose traces carry every tool call made.

    Returns:
        The count of invalid calls and the rate of navigation on off-topic questions.
    """
    off_topic = [
        row for row in routing.items if row["category"] == "off-topic" and row["error"] is None
    ]
    invalid = [row for row in routing.items if row["error"] and "tool" in row["error"].lower()]
    items = [
        {
            "id": row["id"],
            "category": row["category"],
            "sources": row["sources"],
            "moved": row["moved"],
        }
        for row in routing.items
    ]
    return SuiteResult(
        suite="E6",
        title="Tool-call hygiene",
        metrics={
            "invalid_calls": float(len(invalid)),
            "off_topic_navigation": _ratio(
                sum(1 for row in off_topic if row["moved"]), len(off_topic)
            ),
        },
        thresholds={"invalid_calls": ("<=", 0.0), "off_topic_navigation": ("<=", 0.05)},
        items=items,
        note="Derived from the E1 traces; a rejected tool call surfaces as a turn error.",
    )


async def e5_latency(context: Context, runs: int = 3) -> SuiteResult:
    """E5: are the stage latencies within the budget, with real synthesis?

    Args:
        context: Providers and deck. Its ``tts`` must be the real one.
        runs: How many questions to time.

    Returns:
        p50 and p95 of each stage, in milliseconds.
    """
    questions = [
        "What is this deck about?",
        "How does barge-in work?",
        "Tell me about the latency budget.",
    ][:runs]
    traces: list[TurnTrace] = []
    for question in questions:
        traces.append(
            await run_one(
                utterance=question,
                deck=context.deck,
                llm=context.llm,
                tts=context.tts,
                prompts=context.prompts,
                current_slide=1,
            )
        )

    def sample_of(trace: TurnTrace) -> dict[str, Any]:
        """Read a turn's timings as the client would receive them."""
        if trace.metrics is None:
            return {}
        return trace.metrics.to_message().model_dump()

    def collect(stage: str) -> list[float]:
        values: list[float] = []
        for trace in traces:
            value = sample_of(trace).get(stage)
            if isinstance(value, int | float):
                values.append(float(value))
        return values

    metrics: dict[str, float] = {}
    items: list[dict[str, Any]] = []
    for stage in ("stt_ms", "llm_ttft_ms", "llm_total_ms", "tts_ttfb_ms"):
        values = collect(stage)
        if not values:
            continue
        metrics[f"{stage}_p50"] = statistics.median(values)
        metrics[f"{stage}_p95"] = max(values)
    for question, trace in zip(questions, traces, strict=True):
        items.append(
            {
                "id": question,
                "answer": trace.answer,
                "sentences": trace.sentences,
                "metrics": sample_of(trace),
                "error": trace.error,
            }
        )
    return SuiteResult(
        suite="E5",
        title="Latency",
        metrics=metrics,
        # Server-side first audio is time to first token plus synthesis, which is what the two
        # thresholds below add up to; the client measures the whole span and the HUD shows it.
        thresholds={"llm_ttft_ms_p95": ("<=", 2500.0), "tts_ttfb_ms_p95": ("<=", 600.0)},
        items=items,
        note=f"{len(traces)} live turns with real synthesis; p95 of three runs is the maximum.",
    )


async def calibrate(context: Context) -> SuiteResult:
    """Check the judge against hand labels before its verdicts are believed.

    Args:
        context: Providers and deck.

    Returns:
        Agreement with the labelled set.
    """
    dataset = read_dataset("judge_calibration.jsonl")
    instructions = rubric("grounded")

    async def one(item: dict[str, Any]) -> dict[str, Any]:
        notes = context.deck.slide(item["slide"]).notes
        case = (
            f"SLIDE NOTES:\n{notes}\n\n"
            f"QUESTION: {item['question']}\n\n"
            f"ANSWER: {item['answer']}\n\n"
            "The notes do contain the answer."
        )
        verdict = await judge(context.judge_llm, instructions, case)
        score = verdict.payload.get("score")
        return {
            "id": item["id"],
            "expected": item["label"],
            "actual": score if isinstance(score, int) else None,
            "agrees": score == item["label"],
            "why": item["why"],
            "rationale": verdict.payload.get("rationale", ""),
            "judge_error": verdict.error,
        }

    items = await _map(dataset, one)
    return SuiteResult(
        suite="JUDGE",
        title="Judge calibration",
        metrics={"agreement": _ratio(sum(1 for row in items if row["agrees"]), len(items))},
        thresholds={"agreement": (">=", CALIBRATION_THRESHOLD)},
        items=items,
        note="Judged suites are only believed when this clears its threshold.",
    )


def _ratio(numerator: int, denominator: int) -> float:
    """Divide, treating an empty denominator as zero rather than an error.

    Args:
        numerator: The count.
        denominator: The total.

    Returns:
        The ratio, or 0.0 when there was nothing to count.
    """
    return numerator / denominator if denominator else 0.0
