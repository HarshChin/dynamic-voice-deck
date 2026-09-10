"""Cross-language protocol parity (TR-143).

``backend/app/protocol.py`` and ``frontend/src/protocol.ts`` describe the same
wire contract in two languages, and nothing at runtime notices when they stop
agreeing: a message type added on one side simply never arrives on the other,
and the failure shows up as silence in a demo rather than as an exception. This
module is the only thing that catches it, so it reads the TypeScript file from
disk and compares what it declares against the Python source of truth.

The TypeScript is parsed with a regular expression rather than a JavaScript
parser, which is a deliberate trade: the arrays it reads are plain lists of
string literals, and every helper here fails loudly -- naming the file, the
constant, and what it saw -- if the shape ever changes. A parser that quietly
returned an empty set would turn this file into a test that cannot fail, which
is worse than one that stops compiling.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
from app.config import REPO_ROOT
from app.protocol import (
    AUDIO_HEADER_BYTES,
    CLIENT_MESSAGE_TYPES,
    MAX_TEXT_INPUT_CHARS,
    PROTOCOL_VERSION,
    SERVER_MESSAGE_TYPES,
    ControlAction,
    ErrorCode,
    SessionMode,
    SessionState,
    ToolSource,
)

PROTOCOL_TS: Final[Path] = REPO_ROOT / "frontend" / "src" / "protocol.ts"
"""The TypeScript half of the contract."""

_ARRAY_RE: Final = r"export const {name}(?::[^=]+)? = \[(?P<body>[^\]]*)\]"
"""An exported array literal. Bodies hold no ``]``, so the class is safe."""

_NUMBER_RE: Final = r"export const {name}(?::[^=]+)? = (?P<value>-?\d+);"
"""An exported integer constant."""

_STRING_RE: Final = re.compile(r'"([^"]*)"')
"""One double-quoted literal; the TypeScript is Prettier-formatted, so quotes
are always double and never escaped inside these arrays."""

ARRAY_PUNCTUATION: Final = frozenset(", \n\r\t")
"""Everything an array body may contain besides its string literals."""


def read_protocol_ts() -> str:
    """Read the frontend protocol module.

    Returns:
        Its full text.

    Raises:
        AssertionError: If the file is missing or empty, which would otherwise
            make every comparison below vacuously true.
    """
    assert PROTOCOL_TS.is_file(), (
        f"{PROTOCOL_TS} does not exist; the parity test cannot compare what it cannot read"
    )
    source = PROTOCOL_TS.read_text(encoding="utf-8")
    assert source.strip(), f"{PROTOCOL_TS} is empty"
    return source


def string_array(source: str, name: str) -> tuple[str, ...]:
    """Extract the string literals of one exported TypeScript array.

    Args:
        source: The module's text.
        name: The exported constant to read.

    Returns:
        The literals, in the order they are written.

    Raises:
        AssertionError: If the constant is missing, holds no literal, or holds
            anything other than literals -- a spread, a computed value, or a
            comment. Any of those would mean this test is reading less than the
            file declares, and it must fail rather than compare a subset.
    """
    match = re.search(_ARRAY_RE.format(name=name), source)
    assert match is not None, (
        f"no `export const {name} = [...]` in {PROTOCOL_TS.name}; the parity test "
        f"reads that exact shape, so either restore it or teach this test the new one"
    )
    body = match.group("body")
    values = tuple(_STRING_RE.findall(body))
    leftovers = set(_STRING_RE.sub("", body)) - ARRAY_PUNCTUATION
    assert not leftovers, (
        f"{name} in {PROTOCOL_TS.name} contains {sorted(leftovers)} besides string "
        f"literals; this test would silently ignore whatever that is"
    )
    assert values, f"{name} in {PROTOCOL_TS.name} is empty"
    assert len(set(values)) == len(values), f"{name} repeats a value: {values}"
    return values


def number(source: str, name: str) -> int:
    """Extract one exported integer constant.

    Args:
        source: The module's text.
        name: The exported constant to read.

    Returns:
        Its value.

    Raises:
        AssertionError: If the constant is missing or is not an integer literal.
    """
    match = re.search(_NUMBER_RE.format(name=name), source)
    assert match is not None, f"no integer `export const {name}` in {PROTOCOL_TS.name}"
    return int(match.group("value"))


@pytest.fixture(scope="module")
def protocol_ts() -> str:
    """Return the frontend protocol module's text.

    Returns:
        The file contents, read once for the module.
    """
    return read_protocol_ts()


def test_the_two_languages_declare_the_same_message_types(protocol_ts: str) -> None:
    """TC-PAR-001: TR-143 -- the client and server type sets are equal in both files."""
    client = string_array(protocol_ts, "CLIENT_MESSAGE_TYPES")
    server = string_array(protocol_ts, "SERVER_MESSAGE_TYPES")

    assert frozenset(client) == CLIENT_MESSAGE_TYPES
    assert frozenset(server) == SERVER_MESSAGE_TYPES
    # Set equality above would tolerate a repeat; the helper rejects one, and
    # this pins the count so a merge that duplicates a line is caught too.
    assert len(client) == len(CLIENT_MESSAGE_TYPES)
    assert len(server) == len(SERVER_MESSAGE_TYPES)


def test_typescript_checks_its_own_arrays_against_its_own_unions(protocol_ts: str) -> None:
    """TC-PAR-001: TR-143 -- the `satisfies` clauses keep the frontend half honest.

    This test compares two *lists of strings*. What stops the TypeScript list
    from drifting away from the TypeScript interfaces is the compiler, via the
    ``satisfies`` clause on each array; without it the frontend could declare a
    type it has no message for and both halves of the parity check would still
    agree.
    """
    assert "] as const satisfies readonly ClientMessageType[];" in protocol_ts
    assert "] as const satisfies readonly ServerMessageType[];" in protocol_ts


def test_the_shared_constants_agree(protocol_ts: str) -> None:
    """TC-PAR-002: TR-143 -- version, header size, and the text cap match."""
    assert number(protocol_ts, "PROTOCOL_VERSION") == PROTOCOL_VERSION
    assert number(protocol_ts, "AUDIO_HEADER_BYTES") == AUDIO_HEADER_BYTES
    assert number(protocol_ts, "MAX_TEXT_INPUT_CHARS") == MAX_TEXT_INPUT_CHARS


@pytest.mark.parametrize(
    ("constant", "enum"),
    [
        ("SESSION_STATES", SessionState),
        ("ERROR_CODES", ErrorCode),
        ("SESSION_MODES", SessionMode),
        ("TOOL_SOURCES", ToolSource),
        ("CONTROL_ACTIONS", ControlAction),
    ],
)
def test_every_closed_value_set_agrees(
    protocol_ts: str, constant: str, enum: type[SessionState]
) -> None:
    """TC-PAR-003: TR-143 -- states, error codes, modes, sources, and actions match.

    A message type is not the only thing that can drift. An error code the
    frontend has never heard of renders as an unhandled chip; a session state it
    does not know leaves the orb blank. These sets are as much a part of the
    contract as the message names.
    """
    declared = string_array(protocol_ts, constant)

    assert list(declared) == [member.value for member in enum]
