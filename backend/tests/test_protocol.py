"""The wire contract itself (:mod:`app.protocol`).

Two things are checked here. First, that an invalid frame is *rejected* rather
than half-understood: the discriminated union is the only thing standing between
a typo in the client and a session that behaves unpredictably, so each row also
follows the rejection out to the ``error`` the client actually receives. Second,
that the binary audio framing is byte-exact, because the frontend decodes it
with a hand-written ``DataView`` reader (``frontend/src/protocol.ts``) and a
one-byte disagreement would be heard as noise rather than seen as an exception.

The WebSocket harness is imported from ``tests/test_session.py`` rather than
duplicated: it is the same session, driven the same way, and one copy of it can
only drift from the other.
"""

from __future__ import annotations

import json
from typing import Any, get_args

import pytest
from app.protocol import (
    AUDIO_HEADER,
    AUDIO_HEADER_BYTES,
    CLIENT_MESSAGE_ADAPTER,
    CLIENT_MESSAGE_TYPES,
    MAX_TEXT_INPUT_CHARS,
    PROTOCOL_VERSION,
    SERVER_MESSAGE_TYPES,
    ClientMessage,
    ServerMessage,
    SpeechEndMsg,
    TextInputMsg,
    decode_audio_frame,
    encode_audio_frame,
)
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from pydantic import ValidationError

from tests.fakes import FakeLLM
from tests.test_session import connect

UINT32_MAX = 0xFFFF_FFFF
"""Largest value the header's two little-endian uint32 fields can carry."""

MAX_PAYLOAD_BYTES = 4_800
"""One server audio frame's payload: 100 ms of PCM16 at 24 kHz (TR-141)."""


def literal_types(union: Any) -> frozenset[str]:
    """Return the ``type`` literal of every model in a discriminated union.

    Args:
        union: The ``Annotated[A | B | ..., Field(discriminator=...)]`` alias.

    Returns:
        Every ``type`` string the union accepts.
    """
    members = get_args(get_args(union)[0])
    return frozenset(get_args(model.model_fields["type"].annotation)[0] for model in members)


# --------------------------------------------------------------------------- #
# TC-BE-080 -- a message missing a required field
# --------------------------------------------------------------------------- #


def test_a_speech_end_without_a_duration_is_rejected(isolated_env: Any) -> None:
    """TC-BE-080: TR-142 -- the missing field is named, and the client is told."""
    with pytest.raises(ValidationError) as excinfo:
        CLIENT_MESSAGE_ADAPTER.validate_json('{"type": "speech.end"}')

    problem = excinfo.value.errors()[0]
    assert problem["loc"] == ("speech.end", "duration_ms")
    assert problem["type"] == "missing"

    with connect(FakeLLM()) as harness:
        harness.send(type="speech.end")
        error = harness.recv()
        # An invalid frame is reported and otherwise ignored: the session lives
        # on and the next valid message is served normally (TR-142).
        quiet = harness.barrier()

    assert error["type"] == "error"
    assert error["code"] == "bad_message"
    assert error["recoverable"] is True
    assert "duration_ms" in error["message"]
    assert quiet == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"type": "speech.end", "duration_ms": -1},
        {"type": "speech.end", "duration_ms": "soon"},
        {"type": "playback.progress", "turn_id": 1},
        {"type": "slide.changed", "index": 0, "source": "user"},
        {"type": "slide.changed", "index": 2, "source": "agent"},
        {"type": "control", "action": "sing"},
        {"type": "interrupt", "last_completed_sentence_id": -2},
        {"type": "session.start"},
        {"type": "session.start", "deck_id": "d", "mode": "karaoke"},
    ],
)
def test_a_malformed_client_message_never_validates(payload: dict[str, Any]) -> None:
    """TC-BE-080: TR-142 -- absent, mistyped, and out-of-range fields are all refused."""
    with pytest.raises(ValidationError):
        CLIENT_MESSAGE_ADAPTER.validate_json(json.dumps(payload))


def test_a_well_formed_message_still_validates() -> None:
    """TC-BE-080: the rejections above are not simply refusing everything."""
    message = CLIENT_MESSAGE_ADAPTER.validate_json(
        '{"type": "speech.end", "duration_ms": 420, "client_ts": 1.5}'
    )

    assert isinstance(message, SpeechEndMsg)
    assert message.duration_ms == 420
    assert message.client_ts == 1.5


# --------------------------------------------------------------------------- #
# TC-BE-081 -- the typed-question length cap
# --------------------------------------------------------------------------- #


def test_a_question_at_the_cap_is_accepted_and_one_past_it_is_not() -> None:
    """TC-BE-081: TR-142 -- text.input holds 1 to 500 characters, inclusive."""
    at_cap = TextInputMsg.model_validate({"type": "text.input", "text": "x" * MAX_TEXT_INPUT_CHARS})

    assert len(at_cap.text) == MAX_TEXT_INPUT_CHARS

    with pytest.raises(ValidationError) as too_long:
        TextInputMsg.model_validate(
            {"type": "text.input", "text": "x" * (MAX_TEXT_INPUT_CHARS + 1)}
        )

    assert "at most 500 characters" in str(too_long.value)

    # Empty is refused at the other end: there is no question to answer.
    with pytest.raises(ValidationError):
        TextInputMsg.model_validate({"type": "text.input", "text": ""})


def test_an_overlong_question_reaches_the_client_as_bad_message(isolated_env: Any) -> None:
    """TC-BE-081: TR-142 -- 501 characters is refused over the socket, and no turn runs."""
    llm = FakeLLM()

    with connect(llm) as harness:
        harness.send(type="text.input", text="x" * (MAX_TEXT_INPUT_CHARS + 1))
        error = harness.recv()
        quiet = harness.barrier()

        # The cap is the only thing that refused it: one character less works.
        harness.send(type="text.input", text="x" * MAX_TEXT_INPUT_CHARS)
        accepted = harness.recv()

    assert error["type"] == "error"
    assert error["code"] == "bad_message"
    assert "text.input.text" in error["message"]
    assert quiet == []
    assert accepted["type"] == "state"
    assert accepted["value"] == "thinking"


# --------------------------------------------------------------------------- #
# TC-BE-082 / TC-BE-185 / TC-BE-186 -- binary audio framing
# --------------------------------------------------------------------------- #


def test_the_audio_header_is_two_little_endian_uint32s() -> None:
    """TC-BE-082: §6.1 -- sentence_id 7 and seq 3 frame as 07 00 00 00 03 00 00 00."""
    frame = encode_audio_frame(7, 3, b"..")

    assert frame[:AUDIO_HEADER_BYTES] == bytes([7, 0, 0, 0, 3, 0, 0, 0])
    assert frame[AUDIO_HEADER_BYTES:] == b".."
    assert AUDIO_HEADER_BYTES == AUDIO_HEADER.size == 8
    # The frontend hard-codes the same 8 in `AUDIO_HEADER_BYTES`; the parity
    # test compares the two files.
    assert len(frame) == AUDIO_HEADER_BYTES + 2


@given(
    sentence_id=st.integers(min_value=0, max_value=UINT32_MAX),
    seq=st.integers(min_value=0, max_value=UINT32_MAX),
    pcm16=st.binary(max_size=MAX_PAYLOAD_BYTES),
)
@hypothesis_settings(max_examples=300, deadline=None)
def test_any_frame_survives_the_round_trip(sentence_id: int, seq: int, pcm16: bytes) -> None:
    """TC-BE-185: §6.1 -- decode(encode(x)) is x for every id, sequence, and payload."""
    decoded = decode_audio_frame(encode_audio_frame(sentence_id, seq, pcm16))

    assert decoded == (sentence_id, seq, pcm16)


@pytest.mark.parametrize("length", list(range(AUDIO_HEADER_BYTES)))
def test_a_frame_too_short_to_hold_a_header_is_refused(length: int) -> None:
    """TC-BE-186: §6.1 -- a truncated frame raises rather than decoding garbage."""
    with pytest.raises(ValueError, match="at least 8 bytes"):
        decode_audio_frame(b"\x00" * length)


def test_a_header_with_no_payload_decodes_to_empty_audio() -> None:
    """TC-BE-186: §6.1 -- exactly eight bytes is a valid, empty frame."""
    assert decode_audio_frame(encode_audio_frame(0, 0, b"")) == (0, 0, b"")


# --------------------------------------------------------------------------- #
# TC-BE-187 -- the exported sets match the models
# --------------------------------------------------------------------------- #


def test_the_exported_type_sets_match_the_message_unions() -> None:
    """TC-BE-187: TR-143 -- the parity test's Python half is derived, not hand-kept.

    ``CLIENT_MESSAGE_TYPES`` and ``SERVER_MESSAGE_TYPES`` are literal sets, so a
    new message model could be added without either of them noticing -- and the
    cross-language parity test would then pass while the two languages disagree.
    This closes that gap by comparing the sets against the unions themselves.
    """
    assert literal_types(ClientMessage) == CLIENT_MESSAGE_TYPES
    assert literal_types(ServerMessage) == SERVER_MESSAGE_TYPES
    assert len(CLIENT_MESSAGE_TYPES) == 9
    assert len(SERVER_MESSAGE_TYPES) == 10
    assert CLIENT_MESSAGE_TYPES.isdisjoint(SERVER_MESSAGE_TYPES)
    assert PROTOCOL_VERSION == 1
