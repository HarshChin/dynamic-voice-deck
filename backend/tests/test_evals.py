"""The deterministic half of the eval harness (TRD §13.2).

The suites themselves call real models and are not tests; what is testable is everything around
them -- how a dataset is read, how a style failure is recognised, how a threshold is compared, how
a judge's reply is parsed, and what the summary table says. Those are the parts that decide whether
a recorded number means what it claims, so they are worth pinning.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.errors import ProviderError
from app.pipeline.prompt import PromptBuilder
from app.providers.base import Message, TokenDelta, ToolSpec
from evals import harness, pacing
from evals.budget import BUDGET, NOT_ATTEMPTED
from evals.judge import judge
from evals.pacing import PacedLLM, Pacer
from evals.report import to_markdown
from evals.suites import (
    STYLE_MAX_SENTENCES,
    STYLE_MAX_WORDS,
    SuiteResult,
    _exclusions,
    _ratio,
    _style_failures,
    e4_style,
    e6_tools,
)

from tests.fakes import FakeTTS


@pytest.fixture(autouse=True)
def _fresh_budget() -> Iterator[None]:
    """A spent budget is module state, and must not leak from one test into the next."""
    BUDGET.reset()
    yield
    BUDGET.reset()


class ScriptedJudge:
    """A judge that replies with whatever text a test hands it."""

    def __init__(self, reply: str) -> None:
        self.name = "scripted"
        self._reply = reply
        self.calls: list[list[Message]] = []

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        tool_choice: str = "auto",
    ) -> Any:
        """Yield the scripted reply as one token."""
        self.calls.append(list(messages))
        yield TokenDelta(text=self._reply)


class RefusingModel:
    """A model whose account has been refused for the day: every call is a 429 with a long wait."""

    name = "refusing"

    def __init__(self, retry_after: float) -> None:
        self._retry_after = retry_after
        self.calls = 0

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        tool_choice: str = "auto",
    ) -> Any:
        """Refuse, the way the free tier does once the daily bucket is empty."""
        self.calls += 1
        raise ProviderError("groq_llm", "HTTP 429", retryable=True, retry_after=self._retry_after)
        yield  # pragma: no cover - unreachable, kept so this is an async generator


class FailingJudge:
    """A judge whose provider is down."""

    name = "failing"

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        tool_choice: str = "auto",
    ) -> Any:
        """Fail instead of replying."""
        raise RuntimeError("upstream refused")
        yield  # pragma: no cover - unreachable, kept so this is an async generator


# --------------------------------------------------------------------------- #
# Datasets                                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "minimum"),
    [("routing.jsonl", 18), ("interruption.jsonl", 6), ("grounded.jsonl", 10)],
)
def test_every_dataset_meets_the_size_the_trd_requires(name: str, minimum: int) -> None:
    """TC-BE-290: TRD §13.1 -- a suite below its stated size is not the suite that was specified.

    The sizes are the release-gate sizes set on 2026-09-11, when the forty-item routing set turned
    out to cost a whole day of the free tier on its own (EVALS.md).
    """
    assert len(harness.read_dataset(name)) >= minimum


def test_dataset_ids_are_unique_within_a_file() -> None:
    """TC-BE-291: a duplicate id would silently overwrite a result in the report."""
    for name in (
        "routing.jsonl",
        "interruption.jsonl",
        "grounded.jsonl",
        "judge_calibration.jsonl",
    ):
        items = harness.read_dataset(name)
        ids = [item["id"] for item in items]
        assert len(set(ids)) == len(ids), name


def test_the_routing_set_covers_every_category_the_design_names() -> None:
    """TC-BE-292: TRD §13.1 -- accuracy over one kind of question is not accuracy."""
    counts: dict[str, int] = {}
    for item in harness.read_dataset("routing.jsonl"):
        counts[item["category"]] = counts.get(item["category"], 0) + 1

    # Three per category: the set is balanced so no category's rate is hidden by another's.
    assert counts == {
        "direct": 3,
        "paraphrase": 3,
        "relative": 3,
        "cross-reference": 3,
        "stay": 3,
        "off-topic": 3,
    }


def test_the_grounded_set_includes_questions_the_deck_cannot_answer() -> None:
    """TC-BE-293: TRD §13.1 -- the decline rate needs something to decline."""
    items = harness.read_dataset("grounded.jsonl")
    assert sum(1 for item in items if not item["answerable"]) >= 5


def test_every_slide_a_dataset_names_exists_in_the_deck() -> None:
    """TC-BE-294: an item pointing at a missing slide would fail as a routing error."""
    deck = harness.load_deck()
    for name, key in [("grounded.jsonl", "slide"), ("judge_calibration.jsonl", "slide")]:
        for item in harness.read_dataset(name):
            assert 1 <= item[key] <= deck.last_index, f"{name}:{item['id']}"
    for item in harness.read_dataset("routing.jsonl"):
        assert 1 <= item["current_slide"] <= deck.last_index, item["id"]
        if item["expected_slide"] is not None:
            assert 1 <= item["expected_slide"] <= deck.last_index, item["id"]


def test_comments_and_blank_lines_are_not_data(tmp_path: Path) -> None:
    """TC-BE-295: the datasets carry their design notes at the top of the file."""
    path = tmp_path / "datasets" / "sample.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('# a note\n\n{"id": "a"}\n{"id": "b"}\n', encoding="utf-8")

    items = [json.loads(line) for line in path.read_text().splitlines() if line.startswith("{")]

    assert items == [{"id": "a"}, {"id": "b"}]


def test_the_limit_caps_a_dataset_for_a_cheap_smoke_run() -> None:
    """TC-BE-296: `--limit` exists so a change to the runner can be proved without a full run."""
    harness.LIMIT = 3
    try:
        assert len(harness.read_dataset("routing.jsonl")) == 3
    finally:
        harness.LIMIT = None


# --------------------------------------------------------------------------- #
# Style (E4)                                                                   #
# --------------------------------------------------------------------------- #


def test_speech_passes_the_style_check() -> None:
    """TC-BE-297: E4 -- an ordinary spoken answer has nothing wrong with it."""
    assert _style_failures("Two layers, actually. The browser stops first.", ["a", "b"]) == []


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("Read **this** carefully.", "markup"),
        ("- first point", "markup"),
        ("See https://example.com for more.", "markup"),
        ("Great question 🎉", "markup"),
        ("Use `go_to_slide` for that.", "markup"),
        ("See [the docs](https://example.com).", "markup"),
    ],
)
def test_anything_that_reads_as_written_fails_the_style_check(answer: str, expected: str) -> None:
    """TC-BE-298: E4 -- markdown, links and emoji are not speech."""
    problems = _style_failures(answer, ["one"])
    assert problems, answer
    assert any(expected in problem for problem in problems)


def test_an_answer_that_runs_long_fails_on_length() -> None:
    """TC-BE-299: E4 -- a presenter who monologues has stopped presenting."""
    long_answer = " ".join(["word"] * (STYLE_MAX_WORDS + 1))
    assert any("words" in problem for problem in _style_failures(long_answer, ["one"]))

    many = ["A sentence."] * (STYLE_MAX_SENTENCES + 1)
    assert any("sentences" in problem for problem in _style_failures("A sentence.", many))


def test_the_style_suite_reads_every_answer_the_other_suites_produced() -> None:
    """TC-BE-300: E4 is derived, so it must not need its own model calls."""
    routing = SuiteResult(
        suite="E1",
        title="Slide routing",
        items=[
            {"id": "r001", "answer": "Short and spoken.", "sentences": ["Short and spoken."]},
            {"id": "r002", "answer": "Use **markdown**.", "sentences": ["Use **markdown**."]},
            {"id": "r003", "answer": "", "sentences": []},
        ],
    )

    result = e4_style([routing])

    # The empty answer is not counted either way: there was nothing to judge.
    assert len(result.items) == 2
    assert result.metrics["pass_rate"] == 0.5
    assert not result.passed


# --------------------------------------------------------------------------- #
# Thresholds and reporting                                                     #
# --------------------------------------------------------------------------- #


def test_a_threshold_is_compared_in_the_direction_it_was_written() -> None:
    """TC-BE-301: a rate that must stay low and one that must stay high are not the same check."""
    result = SuiteResult(
        suite="E1",
        title="Slide routing",
        metrics={"accuracy": 0.92, "false_navigation": 0.10},
        thresholds={"accuracy": (">=", 0.90), "false_navigation": ("<=", 0.05)},
    )

    assert result.meets("accuracy")
    assert not result.meets("false_navigation")
    assert not result.passed


def test_a_metric_that_was_never_measured_does_not_silently_pass() -> None:
    """TC-BE-302: a missing number is a failed threshold, not an absent one."""
    result = SuiteResult(
        suite="E3",
        title="Groundedness",
        metrics={},
        thresholds={"mean_score": (">=", 1.7)},
    )

    assert not result.meets("mean_score")


def test_an_empty_denominator_is_zero_rather_than_an_error() -> None:
    """TC-BE-303: a suite whose items all failed still has to produce a report."""
    assert _ratio(0, 0) == 0.0
    assert _ratio(3, 4) == 0.75


def test_the_summary_names_each_metric_with_its_units() -> None:
    """TC-BE-304: TR-202 -- the table is pasted into EVALS.md, so it has to read on its own."""
    results = [
        SuiteResult(
            suite="E1",
            title="Slide routing",
            metrics={"accuracy": 0.925},
            thresholds={"accuracy": (">=", 0.90)},
            note="40 of 40 items answered.",
        ),
        SuiteResult(
            suite="E3",
            title="Groundedness",
            metrics={"mean_score": 1.84},
            thresholds={"mean_score": (">=", 1.7)},
        ),
        SuiteResult(suite="E5", title="Latency", metrics={"llm_ttft_ms_p50": 712.0}),
    ]

    table = to_markdown(
        results,
        model="qwen/qwen3.8-27b",
        judge_model="qwen/qwen3.8-27b",
        sha="abc1234",
        stamp="2026-09-11T00-00-00Z",
    )

    assert "2026-09-11 — abc1234 — qwen/qwen3.8-27b" in table
    assert "| E1 Slide routing | accuracy | 92.5 % | >= 90.0 % | yes |" in table
    assert "| E3 Groundedness | mean_score | 1.84 / 2 | >= 1.70 / 2 | yes |" in table
    assert "| E5 Latency | llm_ttft_ms_p50 | 712 ms | — | — |" in table
    assert "40 of 40 items answered." in table


def test_tool_hygiene_ignores_items_the_provider_refused() -> None:
    """TC-BE-305: E6 -- a rate-limited item says nothing about tool calls."""
    routing = SuiteResult(
        suite="E1",
        title="Slide routing",
        items=[
            {"id": "r050", "category": "off-topic", "sources": [], "moved": False, "error": None},
            {
                "id": "r051",
                "category": "off-topic",
                "sources": ["llm"],
                "moved": True,
                "error": None,
            },
            {"id": "r052", "category": "off-topic", "sources": [], "moved": False, "error": "429"},
            {"id": "r001", "category": "direct", "sources": ["llm"], "moved": True, "error": None},
        ],
    )

    result = e6_tools(routing)

    # One of the two gradeable off-topic items navigated.
    assert result.metrics["off_topic_navigation"] == 0.5
    assert result.metrics["invalid_calls"] == 0.0
    assert not result.passed


# --------------------------------------------------------------------------- #
# The judge                                                                    #
# --------------------------------------------------------------------------- #


async def test_a_judge_reply_is_read_even_when_it_is_wrapped_in_prose() -> None:
    """TC-BE-306: TR-201 -- models add preambles and code fences; the score is still in there."""
    reply = (
        'Here is my grade:\n```json\n{"score": 2, "rationale": "supported"}\n```\nHope that helps.'
    )

    verdict = await judge(ScriptedJudge(reply), "rubric", "case")

    assert verdict.error is None
    assert verdict.payload == {"score": 2, "rationale": "supported"}


async def test_a_reply_with_no_json_is_an_error_rather_than_a_zero() -> None:
    """TC-BE-307: TR-201 -- an ungradeable item must not be counted as a failed one."""
    verdict = await judge(ScriptedJudge("I am not sure."), "rubric", "case")

    assert verdict.payload == {}
    assert verdict.error is not None
    assert "no JSON" in verdict.error


async def test_unparseable_json_is_reported_with_what_was_said() -> None:
    """TC-BE-308: the raw reply is kept so a surprising verdict can be read back."""
    verdict = await judge(ScriptedJudge('{"score": }'), "rubric", "case")

    assert verdict.error is not None
    assert verdict.raw == '{"score": }'


async def test_a_judge_whose_provider_fails_returns_a_verdict_not_an_exception() -> None:
    """TC-BE-309: one failed grade must not end a run that has already spent a hundred calls."""
    verdict = await judge(FailingJudge(), "rubric", "case")

    assert verdict.payload == {}
    assert verdict.error is not None
    assert "RuntimeError" in verdict.error


async def test_the_rubric_is_the_system_message_and_the_case_is_the_user_message() -> None:
    """TC-BE-310: TR-201 -- the rubric is fixed and the item varies, not the other way round."""
    scripted = ScriptedJudge('{"score": 1}')

    await judge(scripted, "THE RUBRIC", "THE CASE")

    roles = [(message.role, message.content) for message in scripted.calls[0]]
    assert roles == [("system", "THE RUBRIC"), ("user", "THE CASE")]


# --------------------------------------------------------------------------- #
# The daily budget (TR-205)                                                    #
# --------------------------------------------------------------------------- #


async def test_a_refusal_longer_than_the_wait_bound_ends_the_runs_attempts() -> None:
    """TC-BE-346: TR-205 -- once the day is spent, later items are skipped rather than refused."""
    refusing = RefusingModel(retry_after=1217.0)
    shared: dict[str, Any] = {
        "deck": harness.load_deck(),
        "llm": refusing,
        "tts": FakeTTS(),
        "prompts": PromptBuilder(),
        "current_slide": 1,
    }

    first = await harness.run_one(utterance="How does barge-in work?", **shared)
    second = await harness.run_one(utterance="Next slide please.", **shared)

    assert first.error is not None
    assert "429" in first.error
    assert BUDGET.exhausted_after_s == 1217.0
    assert second.error == NOT_ATTEMPTED
    assert refusing.calls == 1


async def test_the_judge_is_not_asked_once_the_day_is_spent() -> None:
    """TC-BE-347: TR-205 -- the judge is not asked against a budget that is known to be spent."""
    BUDGET.spend(900.0)
    scripted = ScriptedJudge('{"score": 2}')

    verdict = await judge(scripted, "rubric", "case")

    assert verdict.error == NOT_ATTEMPTED
    assert scripted.calls == []


def test_the_note_separates_items_never_attempted_from_items_refused() -> None:
    """TC-BE-348: TR-205 -- a reader must be able to tell a spent budget from a flaky provider."""
    items = [
        {"error": None},
        {"error": "ProviderError: groq_llm: HTTP 429"},
        {"error": NOT_ATTEMPTED},
        {"error": NOT_ATTEMPTED},
    ]

    assert _exclusions(items) == (
        "; 3 excluded after a provider failure, 2 of them not attempted once the daily budget "
        "had run out."
    )
    assert _exclusions([{"error": None}]) == "."


async def test_the_pacer_spaces_calls_by_at_least_the_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-BE-344: a refused request counts against the minute that refused it; never be refused."""

    clock = {"now": 100.0}
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(pacing.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(pacing.asyncio, "sleep", fake_sleep)
    pacer = pacing.Pacer(31.0)

    await pacer.wait()  # first call goes immediately
    await pacer.wait()  # second waits the full interval
    await pacer.wait()

    assert slept == [31.0, 31.0]


async def test_a_paced_provider_waits_then_delegates_unchanged() -> None:
    """TC-BE-345: pacing changes when a call happens, never what it returns."""

    inner = ScriptedJudge('{"score": 2}')
    waited: list[bool] = []

    class CountingPacer(Pacer):
        async def wait(self) -> None:
            waited.append(True)

    paced = PacedLLM(inner, CountingPacer(31.0))
    events = [
        event async for event in paced.stream([Message(role="user", content="q")], [], "none")
    ]

    assert waited == [True]
    assert [event.text for event in events if isinstance(event, TokenDelta)] == ['{"score": 2}']
    assert paced.name == inner.name
