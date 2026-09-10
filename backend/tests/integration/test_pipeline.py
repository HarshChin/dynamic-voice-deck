"""The whole pipeline against the real providers (PRD F5, F6, TR-125).

Everything else in the suite replaces the network with a fake, which is what makes the suite fast
and free. What no fake can tell us is whether the thing is quick enough to talk to, or whether the
model actually routes a question to the right slide when it is the model and not a script deciding.
Both need real calls, so both live here and neither runs by default.

The application goes through its real startup: the credential is put back into the environment the
unit suite strips, and the lifespan then builds the providers and warms the synthesiser exactly as
``make backend`` does. Anything less would measure a cold model and call it latency.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from app.main import create_app
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

DECK_ID = "anatomy_of_a_voice_agent"

FIRST_AUDIO_BUDGET_MS = 1_500
"""TR-125: what the product promises between asking and hearing."""

INTERRUPTION_SLIDE = 4
"""The slide about barge-in, which is where a question about interruptions belongs."""


@pytest.fixture
def live_client(
    monkeypatch: pytest.MonkeyPatch, groq_api_key: str, groq_llm_model: str
) -> Iterator[TestClient]:
    """A client for an application that started the way the shipped one does.

    The unit suite's autouse isolation deletes every settings variable so no test can reach a real
    provider by accident; these tests put back exactly the two they need and then let the ordinary
    lifespan run, so the providers, the deck repository and the synthesiser warm-up are all the
    real ones.

    Args:
        monkeypatch: Pytest patcher, undone at teardown.
        groq_api_key: The credential read from the repository's ``.env``.
        groq_llm_model: The model the project is configured to answer with.

    Yields:
        A client whose application has completed startup.
    """
    monkeypatch.setenv("GROQ_API_KEY", groq_api_key)
    monkeypatch.setenv("GROQ_LLM_MODEL", groq_llm_model)
    with TestClient(create_app()) as client:
        assert client.get("/api/health").json()["tts_warm"] is True
        yield client


def ask(client: TestClient, question: str) -> dict[str, Any]:
    """Run one complete turn and report what came back.

    Args:
        client: The test client for a live application.
        question: What to ask.

    Returns:
        The messages, the audio frame count, and the time to the first frame.
    """
    with client.websocket_connect("/ws/session") as ws:
        ws.send_text(json.dumps({"type": "session.start", "deck_id": DECK_ID, "mode": "qa"}))
        messages: list[dict[str, Any]] = []
        asked_at = first_audio_at = None
        frames = 0

        while True:
            frame = ws.receive()
            if frame.get("bytes") is not None:
                frames += 1
                if first_audio_at is None:
                    first_audio_at = time.perf_counter()
                continue
            message: dict[str, Any] = json.loads(frame["text"])
            messages.append(message)
            if message["type"] == "session.ready":
                asked_at = time.perf_counter()
                ws.send_text(json.dumps({"type": "text.input", "text": question}))
            elif message["type"] == "error":
                pytest.fail(f"{message['code']}: {message['message']}")
            elif message["type"] == "metrics":
                break

    assert asked_at is not None
    return {
        "messages": messages,
        "frames": frames,
        "first_audio_ms": None
        if first_audio_at is None
        else round(1000 * (first_audio_at - asked_at)),
    }


def spoken(result: dict[str, Any]) -> str:
    """Join the sentences the agent said.

    Args:
        result: What :func:`ask` returned.

    Returns:
        The whole answer as one string.
    """
    return " ".join(
        message["text"] for message in result["messages"] if message["type"] == "transcript.agent"
    )


def test_a_question_is_answered_out_loud_within_the_latency_budget(
    live_client: TestClient,
) -> None:
    """TC-INT-005: the first audio frame leaves inside the budget, measured from the question.

    Server-side, so it excludes the browser's own scheduling; the client measures the same span
    from its side and shows it in the latency panel. The budget is the one the product promises,
    not the one it happens to hit, so this test is allowed to fail when a provider is slow.
    """
    result = ask(live_client, "What is this deck about?")

    assert result["frames"] > 0, "the agent said nothing"
    assert result["first_audio_ms"] is not None
    assert result["first_audio_ms"] <= FIRST_AUDIO_BUDGET_MS, (
        f"first audio took {result['first_audio_ms']} ms"
    )


def test_a_question_about_interruptions_moves_the_deck_to_the_slide_about_them(
    live_client: TestClient,
) -> None:
    """TC-INT-003: the model, not a script, routes the question to the right slide."""
    result = ask(live_client, "How do you handle interruptions?")

    moves = [m for m in result["messages"] if m["type"] == "slide.goto"]
    assert moves, f"the deck never moved; the agent said: {spoken(result)}"
    assert moves[-1]["index"] == INTERRUPTION_SLIDE, moves


def test_a_question_the_deck_does_not_answer_is_declined_without_moving_it(
    live_client: TestClient,
) -> None:
    """TC-INT-004: an off-topic question turns back to the deck, briefly, and moves nothing.

    The brevity matters as much as the refusal: a presenter who monologues about the weather has
    stopped presenting. Three sentences is the allowance, because the prompt asks for one and a
    model that adds an offer of what it *can* cover is doing the right thing.
    """
    result = ask(live_client, "What is the weather in London today?")

    answer = spoken(result)
    sentences = [m for m in result["messages"] if m["type"] == "transcript.agent"]
    assert len(sentences) <= 3, answer
    assert not [m for m in result["messages"] if m["type"] == "slide.goto"], answer
