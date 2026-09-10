"""Per-turn latency instrumentation (:mod:`app.pipeline.metrics`).

Metrics are the only evidence behind the latency numbers in the README
(CLAUDE.md §3.4), so the rules that keep them honest are worth pinning: a stage
that did not both start and finish reports ``None`` rather than a plausible
zero, and the "first" marks record the first call and ignore every later one --
otherwise time-to-first-token would quietly become time-to-last-token.

Timings come from :func:`time.perf_counter`, so the durations here are asserted
as bounds and types rather than as exact numbers; a monotonic clock is the point
of TR-035, not a particular value.
"""

from __future__ import annotations

import time

import pytest
from app.pipeline.metrics import TurnMetrics
from app.protocol import MetricsMsg

TURN_ID = 7
"""Turn identifier used throughout; any value that survives to the wire will do."""

DURATIONS = ("stt_ms", "llm_ttft_ms", "llm_total_ms", "tts_ttfb_ms")
"""Every duration :class:`~app.pipeline.metrics.TurnMetrics` reports."""


def test_a_fresh_turn_reports_no_duration_at_all() -> None:
    """TC-BE-180: TR-163 -- before any stage runs, every duration is None."""
    metrics = TurnMetrics(turn_id=TURN_ID)

    assert [getattr(metrics, name) for name in DURATIONS] == [None, None, None, None]
    assert metrics.sentences == 0
    # `started` is stamped at construction, so a turn always knows when it began.
    assert isinstance(metrics.started, float)


@pytest.mark.parametrize(
    ("duration", "start", "end"),
    [
        ("stt_ms", "mark_stt_start", "mark_stt_done"),
        ("llm_ttft_ms", "mark_llm_start", "mark_first_token"),
        ("llm_total_ms", "mark_llm_start", "mark_llm_done"),
        ("tts_ttfb_ms", "mark_tts_request", "mark_first_audio"),
    ],
)
def test_a_duration_stays_none_until_both_of_its_ends_are_marked(
    duration: str, start: str, end: str
) -> None:
    """TC-BE-180: TR-163 -- a half-finished stage reports nothing, not zero.

    The interrupted turn is the case that matters: the model stream is cancelled
    part way, so ``llm_total_ms`` has a start and no end, and a zero there would
    read as an impossibly fast answer.
    """
    metrics = TurnMetrics(turn_id=TURN_ID)

    getattr(metrics, start)()

    assert getattr(metrics, duration) is None

    getattr(metrics, end)()
    measured = getattr(metrics, duration)

    assert isinstance(measured, int)
    assert measured >= 0


def test_an_end_without_a_start_is_still_none() -> None:
    """TC-BE-180: a stage that never began cannot have a duration."""
    metrics = TurnMetrics(turn_id=TURN_ID)

    metrics.mark_stt_done()
    metrics.mark_first_token()
    metrics.mark_llm_done()
    metrics.mark_first_audio()

    assert [getattr(metrics, name) for name in DURATIONS] == [None, None, None, None]


@pytest.mark.parametrize(
    ("mark", "field"),
    [
        ("mark_first_token", "llm_first_token"),
        ("mark_first_audio", "tts_first_audio"),
        ("mark_tts_request", "tts_first_request"),
    ],
)
def test_the_first_marks_ignore_every_later_call(mark: str, field: str) -> None:
    """TC-BE-181: TR-035 -- only the first token, request, and audio chunk count.

    The pipeline calls these once per sentence rather than once per turn, so
    idempotence is what makes the caller's life simple: it never has to track
    whether it has already reported one.
    """
    metrics = TurnMetrics(turn_id=TURN_ID)

    getattr(metrics, mark)()
    first = getattr(metrics, field)
    # A busy wait rather than a sleep: perf_counter must actually advance, and
    # this costs microseconds.
    spin_until_the_clock_moves()
    getattr(metrics, mark)()

    assert getattr(metrics, field) == first


def test_time_to_first_token_measures_the_first_token_not_the_last() -> None:
    """TC-BE-181: TR-035 -- a long stream does not inflate the TTFT it reports."""
    metrics = TurnMetrics(turn_id=TURN_ID)
    metrics.mark_llm_start()
    metrics.mark_first_token()
    ttft = metrics.llm_ttft_ms

    for _ in range(3):
        spin_until_the_clock_moves()
        metrics.mark_first_token()
    metrics.mark_llm_done()

    assert metrics.llm_ttft_ms == ttft
    # The whole stream took at least as long as its first token did.
    assert metrics.llm_total_ms is not None
    assert ttft is not None
    assert metrics.llm_total_ms >= ttft


def test_the_message_carries_every_timing_the_turn_recorded() -> None:
    """TC-BE-182: TR-163 -- to_message renders a valid MetricsMsg for the wire."""
    metrics = TurnMetrics(turn_id=TURN_ID)
    metrics.mark_stt_start()
    metrics.mark_stt_done()
    metrics.mark_llm_start()
    metrics.mark_first_token()
    metrics.mark_llm_done()
    metrics.mark_tts_request()
    metrics.mark_first_audio()
    metrics.sentences = 3

    message = metrics.to_message()

    assert isinstance(message, MetricsMsg)
    assert message.type == "metrics"
    assert message.turn_id == TURN_ID
    assert message.sentences == 3
    assert [getattr(message, name) for name in DURATIONS] == [
        getattr(metrics, name) for name in DURATIONS
    ]
    assert all(isinstance(getattr(message, name), int) for name in DURATIONS)

    # It survives the round trip the socket puts it through.
    assert MetricsMsg.model_validate_json(message.model_dump_json()) == message


def test_a_turn_that_produced_nothing_still_renders_a_message() -> None:
    """TC-BE-182: TR-163 -- a dropped or failed turn reports Nones, not an error."""
    message = TurnMetrics(turn_id=TURN_ID).to_message()

    assert message.model_dump() == {
        "type": "metrics",
        "turn_id": TURN_ID,
        "stt_ms": None,
        "llm_ttft_ms": None,
        "llm_total_ms": None,
        "tts_ttfb_ms": None,
        "sentences": 0,
    }


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (1.0, 1.0, 0),
        (1.0, 1.2345, 234),
        (1.0, 1.2346, 235),
        (1.0, 1.0004, 0),
        # A stage cannot take negative time; a reading out of order is clamped
        # rather than reported as a number no reader could interpret.
        (2.0, 1.0, 0),
        (None, 1.0, None),
        (1.0, None, None),
        (None, None, None),
    ],
)
def test_durations_are_whole_non_negative_milliseconds(
    start: float | None, end: float | None, expected: int | None
) -> None:
    """TC-BE-183: TR-035 -- elapsed time is rounded to milliseconds and never negative.

    The readings are written straight onto the dataclass rather than produced by
    marking stages, because these are the edge cases a real clock will not
    reproduce on demand: sub-millisecond stages, a half-millisecond that must
    round, and readings that arrive out of order.
    """
    metrics = TurnMetrics(turn_id=TURN_ID)
    metrics.stt_started = start
    metrics.stt_finished = end

    assert metrics.stt_ms == expected


def test_the_clock_is_monotonic() -> None:
    """TC-BE-183: TR-035 -- readings come from perf_counter, so they never go back."""
    first = TurnMetrics.now()
    spin_until_the_clock_moves()
    second = TurnMetrics.now()

    assert second > first
    assert isinstance(first, float)


def spin_until_the_clock_moves() -> None:
    """Busy-wait until :func:`time.perf_counter` reports a new value.

    Sleeping would work too but costs a scheduling round trip in every
    parametrised case; the counter's resolution is nanoseconds, so this returns
    almost immediately.
    """
    start = time.perf_counter()
    while time.perf_counter() == start:  # pragma: no cover - typically one iteration
        pass
