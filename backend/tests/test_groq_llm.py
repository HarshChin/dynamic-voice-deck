"""Contract tests for :mod:`app.providers.groq_llm` against recorded SSE text.

No test here touches the network. Each one drives the real parser through an
:class:`httpx.MockTransport` whose body is a transcript of what Groq's
OpenAI-compatible endpoint actually sends: ``data:`` frames carrying chat
completion chunks, keep-alive comments, blank-line event separators, and a
final ``[DONE]``. That keeps the tests honest about the wire format while
staying fast and offline, which is what TRD §12.1 calls a contract test.

Three properties matter more than the rest, because a live demo depends on
them. A tool call must survive being split across fragments, since the deck
only moves once the arguments parse (TR-031). A rate limit must surface as a
*retryable* error carrying the upstream's own wait (TR-085). And cancelling
the consumer must close the HTTP response, because that is the entire
mechanism behind barge-in (TR-034).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any, Final

import httpx
import pytest
from app.config import Settings
from app.errors import ConfigError, ProviderError
from app.pipeline.tools import GO_TO_SLIDE, HIGHLIGHT_BULLET, build_tools
from app.providers import groq_llm
from app.providers.base import LLMDone, LLMEvent, Message, TokenDelta, ToolCallDelta, ToolSpec
from app.providers.groq_llm import (
    CONNECT_TIMEOUT_S,
    POOL_TIMEOUT_S,
    PROVIDER_NAME,
    READ_TIMEOUT_S,
    WRITE_TIMEOUT_S,
    GroqLLM,
)

API_KEY: Final = "gsk-test-key-not-real"  # A stand-in; it never leaves the process.
MODEL: Final = "openai/gpt-oss-120b"
BASE_URL: Final = "https://api.groq.test/openai/v1"
COMPLETIONS_URL: Final = f"{BASE_URL}/chat/completions"
TEMPERATURE: Final = 0.4
MAX_TOKENS: Final = 350
SLIDE_COUNT: Final = 6
KEEP_ALIVE_REQUESTS: Final = 3
"""Requests the reuse test makes; more than one is the whole point."""

LOOPBACK: Final = "127.0.0.1"
RESPONSE_HEAD: Final = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: text/event-stream\r\n"
    b"Transfer-Encoding: chunked\r\n"
    b"Connection: keep-alive\r\n"
    b"\r\n"
)
LAST_CHUNK: Final = b"0\r\n\r\n"
"""Zero-length chunk that ends a chunked body; reading it is what frees the connection."""

CONTENT_LENGTH_HEADER: Final = b"content-length"
HEADER_SEPARATOR: Final = b"\r\n\r\n"
SERVER_STOP_TIMEOUT_S: Final = 5.0
"""Bound on the loopback server's shutdown, so a stuck handler fails the test."""

MESSAGES: Final = [
    Message(role="system", content="You are presenting a six-slide deck."),
    Message(role="user", content="How does barge-in work?"),
]
"""A minimal but realistic history: the pipeline always sends system then user."""


# --- Recorded fixtures -------------------------------------------------------
#
# Each entry is the JSON of one `data:` frame, wrapped into a stream body by
# `_sse_body`. They are kept as recorded text rather than built from dicts, so
# that a change in the wire format shows up as a diff in the fixture.

TEXT_ONLY_FRAMES: Final = (
    '{"id":"chatcmpl-1","object":"chat.completion.chunk","model":"openai/gpt-oss-120b",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
    '{"choices":[{"index":0,"delta":{"content":"Barge-in "},"finish_reason":null}]}',
    '{"choices":[{"index":0,"delta":{"content":"cancels the turn."},"finish_reason":null}]}',
    '{"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
    # Groq closes with a usage-only frame that carries no choices at all.
    '{"choices":[],"usage":{"completion_tokens":7,"total_tokens":211}}',
)

FRAGMENTED_TOOL_CALL_FRAMES: Final = (
    '{"choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
    '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_9Xk2Rb",'
    '"type":"function","function":{"name":"go_to_slide","arguments":""}}]},'
    '"finish_reason":null}]}',
    '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
    '"function":{"arguments":"{\\"slide_"}}]},"finish_reason":null}]}',
    '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
    '"function":{"arguments":"index\\": 4, \\"rea"}}]},"finish_reason":null}]}',
    '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
    '"function":{"arguments":"son\\": \\"User asked about barge-in\\"}"}}]},'
    '"finish_reason":null}]}',
    '{"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
)

SINGLE_DELTA_TOOL_CALL_FRAMES: Final = (
    '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_Whole1",'
    '"type":"function","function":{"name":"highlight_bullet",'
    '"arguments":"{\\"bullet_index\\": 2}"}}]},"finish_reason":"tool_calls"}]}',
)

TOOL_CALL_WITHOUT_FINISH_REASON_FRAMES: Final = (
    '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_NoFinish",'
    '"type":"function","function":{"name":"go_to_slide",'
    '"arguments":"{\\"slide_index\\": 1, \\"reason\\": \\"Opening\\"}"}}]},'
    '"finish_reason":null}]}',
)

TWO_TOOL_CALLS_FRAMES: Final = (
    '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_A",'
    '"type":"function","function":{"name":"go_to_slide","arguments":"{\\"slide_index\\""}}]}}]}',
    '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":1,"id":"call_B",'
    '"type":"function","function":{"name":"highlight_bullet","arguments":"{\\"bullet"}}]}}]}',
    '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
    '"function":{"arguments":": 2, \\"reason\\": \\"Architecture\\"}"}}]}}]}',
    '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":1,'
    '"function":{"arguments":"_index\\": 0}"}}]}}]}',
    '{"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
)

UNINDEXED_TOOL_CALL_FRAMES: Final = (
    # The opening fragment names the call, and every fragment after it carries
    # neither `index` nor `id` -- only more argument text. Nothing but arrival
    # order says which call they belong to, which is the case the fallback key
    # exists for.
    '{"choices":[{"delta":{"tool_calls":[{"id":"call_NoIndex","type":"function",'
    '"function":{"name":"go_to_slide","arguments":"{\\"slide_"}}]}}]}',
    '{"choices":[{"delta":{"tool_calls":[{"function":{"arguments":"index\\": 3, \\"rea"}}]}}]}',
    '{"choices":[{"delta":{"tool_calls":[{"function":'
    '{"arguments":"son\\": \\"Latency numbers\\"}"}}]}}]}',
    '{"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
)

TWO_UNINDEXED_TOOL_CALLS_FRAMES: Final = (
    # Two calls in a row, neither carrying an index or an id: the first is
    # complete by its second fragment, so the third fragment starts a new call
    # rather than appending to a call that is already on its way to the deck.
    '{"choices":[{"delta":{"tool_calls":[{"function":'
    '{"name":"go_to_slide","arguments":"{\\"slide_index\\": 2, "}}]}}]}',
    '{"choices":[{"delta":{"tool_calls":[{"function":'
    '{"arguments":"\\"reason\\": \\"Architecture\\"}"}}]}}]}',
    '{"choices":[{"delta":{"tool_calls":[{"function":'
    '{"name":"highlight_bullet","arguments":"{\\"bullet_index\\": 1}"}}]}}]}',
    '{"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
)

EMPTY_ARGUMENTS_TOOL_CALL_FRAMES: Final = (
    '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_Bare",'
    '"type":"function","function":{"name":"go_to_slide","arguments":""}}]}}]}',
    '{"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
)

PROTOCOL_NOISE: Final = 'event: message\nid: 42\ndata: {"choices":[]}\n\n'
"""An event carrying SSE fields other than ``data``, which must be ignored."""

MALFORMED_FRAMES: Final = (
    # A payload that is not an object at all.
    '["unexpected"]',
    # `choices` is not a list, a choice is not an object, a delta is not one.
    '{"choices":{"index":0}}',
    '{"choices":["nonsense"]}',
    '{"choices":[{"index":0,"delta":"nonsense"}]}',
    '{"choices":[{"index":0,"delta":{"tool_calls":["nonsense"]}}]}',
    # A tool call with neither index nor id whose arguments never complete.
    '{"choices":[{"index":0,"delta":{"tool_calls":[{"function":'
    '{"name":"go_to_slide","arguments":"{\\"slide_index\\":"}}]}}]}',
    # A tool call known only by id, whose function never arrives, so it is never named.
    '{"choices":[{"index":0,"delta":{"tool_calls":[{"id":"call_Nameless",'
    '"function":"nonsense"}]}}]}',
    # Arguments that parse but are not an object.
    '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":9,"id":"call_Array",'
    '"type":"function","function":{"name":"go_to_slide","arguments":"[1, 2]"}}]}}]}',
)
"""Frames no well-behaved provider sends. Every one of them must be survivable:
a dropped turn is a far worse answer to a malformed frame than a missing token.
"""

RATE_LIMIT_BODY: Final = json.dumps(
    {
        "error": {
            "message": "Rate limit reached for model `openai/gpt-oss-120b`.",
            "type": "rate_limit_exceeded",
        }
    }
)


def _sse_body(
    frames: Sequence[str],
    *,
    done: bool = True,
    keep_alive: bool = True,
    terminated: bool = True,
    preamble: str = "",
    terminator: str = "\n",
) -> bytes:
    """Wrap recorded JSON frames into a Server-Sent Events body.

    Args:
        frames: JSON text of each event, in order.
        done: Whether to append the ``[DONE]`` sentinel, as a real stream does.
        keep_alive: Whether to open with the comment line proxies send to hold
            the connection open, which the parser must ignore.
        terminated: Whether the last event ends with its blank-line separator.
            ``False`` reproduces a connection that dropped mid-event.
        preamble: Raw SSE text inserted before the frames.
        terminator: Line terminator to use. SSE allows CR, LF, and CRLF, and
            servers behind a proxy do not all pick the same one.

    Returns:
        The encoded response body.
    """
    parts = [f": keep-alive{terminator}{terminator}"] if keep_alive else []
    if preamble:
        parts.append(preamble)
    parts.extend(f"data: {frame}{terminator}{terminator}" for frame in frames)
    if done:
        parts.append(f"data: [DONE]{terminator}{terminator}")
    if not terminated and parts:
        parts[-1] = parts[-1].rstrip("\r\n")
    return "".join(parts).encode("utf-8")


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """Build a client whose transport is the given handler.

    Args:
        handler: Called with each request; returns the response to stream back.

    Returns:
        A client that never opens a socket.
    """
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _make_llm(client: httpx.AsyncClient | None = None) -> GroqLLM:
    """Build the provider under test, configured as the registry configures it.

    Args:
        client: Transport-backed client to inject, or ``None`` to let the
            provider build and own its own.

    Returns:
        The provider.
    """
    return GroqLLM(
        api_key=API_KEY,
        model=MODEL,
        base_url=BASE_URL,
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        client=client,
    )


async def _collect(
    frames: Sequence[str],
    *,
    done: bool = True,
    terminated: bool = True,
    preamble: str = "",
    terminator: str = "\n",
    tools: list[ToolSpec] | None = None,
) -> list[LLMEvent]:
    """Stream a recorded body through the provider and collect every event.

    Args:
        frames: JSON text of each recorded event.
        done: Whether the recording ends with ``[DONE]``.
        terminated: Whether the last event is followed by its blank line.
        preamble: Raw SSE text inserted before the frames.
        terminator: Line terminator the recorded server uses.
        tools: Tools to offer; the deck's real tools by default.

    Returns:
        Every event the provider yielded, in order.
    """
    body = _sse_body(
        frames,
        done=done,
        terminated=terminated,
        preamble=preamble,
        terminator=terminator,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    async with _make_client(handler) as client:
        llm = _make_llm(client)
        return [event async for event in llm.stream(MESSAGES, tools or build_tools(SLIDE_COUNT))]


async def _collect_from_pieces(pieces: Sequence[bytes]) -> list[LLMEvent]:
    """Stream a body delivered as exactly these network reads.

    Args:
        pieces: The chunks the parser should see, in order. Where a piece ends
            is where a real connection's read boundary would fall.

    Returns:
        Every event the provider yielded, in order.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_ChunkedStream(pieces),
        )

    async with _make_client(handler) as client:
        llm = _make_llm(client)
        return [event async for event in llm.stream(MESSAGES, [])]


class _RecordingResponse(httpx.Response):
    """Response that counts how many times it was closed.

    Attributes:
        aclose_calls: Number of completed ``aclose()`` awaits.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.aclose_calls = 0

    async def aclose(self) -> None:
        """Close the response, recording that it happened."""
        await super().aclose()
        self.aclose_calls += 1


class _ChunkedStream(httpx.AsyncByteStream):
    """Byte stream that hands over exactly the given pieces, in order.

    A body passed as ``content=`` arrives in one piece, which hides every bug
    that lives at a read boundary -- a CRLF split down the middle above all.
    """

    def __init__(self, pieces: Sequence[bytes]) -> None:
        self._pieces = list(pieces)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield each recorded piece.

        Yields:
            One piece per read the parser should see.
        """
        for piece in self._pieces:
            yield piece


class _KeepAliveServer:
    """Loopback HTTP/1.1 server that streams SSE and counts its connections.

    :class:`httpx.MockTransport` never opens a socket, so it cannot show
    whether a finished response went back to the keep-alive pool or took its
    connection down with it. This can: it answers every request on the
    connection that request arrived on, and remembers how many connections it
    had to accept.

    Attributes:
        connections: TCP connections accepted since the server started.
    """

    def __init__(self, body: bytes) -> None:
        self._body = body
        self._server: asyncio.Server | None = None
        self.connections = 0

    async def __aenter__(self) -> _KeepAliveServer:
        """Start listening on an ephemeral loopback port.

        Returns:
            This server, ready to serve.
        """
        self._server = await asyncio.start_server(self._serve, LOOPBACK, 0)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Stop listening and wait for every handler to finish.

        Args:
            exc_info: Exception details from the ``async with`` block, unused.
        """
        assert self._server is not None
        self._server.close()
        async with asyncio.timeout(SERVER_STOP_TIMEOUT_S):
            await self._server.wait_closed()

    @property
    def base_url(self) -> str:
        """Return the OpenAI-compatible base URL of this server.

        Returns:
            A URL the provider can be configured with.
        """
        assert self._server is not None
        port = self._server.sockets[0].getsockname()[1]
        return f"http://{LOOPBACK}:{port}/openai/v1"

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Answer every request that arrives on one connection.

        Args:
            reader: Incoming half of the connection.
            writer: Outgoing half of the connection.
        """
        self.connections += 1
        try:
            while await self._read_request(reader):
                writer.write(RESPONSE_HEAD)
                # One HTTP chunk per SSE event, as a streaming API sends them.
                for line in self._body.splitlines(keepends=True):
                    writer.write(f"{len(line):x}\r\n".encode() + line + b"\r\n")
                writer.write(LAST_CHUNK)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):  # pragma: no cover - client hung up
            pass
        finally:
            writer.close()

    @staticmethod
    async def _read_request(reader: asyncio.StreamReader) -> bool:
        """Consume one whole request so the next one on this connection parses.

        Args:
            reader: Incoming half of the connection.

        Returns:
            ``True`` when a request was read, ``False`` at end of connection.
        """
        try:
            head = await reader.readuntil(HEADER_SEPARATOR)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return False
        length = 0
        for line in head.split(b"\r\n"):
            name, _, value = line.partition(b":")
            if name.lower() == CONTENT_LENGTH_HEADER:
                length = int(value)
        if length:
            await reader.readexactly(length)
        return True


class _StallingStream(httpx.AsyncByteStream):
    """Byte stream that delivers one chunk and then never delivers another.

    It stands in for a model that is still generating: the consumer is left
    suspended inside the HTTP read, which is exactly where barge-in cancels a
    real turn.
    """

    def __init__(self, first_chunk: bytes) -> None:
        self._first_chunk = first_chunk

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield the first chunk, then stall until cancelled.

        Yields:
            The one chunk this stream ever produces.
        """
        yield self._first_chunk
        await asyncio.Event().wait()


# --- Streaming ---------------------------------------------------------------


async def test_fragmented_tool_call_arrives_as_one_parsed_delta() -> None:
    """TC-BE-070: argument fragments reassemble into a single ToolCallDelta."""
    events = await _collect(FRAGMENTED_TOOL_CALL_FRAMES)

    tool_calls = [event for event in events if isinstance(event, ToolCallDelta)]
    assert len(tool_calls) == 1
    assert tool_calls[0].call_id == "call_9Xk2Rb"
    assert tool_calls[0].name == GO_TO_SLIDE
    assert tool_calls[0].arguments == {"slide_index": 4, "reason": "User asked about barge-in"}
    assert isinstance(events[-1], LLMDone)
    assert events[-1].finish_reason == "tool_calls"


async def test_text_stream_yields_tokens_then_one_done() -> None:
    """TC-BE-071: content frames become TokenDeltas, then a single LLMDone(stop).

    The recording also carries everything a connection can put around those
    frames -- keep-alive comments, non-``data`` fields, a usage-only frame, a
    truncated frame, and objects of the wrong shape entirely. None of it may
    cost the user their answer, so the assertion is that the tokens and the
    single terminating event come through unchanged.
    """
    truncated = '{"choices":[{"index":0,"delta":{"content":'
    events = await _collect(
        (*TEXT_ONLY_FRAMES, *MALFORMED_FRAMES, truncated),
        preamble=PROTOCOL_NOISE,
    )

    tokens = [event for event in events if isinstance(event, TokenDelta)]
    assert "".join(token.text for token in tokens) == "Barge-in cancels the turn."
    assert [type(event) for event in events] == [TokenDelta, TokenDelta, LLMDone]
    assert isinstance(events[-1], LLMDone)
    assert events[-1].finish_reason == "stop"


@pytest.mark.parametrize(
    ("frames", "expected"),
    [
        pytest.param(SINGLE_DELTA_TOOL_CALL_FRAMES, {"bullet_index": 2}, id="whole-call-one-delta"),
        pytest.param(EMPTY_ARGUMENTS_TOOL_CALL_FRAMES, {}, id="empty-arguments-flushed"),
    ],
)
async def test_unfragmented_tool_calls_are_emitted_once(
    frames: tuple[str, ...],
    expected: dict[str, object],
) -> None:
    """TC-BE-151: a call sent whole, or with empty arguments, still emits once.

    Providers differ: some send the complete call in one delta. A call to a tool
    with no arguments streams ``""``, which cannot be recognised as complete
    while more fragments may still arrive, so it becomes ``{}`` at end of stream.
    """
    events = await _collect(frames)

    tool_calls = [event for event in events if isinstance(event, ToolCallDelta)]
    assert len(tool_calls) == 1
    assert tool_calls[0].arguments == expected
    assert isinstance(events[-1], LLMDone)
    assert events[-1].finish_reason == "tool_calls"


async def test_two_interleaved_tool_calls_keep_their_order() -> None:
    """TC-BE-152: fragments of two calls are separated by index and kept in order.

    TR-032 applies tool calls in the order the model made them, so the call that
    completes second must still be delivered second.
    """
    events = await _collect(TWO_TOOL_CALLS_FRAMES)

    tool_calls = [event for event in events if isinstance(event, ToolCallDelta)]
    assert [call.name for call in tool_calls] == [GO_TO_SLIDE, HIGHLIGHT_BULLET]
    assert [call.call_id for call in tool_calls] == ["call_A", "call_B"]
    assert tool_calls[0].arguments == {"slide_index": 2, "reason": "Architecture"}
    assert tool_calls[1].arguments == {"bullet_index": 0}


async def test_a_tool_call_without_an_index_survives_being_fragmented() -> None:
    """TC-BE-165: fragments carrying neither index nor id continue the open call.

    Providers that stream one call at a time need not repeat its identity on
    every fragment, and then arrival order is all there is. Arrival order has to
    mean *the call being assembled*: keyed by the number of calls seen so far, a
    three-fragment call became three accumulators holding a third of the
    argument JSON each, none of them ever parsed, and the deck sat still while
    the model believed it had moved the slide.
    """
    events = await _collect(UNINDEXED_TOOL_CALL_FRAMES)

    tool_calls = [event for event in events if isinstance(event, ToolCallDelta)]
    assert len(tool_calls) == 1
    assert tool_calls[0].call_id == "call_NoIndex"
    assert tool_calls[0].name == GO_TO_SLIDE
    assert tool_calls[0].arguments == {"slide_index": 3, "reason": "Latency numbers"}
    assert isinstance(events[-1], LLMDone)
    assert events[-1].finish_reason == "tool_calls"


async def test_a_completed_call_does_not_swallow_the_next_unindexed_one() -> None:
    """TC-BE-166: once a call is emitted, the next unidentified fragment starts a new one.

    The counterpart to TC-BE-165: continuing the open call must not become
    "append everything to the first call", or a model that asks for two slides
    in one turn would only ever get the first.
    """
    events = await _collect(TWO_UNINDEXED_TOOL_CALLS_FRAMES)

    tool_calls = [event for event in events if isinstance(event, ToolCallDelta)]
    assert [call.name for call in tool_calls] == [GO_TO_SLIDE, HIGHLIGHT_BULLET]
    assert tool_calls[0].arguments == {"slide_index": 2, "reason": "Architecture"}
    assert tool_calls[1].arguments == {"bullet_index": 1}
    # No id was streamed, so the ids are synthetic -- and must still differ, or
    # the two tool results would answer the same call.
    assert tool_calls[0].call_id != tool_calls[1].call_id


@pytest.mark.parametrize(
    "separator",
    [
        pytest.param("\u2028", id="line-separator"),
        pytest.param("\u2029", id="paragraph-separator"),
        pytest.param("\u0085", id="next-line"),
    ],
)
async def test_a_unicode_line_break_inside_a_frame_does_not_split_it(separator: str) -> None:
    """TC-BE-163: only CR, LF, and CRLF end an SSE line.

    ``str.splitlines`` -- and therefore ``httpx.Response.aiter_lines`` -- ends a
    line on six characters besides those, so a raw U+2028 inside a token used to
    tear the frame in two, leaving both halves unparsable and the token lost
    entirely. Models do emit exotic characters unprompted: the run recorded in
    docs/EVALS.md produced 221 characters of zero-width space in a row.

    Only the three separators above are exercised: the other three
    ``splitlines`` characters are ASCII control codes, which a JSON encoder
    escapes before they ever reach the wire.
    """
    text = f"before{separator}after"
    # ensure_ascii=False: the character has to reach the wire raw, which is
    # the whole point; escaped as a JSON \u sequence it would be harmless.
    frame = json.dumps(
        {"choices": [{"index": 0, "delta": {"content": text}}]},
        ensure_ascii=False,
    )

    events = await _collect((frame,))

    tokens = [event for event in events if isinstance(event, TokenDelta)]
    assert [token.text for token in tokens] == [text]


@pytest.mark.parametrize(
    "terminator",
    [pytest.param("\n", id="lf"), pytest.param("\r\n", id="crlf"), pytest.param("\r", id="cr")],
)
async def test_all_three_sse_line_terminators_are_understood(terminator: str) -> None:
    """TC-BE-164: CR, LF, and CRLF all end a line, whichever the server picked."""
    events = await _collect(TEXT_ONLY_FRAMES, terminator=terminator)

    tokens = [event for event in events if isinstance(event, TokenDelta)]
    assert "".join(token.text for token in tokens) == "Barge-in cancels the turn."
    assert isinstance(events[-1], LLMDone)
    assert events[-1].finish_reason == "stop"


async def test_a_crlf_split_across_two_reads_is_one_terminator() -> None:
    """TC-BE-164: a CR ending a network read is held back until its LF arrives.

    Treating that CR as a terminator the moment it lands makes the LF that
    follows it an empty line, and an empty line ends an SSE event. The event
    below carries its payload over two ``data:`` lines, which the spec allows
    and the parser joins with a newline, so ending it early leaves two
    unparsable halves and the token never reaches the user.
    """
    pieces = [
        b'data: {"choices":[{"index":0,"delta":\r',
        b'\ndata: {"content":"Barge-in cancels the turn."}}]}\r\n\r\n',
        b"data: [DONE]\r\n\r\n",
    ]

    events = await _collect_from_pieces(pieces)

    tokens = [event for event in events if isinstance(event, TokenDelta)]
    assert [token.text for token in tokens] == ["Barge-in cancels the turn."]
    assert [type(event) for event in events] == [TokenDelta, LLMDone]


@pytest.mark.parametrize(
    ("frames", "done", "terminated", "expected"),
    [
        pytest.param(
            ('{"choices":[{"index":0,"delta":{"content":"Hi."}}]}',),
            True,
            True,
            "stop",
            id="no-finish-reason-but-done",
        ),
        pytest.param(
            ('{"choices":[{"index":0,"delta":{"content":"Hi."},"finish_reason":"length"}]}',),
            True,
            True,
            "length",
            id="truncated-by-max-tokens",
        ),
        pytest.param(
            (
                '{"choices":[{"index":0,"delta":{"content":"Hi."},'
                '"finish_reason":"content_filter"}]}',
            ),
            True,
            True,
            "stop",
            id="unknown-reason-falls-back",
        ),
        pytest.param(
            TOOL_CALL_WITHOUT_FINISH_REASON_FRAMES,
            False,
            False,
            "tool_calls",
            id="connection-dropped-after-a-tool-call",
        ),
    ],
)
async def test_finish_reason_is_mapped_onto_the_protocol_vocabulary(
    frames: tuple[str, ...],
    done: bool,
    terminated: bool,
    expected: str,
) -> None:
    """TC-BE-153: absent, unknown, and truncated endings all resolve sensibly.

    A stream that ends without ``[DONE]`` -- a dropped connection -- still has
    to produce exactly one LLMDone, or the session would wait for an event that
    is never coming.
    """
    events = await _collect(frames, done=done, terminated=terminated)

    assert isinstance(events[-1], LLMDone)
    assert events[-1].finish_reason == expected
    assert sum(isinstance(event, LLMDone) for event in events) == 1


# --- Connection lifetime -----------------------------------------------------


async def test_successive_requests_share_one_tcp_connection() -> None:
    """TC-BE-162: the body is finished past ``[DONE]``, so the connection is pooled.

    httpcore recycles a connection only once its response body has been read to
    the end. Returning at the sentinel left the terminating chunk unread, so
    every request opened a socket and paid a fresh TCP and TLS handshake --
    twice per turn, on the number this project is worst at already (TRD §8.1).

    A loopback server rather than a mock transport, because a mock never opens a
    socket and so cannot tell reuse from a new connection.
    """
    async with _KeepAliveServer(_sse_body(TEXT_ONLY_FRAMES)) as server:
        llm = GroqLLM(
            api_key=API_KEY,
            model=MODEL,
            base_url=server.base_url,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
        )
        try:
            for _ in range(KEEP_ALIVE_REQUESTS):
                events = [event async for event in llm.stream(MESSAGES, [])]
                assert [type(event) for event in events] == [TokenDelta, TokenDelta, LLMDone]
        finally:
            await llm.aclose()

    assert server.connections == 1


async def test_frames_arriving_after_done_are_read_and_discarded() -> None:
    """TC-BE-167: the drain reads what follows ``[DONE]`` without acting on it.

    Something has to be read for the connection to be freed, and a proxy may
    well append a keep-alive comment or a stray blank line after the sentinel.
    None of it belongs to the answer, which ``[DONE]`` already ended.
    """
    pieces = [
        _sse_body(TEXT_ONLY_FRAMES),
        b': keep-alive\n\ndata: {"choices":[{"index":0,"delta":{"content":"ignored"}}]}\n\n',
    ]

    events = await _collect_from_pieces(pieces)

    assert [type(event) for event in events] == [TokenDelta, TokenDelta, LLMDone]
    assert all("ignored" not in event.text for event in events if isinstance(event, TokenDelta))


async def test_a_body_that_never_ends_does_not_hold_up_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-BE-167: draining is best-effort; the answer is already complete.

    A server that sends ``[DONE]`` and then keeps its body open would otherwise
    hold the turn until the read timeout. The bound is short by design: the cost
    of giving up is one extra handshake on the next turn.
    """
    monkeypatch.setattr(groq_llm, "DRAIN_TIMEOUT_S", 0.05)
    whole_body = _sse_body(TEXT_ONLY_FRAMES)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_StallingStream(whole_body),
        )

    async with _make_client(handler) as client:
        llm = _make_llm(client)
        async with asyncio.timeout(1.0):
            events = [event async for event in llm.stream(MESSAGES, [])]

    assert [type(event) for event in events] == [TokenDelta, TokenDelta, LLMDone]


async def test_the_owned_client_bounds_every_phase_of_a_request() -> None:
    """TC-BE-168: a hung upstream fails on its own, not only on the turn watchdog.

    The read budget is deliberately the loosest and deliberately longer than the
    20 s turn watchdog (``TURN_TIMEOUT_S``): the watchdog should be what
    abandons a slow answer, so the user sees one timeout rather than a race
    between two. The other three phases have nothing to wait for and are short.
    """
    llm = _make_llm()
    try:
        timeout = llm._client.timeout
    finally:
        await llm.aclose()

    assert timeout.connect == CONNECT_TIMEOUT_S
    assert timeout.read == READ_TIMEOUT_S
    assert timeout.write == WRITE_TIMEOUT_S
    assert timeout.pool == POOL_TIMEOUT_S
    assert CONNECT_TIMEOUT_S < Settings().turn_timeout_s < READ_TIMEOUT_S


# --- Request shape -----------------------------------------------------------


async def test_request_carries_the_configured_generation_settings() -> None:
    """TC-BE-150: the request matches TR-082 and offers the deck's tools."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_body(TEXT_ONLY_FRAMES),
        )

    async with _make_client(handler) as client:
        llm = _make_llm(client)
        async for _ in llm.stream(MESSAGES, build_tools(SLIDE_COUNT)):
            pass
        async for _ in llm.stream(MESSAGES, []):
            pass

    with_tools = json.loads(requests[0].content)
    assert requests[0].method == "POST"
    assert str(requests[0].url) == COMPLETIONS_URL
    assert requests[0].headers["authorization"] == f"Bearer {API_KEY}"
    assert with_tools["model"] == MODEL
    assert with_tools["temperature"] == TEMPERATURE
    assert with_tools["max_tokens"] == MAX_TOKENS
    assert with_tools["stream"] is True
    assert with_tools["tool_choice"] == "auto"
    assert [tool["function"]["name"] for tool in with_tools["tools"]] == [
        GO_TO_SLIDE,
        HIGHLIGHT_BULLET,
    ]
    # Null-valued keys are dropped: a strict server rejects `tool_call_id: null`.
    assert with_tools["messages"] == [
        {"role": "system", "content": MESSAGES[0].content},
        {"role": "user", "content": MESSAGES[1].content},
    ]

    # `tool_choice` without `tools` is rejected upstream, so neither is sent.
    without_tools = json.loads(requests[1].content)
    assert "tools" not in without_tools
    assert "tool_choice" not in without_tools


async def test_construction_requires_a_key_and_aclose_respects_ownership() -> None:
    """TC-BE-155: a blank key fails with an actionable message; aclose owns only its own.

    The ownership rule matters because the session shares one provider: closing
    a client that was injected -- in tests, or by a future shared pool -- would
    break every other user of it.
    """
    for blank in ("", "   "):
        with pytest.raises(ConfigError) as raised:
            GroqLLM(
                api_key=blank,
                model=MODEL,
                base_url=BASE_URL,
                temperature=TEMPERATURE,
                max_tokens=MAX_TOKENS,
            )
        assert "GROQ_API_KEY" in str(raised.value)
        assert ".env.example" in str(raised.value)

    owned = _make_llm()
    await owned.aclose()
    assert owned._client.is_closed is True

    injected_client = _make_client(lambda request: httpx.Response(200))
    await _make_llm(injected_client).aclose()
    assert injected_client.is_closed is False
    await injected_client.aclose()


# --- Failures ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "headers", "retryable", "retry_after"),
    [
        pytest.param(429, {"retry-after": "3"}, True, 3.0, id="rate-limited"),
        pytest.param(429, {"retry-after": "in a while"}, True, None, id="unparsable-retry-after"),
        pytest.param(500, {}, True, None, id="upstream-fault"),
        pytest.param(401, {}, False, None, id="bad-credential"),
    ],
)
async def test_error_status_becomes_a_provider_error(
    status: int,
    headers: dict[str, str],
    retryable: bool,
    retry_after: float | None,
) -> None:
    """TC-BE-072: 429 raises a retryable ProviderError carrying its retry-after.

    5xx is retryable too and 4xx is not: the only sensible reaction to a rate
    limit is to wait and try again, while retrying a rejected key would just
    spend another request to be told the same thing.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, content=RATE_LIMIT_BODY.encode())

    async with _make_client(handler) as client:
        llm = _make_llm(client)
        with pytest.raises(ProviderError) as raised:
            async for _ in llm.stream(MESSAGES, build_tools(SLIDE_COUNT)):
                pass

    assert raised.value.provider == PROVIDER_NAME
    assert raised.value.retryable is retryable
    assert raised.value.retry_after == retry_after
    assert str(status) in raised.value.message


@pytest.mark.parametrize(
    ("exception", "retryable"),
    [
        pytest.param(httpx.ConnectError("connection refused"), True, id="network"),
        pytest.param(httpx.ReadTimeout("timed out"), True, id="timeout"),
        pytest.param(httpx.RemoteProtocolError("truncated chunk"), False, id="protocol"),
    ],
)
async def test_transport_failures_are_wrapped_not_leaked(
    exception: httpx.HTTPError,
    retryable: bool,
) -> None:
    """TC-BE-154: no httpx exception escapes the provider boundary (TR-085)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise exception

    async with _make_client(handler) as client:
        llm = _make_llm(client)
        with pytest.raises(ProviderError) as raised:
            async for _ in llm.stream(MESSAGES, build_tools(SLIDE_COUNT)):
                pass

    assert raised.value.provider == PROVIDER_NAME
    assert raised.value.retryable is retryable
    assert type(exception).__name__ in raised.value.message


@pytest.mark.parametrize(
    "error_frame",
    [
        pytest.param(
            '{"error":{"message":"internal server error","type":"internal"}}',
            id="error-object",
        ),
        pytest.param('{"error":"internal server error"}', id="bare-string-error"),
    ],
)
async def test_mid_stream_error_frame_becomes_a_provider_error(error_frame: str) -> None:
    """TC-BE-156: an error frame sent after a 200 OK still fails the turn.

    The status line is long gone by the time this arrives, so the only thing
    standing between the user and a silently truncated answer is the parser
    noticing the ``error`` key.
    """
    frames = ('{"choices":[{"index":0,"delta":{"content":"Barge-in "}}]}', error_frame)

    with pytest.raises(ProviderError) as raised:
        await _collect(frames)

    assert "internal server error" in raised.value.message
    assert raised.value.retryable is False


async def test_cancelling_the_consumer_closes_the_response() -> None:
    """TC-BE-073: cancellation closes the HTTP response before it propagates.

    This is the whole mechanism behind barge-in (TR-034): the turn task is
    cancelled, and generation must actually stop rather than keep streaming
    into a socket nobody is reading.
    """
    responses: list[_RecordingResponse] = []
    first_frame = _sse_body(TEXT_ONLY_FRAMES[1:2], done=False, keep_alive=False)

    def handler(request: httpx.Request) -> httpx.Response:
        response = _RecordingResponse(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_StallingStream(first_frame),
        )
        responses.append(response)
        return response

    async with _make_client(handler) as client:
        llm = _make_llm(client)
        first_token = asyncio.Event()
        seen: list[LLMEvent] = []

        async def consume() -> None:
            async for event in llm.stream(MESSAGES, build_tools(SLIDE_COUNT)):
                seen.append(event)
                first_token.set()

        task = asyncio.create_task(consume())
        await asyncio.wait_for(first_token.wait(), timeout=1.0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert [type(event) for event in seen] == [TokenDelta]
    assert responses[0].aclose_calls == 1
