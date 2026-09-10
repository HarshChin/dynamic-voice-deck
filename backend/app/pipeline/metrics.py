"""Per-turn latency instrumentation (TRD §8, TR-035, TR-163).

Every stage of a turn is timed so the latency budget in TRD §8.1 can be checked
against reality rather than intuition. Timings use :func:`time.perf_counter`,
which is monotonic, so a clock adjustment mid-turn cannot produce a negative
duration.

The client measures the two numbers that matter most to a listener --
``first_audio_ms`` and ``interrupt_stop_ms`` -- because only the browser knows
when a sample actually reached the speakers. This module covers the server side.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..protocol import MetricsMsg


def _ms_between(start: float | None, end: float | None) -> int | None:
    """Return the whole milliseconds between two perf-counter readings.

    Args:
        start: Earlier reading, or ``None`` if the stage never began.
        end: Later reading, or ``None`` if the stage never completed.

    Returns:
        The elapsed milliseconds, or ``None`` when either reading is missing.
    """
    if start is None or end is None:
        return None
    return max(0, round((end - start) * 1000))


@dataclass(slots=True)
class TurnMetrics:
    """Timings for one turn, from user input to the last sentence sent.

    Attributes:
        turn_id: The turn these timings describe.
        started: When the turn began.
        stt_started: When transcription was requested.
        stt_finished: When the transcript arrived.
        llm_started: When the model request was issued.
        llm_first_token: When the first token arrived.
        llm_finished: When the model stream ended.
        tts_first_request: When the first sentence was handed to synthesis.
        tts_first_audio: When the first audio bytes came back.
        sentences: How many sentences the turn produced.
    """

    turn_id: int
    started: float = field(default_factory=time.perf_counter)
    stt_started: float | None = None
    stt_finished: float | None = None
    llm_started: float | None = None
    llm_first_token: float | None = None
    llm_finished: float | None = None
    tts_first_request: float | None = None
    tts_first_audio: float | None = None
    sentences: int = 0

    @staticmethod
    def now() -> float:
        """Return the current monotonic reading.

        Returns:
            A :func:`time.perf_counter` value.
        """
        return time.perf_counter()

    def set_stt_ms(self, milliseconds: int) -> None:
        """Record a transcription cost measured before the turn began.

        The session transcribes an utterance *before* it knows there is a turn
        to run, because an empty transcript means there is no turn at all. The
        cost is real and belongs in the latency panel, so it is carried in
        rather than timed here.

        Args:
            milliseconds: How long transcription took.
        """
        now = self.now()
        self.stt_started = now - milliseconds / 1000
        self.stt_finished = now

    def mark_stt_start(self) -> None:
        """Record that transcription was requested."""
        self.stt_started = self.now()

    def mark_stt_done(self) -> None:
        """Record that the transcript arrived."""
        self.stt_finished = self.now()

    def mark_llm_start(self) -> None:
        """Record that the model request was issued."""
        self.llm_started = self.now()

    def mark_first_token(self) -> None:
        """Record the first token, ignoring later calls.

        Only the first token defines time-to-first-token, so subsequent calls
        are deliberately no-ops and the caller need not track whether it has
        already reported one.
        """
        if self.llm_first_token is None:
            self.llm_first_token = self.now()

    def mark_llm_done(self) -> None:
        """Record that the model stream ended."""
        self.llm_finished = self.now()

    def mark_tts_request(self) -> None:
        """Record the first sentence being handed to synthesis, once."""
        if self.tts_first_request is None:
            self.tts_first_request = self.now()

    def mark_first_audio(self) -> None:
        """Record the first audio bytes returning from synthesis, once."""
        if self.tts_first_audio is None:
            self.tts_first_audio = self.now()

    @property
    def stt_ms(self) -> int | None:
        """Milliseconds spent transcribing.

        Returns:
            The duration, or ``None`` for a typed turn with no transcription.
        """
        return _ms_between(self.stt_started, self.stt_finished)

    @property
    def llm_ttft_ms(self) -> int | None:
        """Milliseconds from the model request to its first token.

        Returns:
            The duration, or ``None`` if no token ever arrived.
        """
        return _ms_between(self.llm_started, self.llm_first_token)

    @property
    def llm_total_ms(self) -> int | None:
        """Milliseconds the whole model stream took.

        Returns:
            The duration, or ``None`` if the stream never finished, which is the
            normal case for an interrupted turn.
        """
        return _ms_between(self.llm_started, self.llm_finished)

    @property
    def tts_ttfb_ms(self) -> int | None:
        """Milliseconds from the first synthesis request to its first bytes.

        Returns:
            The duration, or ``None`` if synthesis never produced audio.
        """
        return _ms_between(self.tts_first_request, self.tts_first_audio)

    def to_message(self) -> MetricsMsg:
        """Render these timings as the wire message.

        Returns:
            The ``metrics`` message for this turn.
        """
        return MetricsMsg(
            turn_id=self.turn_id,
            stt_ms=self.stt_ms,
            llm_ttft_ms=self.llm_ttft_ms,
            llm_total_ms=self.llm_total_ms,
            tts_ttfb_ms=self.tts_ttfb_ms,
            sentences=self.sentences,
        )
