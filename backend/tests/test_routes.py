"""Deck HTTP routes and the session endpoint's startup guard (TRD §4.10).

The frontend fetches the deck over HTTP before it opens a socket, so these
routes are the first thing a running process is asked for. Two behaviours are
worth pinning: the shipped deck is served whole and unmodified, and a process
whose startup has not completed says so with ``503`` instead of pretending the
deck does not exist -- the honest answer, and the one that tells an operator to
look at startup rather than at the deck files.

The 503 cases use the shared ``client`` fixture, which deliberately does not
enter the ``TestClient`` as a context manager and so never runs the lifespan.
The 200 cases attach an application state by hand for the same reason
``tests/test_session.py`` does: startup work belongs in ``test_startup.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from app.config import Settings
from app.decks.models import Deck
from app.decks.repository import DeckRepository
from app.main import AppState, create_app
from app.pipeline.prompt import PromptBuilder
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

DECK_ID = "anatomy_of_a_voice_agent"
DECKS_PATH = "/api/decks"
DECK_PATH = f"/api/decks/{DECK_ID}"
WS_PATH = "/ws/session"
SLIDE_COUNT = 6
"""Slides in the shipped deck (PRD §4 asks for five to six)."""


@pytest.fixture
def started_client(isolated_env: Any) -> Iterator[TestClient]:
    """Return a client for an application whose state is already built.

    Args:
        isolated_env: Ordering dependency, so settings are read from the
            scrubbed environment rather than the developer's ``.env``.

    Yields:
        A client whose application has decks loaded, as it would after startup.
    """
    app = create_app()
    app.state.app_state = AppState(
        settings=Settings(),
        decks=DeckRepository(),
        prompts=PromptBuilder(),
    )
    client = TestClient(app)
    yield client
    client.close()


@pytest.fixture
def unstarted_client(isolated_env: Any) -> Iterator[TestClient]:
    """Return a client for an application that has state but never finished startup.

    This is the shape ``get_app_state`` falls back to when the lifespan has not
    run: settings only, with no decks, providers, or prompt builder.

    Args:
        isolated_env: Ordering dependency on the environment scrub.

    Yields:
        A client whose application state is incomplete.
    """
    app = create_app()
    app.state.app_state = AppState(settings=Settings())
    client = TestClient(app)
    yield client
    client.close()


def test_the_deck_listing_names_the_shipped_deck(started_client: TestClient) -> None:
    """TC-BE-190: GET /api/decks returns one summary per deck, ordered by id."""
    response = started_client.get(DECKS_PATH)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")

    payload = response.json()

    assert payload == [
        {"id": DECK_ID, "title": "Anatomy of a Voice Agent", "slide_count": SLIDE_COUNT}
    ]
    # A summary and nothing more: the listing is for a picker, so it must not
    # carry six slides' worth of notes.
    assert set(payload[0]) == {"id", "title", "slide_count"}


def test_a_deck_is_served_whole(started_client: TestClient) -> None:
    """TC-BE-191: GET /api/decks/{id} returns the deck the agent presents."""
    response = started_client.get(DECK_PATH)

    assert response.status_code == 200

    deck = Deck.model_validate(response.json())

    assert deck.id == DECK_ID
    assert len(deck.slides) == SLIDE_COUNT
    assert [slide.index for slide in deck.slides] == list(range(1, SLIDE_COUNT + 1))
    # Notes and aliases are included: the frontend renders the deck and the
    # session's `session.ready` carries the same model.
    assert all(slide.notes for slide in deck.slides)
    assert all(slide.aliases for slide in deck.slides)


def test_an_unknown_deck_is_a_404_that_names_what_does_exist(
    started_client: TestClient,
) -> None:
    """TC-BE-192: an id that no deck carries is not found, and says which are."""
    response = started_client.get("/api/decks/no_such_deck")

    assert response.status_code == 404

    detail = response.json()["detail"]

    assert "no_such_deck" in detail
    # The usual cause is a typo, so the message lists the ids that do exist.
    assert DECK_ID in detail


@pytest.mark.parametrize("path", [DECKS_PATH, DECK_PATH], ids=["listing", "deck"])
def test_the_deck_routes_answer_503_before_startup_completes(client: TestClient, path: str) -> None:
    """TC-BE-193: TR-151 -- without the lifespan the repository is missing, not empty.

    Answering ``200 []`` or ``404`` here would be a lie: the decks exist, the
    process is simply not ready to serve them yet.
    """
    response = client.get(path)

    assert response.status_code == 503
    assert "startup" in response.json()["detail"]


def test_the_session_socket_refuses_a_connection_before_startup_completes(
    unstarted_client: TestClient,
) -> None:
    """TC-BE-194: TR-026 -- the socket closes with 1011 rather than accepting blindly.

    A client that connected successfully and then heard nothing would look like
    a hung agent; an immediate internal-error close is what makes the frontend's
    reconnect logic (TR-175) do the right thing.

    The application state exists here but is empty, which is what an incomplete
    startup leaves behind. ``session_endpoint`` reads ``app.state.app_state``
    directly rather than through ``get_app_state``, so a process with *no*
    state at all -- the shared ``client`` fixture -- raises ``AttributeError``
    instead of closing cleanly; that gap is reported rather than asserted here,
    because ``app/main.py`` belongs to another change.
    """
    with pytest.raises(WebSocketDisconnect) as excinfo, unstarted_client.websocket_connect(WS_PATH):
        pass  # pragma: no cover - the connection never opens

    assert excinfo.value.code == 1011
    assert "startup" in excinfo.value.reason


def test_health_still_answers_before_startup(client: TestClient) -> None:
    """TC-BE-193: the 503s above are per-route, not the whole application failing."""
    assert client.get("/api/health").status_code == 200
