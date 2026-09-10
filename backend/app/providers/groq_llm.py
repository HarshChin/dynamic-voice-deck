"""Groq LLM provider: streaming chat completions over the OpenAI-compatible API.

The transport is :meth:`httpx.AsyncClient.stream`, deliberately *not* the
``groq`` SDK. Barge-in is implemented by cancelling the turn's asyncio task
(TR-034), and the only thing that makes that cancellation mean anything upstream
is the HTTP response closing at the same instant: an SDK that buffers, retries,
or owns its own background reader would keep generating tokens we have already
decided to throw away, burning free-tier quota and -- worse -- keeping the
socket warm long enough for the next turn to collide with it. Owning the request
here keeps the ``async with`` around the response the single place where
cleanup happens, so ``CancelledError`` unwinds through it and the connection is
released before the exception reaches the caller (TR-031).

The same parser serves any OpenAI-compatible endpoint, which is what TR-084's
Ollama provider will reuse.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal

import httpx

from ..errors import ConfigError, ProviderError
from ..logging_setup import get_logger
from .base import LLMDone, LLMEvent, Message, TokenDelta, ToolCallDelta, ToolChoice, ToolSpec

logger = get_logger(__name__)

PROVIDER_NAME: Final = "groq_llm"
"""Value of :attr:`GroqLLM.name` and of ``ProviderError.provider``."""

CHAT_COMPLETIONS_PATH: Final = "/chat/completions"
"""Path appended to ``GROQ_BASE_URL`` to reach the streaming endpoint."""

TOOL_CHOICE_AUTO: Final = "auto"
"""``tool_choice`` value required by TR-082: the model decides."""

TOOL_CHOICE_NONE: Final = "none"
"""``tool_choice`` value that forbids a call while still declaring the tools."""

SSE_DATA_FIELD: Final = "data"
"""The only Server-Sent Events field this parser consumes."""

SSE_COMMENT_PREFIX: Final = ":"
"""Prefix of an SSE comment line, used by some proxies as a keep-alive."""

SSE_DONE_SENTINEL: Final = "[DONE]"
"""Payload that terminates an OpenAI-compatible stream."""

SSE_CR: Final = "\r"
SSE_LF: Final = "\n"
"""The two characters SSE builds its three line terminators from: CR, LF, CRLF.

Named because :meth:`str.splitlines` -- and therefore
:meth:`httpx.Response.aiter_lines` -- recognises six more, which SSE does not.
"""

CRLF_LENGTH: Final = 2
"""Length of the two-character terminator, used when slicing a line off."""

SINGLE_TERMINATOR_LENGTH: Final = 1
"""Length of a lone CR or LF terminator."""

DRAIN_TIMEOUT_S: Final = 0.5
"""Longest to spend finishing a response body after ``[DONE]``.

Reading the body to its end is what lets the connection go back to the
keep-alive pool, and after ``[DONE]`` the only thing left is the terminating
chunk the server has already sent. The bound exists for the server that holds
the body open anyway: the answer is complete by then, so the cost of giving up
is one extra handshake next turn, while waiting out ``READ_TIMEOUT_S`` would
cost the user thirty seconds of silence.
"""

CONNECT_TIMEOUT_S: Final = 5.0
"""Give up on a TCP/TLS handshake well inside the 20 s turn watchdog."""

READ_TIMEOUT_S: Final = 30.0
"""Longest silence tolerated between two SSE chunks.

Deliberately longer than ``TURN_TIMEOUT_S`` (20 s): the turn watchdog should be
what abandons a stalled turn, so that the user sees one consistent timeout
rather than a race between two of them.
"""

WRITE_TIMEOUT_S: Final = 10.0
"""Longest a request body may take to send; the prompt is a few kilobytes."""

POOL_TIMEOUT_S: Final = 5.0
"""Longest to wait for a free connection from the pool."""

HTTP_ERROR_FLOOR: Final = int(httpx.codes.BAD_REQUEST)
"""First status code treated as a failure rather than a stream."""

TOO_MANY_REQUESTS: Final = int(httpx.codes.TOO_MANY_REQUESTS)
"""Rate-limit status; the only one carrying ``retry-after`` in practice."""

SERVER_ERROR_FLOOR: Final = int(httpx.codes.INTERNAL_SERVER_ERROR)
"""First status code that means "upstream problem", so retrying may help."""

ERROR_BODY_SNIPPET_CHARS: Final = 500
"""Cap on how much of a failed response body reaches the error message."""

RETRY_AFTER_SECONDS_SUFFIX: Final = "s"
"""Groq sometimes writes ``retry-after: 2s``; the RFC allows bare seconds."""


@dataclass(slots=True)
class _ToolCallAccumulator:
    """Partial tool call being reassembled from streamed fragments.

    Attributes:
        key: Stream-local identity of the call (``index`` when the provider
            sends one, otherwise its id or arrival order).
        call_id: Provider identifier echoed back in the ``tool`` reply.
        name: Function name; providers send it whole in the opening fragment.
        arguments: Concatenated argument fragments, valid JSON once complete.
        emitted: Whether a :class:`ToolCallDelta` has already been yielded.
    """

    key: int | str
    call_id: str = ""
    name: str = ""
    arguments: str = ""
    emitted: bool = False

    @property
    def public_id(self) -> str:
        """Return the id to send back to the model.

        Returns:
            The provider's own id, or a stable synthetic one for providers that
            stream a tool call without an id at all.
        """
        return self.call_id or f"{self.name}_{self.key}"


@dataclass(slots=True)
class _StreamState:
    """Everything one ``stream()`` call accumulates across SSE events.

    Attributes:
        calls: Tool calls being reassembled, keyed by stream-local identity and
            held in arrival order so multiple calls are applied in order
            (TR-032).
        finish_reason: Last ``finish_reason`` seen, before mapping.
        text_chars: Characters of content streamed, for the latency log.
        tool_calls_emitted: How many :class:`ToolCallDelta` were yielded.
    """

    calls: dict[int | str, _ToolCallAccumulator] = field(default_factory=dict)
    finish_reason: str | None = None
    text_chars: int = 0
    tool_calls_emitted: int = 0


if TYPE_CHECKING:  # pragma: no cover - type-check-time assertion, never executed
    from .base import LLMProvider

    def _satisfies_protocol(provider: GroqLLM) -> LLMProvider:
        """Assert at type-check time that :class:`GroqLLM` implements the protocol.

        Structural typing is only checked where a value is *used* as the
        protocol, and the registry hands providers to Pydantic's ``Any``-typed
        :class:`~app.providers.base.Providers`, which would hide a drifted
        signature until runtime. This function is never called; mypy reads it.

        Args:
            provider: The implementation to check.

        Returns:
            The same object, typed as the protocol it must satisfy.
        """
        return provider


class GroqLLM:
    """Stream assistant tokens and tool calls from Groq's chat completions API.

    The instance is stateless between calls, so one provider serves every
    session in the process; only the HTTP connection pool is shared.

    Args:
        api_key: Groq API key. Blank or missing is a configuration failure.
        model: Model identifier, from ``GROQ_LLM_MODEL``.
        base_url: OpenAI-compatible base URL, from ``GROQ_BASE_URL``.
        temperature: Sampling temperature, from ``LLM_TEMPERATURE``.
        max_tokens: Response cap, from ``LLM_MAX_TOKENS``.
        client: Pre-built HTTP client. Tests inject one carrying a
            :class:`httpx.MockTransport`; in production the provider builds and
            owns its own.

    Raises:
        ConfigError: If ``api_key`` is missing or blank.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        temperature: float,
        max_tokens: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            msg = (
                "GroqLLM needs a Groq API key. Set GROQ_API_KEY in .env "
                "(see .env.example) or select LLM_PROVIDER=fake."
            )
            raise ConfigError(msg)

        self.name = PROVIDER_NAME
        self._model = model
        self._url = f"{base_url.rstrip('/')}{CHAT_COMPLETIONS_PATH}"
        self._temperature = temperature
        self._max_tokens = max_tokens
        # Held per-request rather than on the client so an injected client never
        # carries the credential, and so a test transport can assert on it.
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        self._owns_client = client is None
        self._client = client if client is not None else _build_client()

    async def aclose(self) -> None:
        """Close the HTTP connection pool if this provider opened it.

        Safe to call more than once, and a no-op for an injected client, whose
        owner closes it.
        """
        if self._owns_client:
            await self._client.aclose()

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        tool_choice: ToolChoice = TOOL_CHOICE_AUTO,
    ) -> AsyncIterator[LLMEvent]:
        """Stream one assistant response.

        Args:
            messages: Conversation history, oldest first, starting with system.
            tool_choice: ``"auto"`` lets the model decide, ``"none"`` forbids a
                call while still declaring the tools.
            tools: Tools the model may call; omitted from the request entirely
                when empty, because ``tool_choice`` without ``tools`` is
                rejected by the API.

        Yields:
            A :class:`TokenDelta` per content fragment, a
            :class:`ToolCallDelta` per completed tool call, and exactly one
            terminating :class:`LLMDone`.

        Raises:
            ProviderError: If the request fails, the upstream returns an error
                status, or an error object arrives mid-stream.
        """
        payload = self._build_payload(messages, tools, tool_choice)
        state = _StreamState()
        started = time.perf_counter()
        logger.debug("llm.request", model=self._model, messages=len(messages), tools=len(tools))

        try:
            async with self._client.stream(
                "POST",
                self._url,
                json=payload,
                headers=self._headers,
            ) as response:
                await _raise_for_status(response, self._model)
                # aclosing() finalises the parser at the moment of cancellation
                # rather than whenever the garbage collector notices it, which
                # keeps barge-in cleanup deterministic.
                async with aclosing(_iter_sse_payloads(response)) as payloads:
                    async for raw in payloads:
                        for event in _events_from_payload(raw, state):
                            yield event
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise _wrap_transport_error(exc) from exc

        for event in _flush_tool_calls(state):
            yield event

        finish_reason = _resolve_finish_reason(state.finish_reason, state.tool_calls_emitted)
        logger.info(
            "llm.stream_done",
            model=self._model,
            finish_reason=finish_reason,
            chars=state.text_chars,
            tool_calls=state.tool_calls_emitted,
            ms=int((time.perf_counter() - started) * 1000),
        )
        yield LLMDone(finish_reason=finish_reason)

    def _build_payload(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        tool_choice: ToolChoice = TOOL_CHOICE_AUTO,
    ) -> dict[str, Any]:
        """Build the JSON body of a streaming chat completion request (TR-082).

        Args:
            messages: Conversation history in provider shape.
            tools: Tools to offer, possibly empty.
            tool_choice: Whether the model may call one.

        Returns:
            The request body.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            # exclude_none drops the tool_call_id / name / tool_calls keys that
            # only some roles carry; sending them as null upsets strict servers.
            "messages": [message.model_dump(exclude_none=True) for message in messages],
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = [tool.to_openai() for tool in tools]
            payload["tool_choice"] = tool_choice
        return payload


def _build_client() -> httpx.AsyncClient:
    """Build the default HTTP client for this provider.

    Returns:
        A client with per-phase timeouts, so a stalled connect fails fast while
        a slow generation is left to the turn watchdog.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=CONNECT_TIMEOUT_S,
            read=READ_TIMEOUT_S,
            write=WRITE_TIMEOUT_S,
            pool=POOL_TIMEOUT_S,
        )
    )


async def _raise_for_status(response: httpx.Response, model: str) -> None:
    """Convert a non-streaming error response into a :class:`ProviderError`.

    Args:
        response: The still-unread streaming response.
        model: Model identifier, included in the message for triage.

    Raises:
        ProviderError: If the status is 400 or above. ``retryable`` is set for
            429 and 5xx, and ``retry_after`` carries the header when the
            upstream sent one.
    """
    status = response.status_code
    if status < HTTP_ERROR_FLOOR:
        return

    # The body was never read (stream=True), so pull it before touching .text.
    await response.aread()
    body = response.text.strip()[:ERROR_BODY_SNIPPET_CHARS]
    retry_after = _parse_retry_after(response.headers.get("retry-after"))
    retryable = status == TOO_MANY_REQUESTS or status >= SERVER_ERROR_FLOOR
    logger.warning("llm.http_error", status=status, retryable=retryable, retry_after=retry_after)
    raise ProviderError(
        PROVIDER_NAME,
        f"HTTP {status} from {model}: {body or response.reason_phrase}",
        retryable=retryable,
        retry_after=retry_after,
    )


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``retry-after`` header expressed in seconds.

    Only the delta-seconds form is understood; the HTTP-date form is treated as
    absent, because acting on a clock-skewed date is worse than falling back to
    the caller's own backoff.

    Args:
        value: Raw header value, if the response carried one.

    Returns:
        Seconds to wait, or ``None`` when absent or not a number.
    """
    if value is None:
        return None
    candidate = value.strip().removesuffix(RETRY_AFTER_SECONDS_SUFFIX)
    try:
        return float(candidate)
    except ValueError:
        logger.warning("llm.retry_after_unparsed", value=value)
        return None


def _wrap_transport_error(exc: httpx.HTTPError | httpx.InvalidURL) -> ProviderError:
    """Wrap an httpx failure so no vendor exception escapes this package (TR-085).

    Args:
        exc: The httpx exception raised while opening or reading the stream.

    Returns:
        The equivalent :class:`ProviderError`; timeouts and network faults are
        marked retryable, everything else is not.
    """
    retryable = isinstance(exc, httpx.TimeoutException | httpx.NetworkError)
    logger.warning("llm.transport_error", error=type(exc).__name__, retryable=retryable)
    return ProviderError(
        PROVIDER_NAME,
        f"{type(exc).__name__}: {exc}",
        retryable=retryable,
    )


async def _iter_sse_payloads(response: httpx.Response) -> AsyncGenerator[str, None]:
    """Yield the payload of each Server-Sent Event, stopping at ``[DONE]``.

    Multi-line ``data:`` fields are joined with newlines as the SSE spec
    requires, and comment lines (``: keep-alive``) are dropped. Providers vary
    in whether they terminate the last event with a blank line, so a pending
    payload is flushed when the connection ends.

    Args:
        response: An open streaming response.

    Yields:
        One payload string per event, with the ``data:`` prefixes removed.
    """
    parts: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if parts:
                payload = "\n".join(parts)
                parts = []
                if payload == SSE_DONE_SENTINEL:
                    return
                yield payload
            continue
        if line.startswith(SSE_COMMENT_PREFIX):
            continue
        field_name, _, value = line.partition(":")
        if field_name != SSE_DATA_FIELD:
            continue
        parts.append(value.removeprefix(" "))

    if parts:
        payload = "\n".join(parts)
        if payload != SSE_DONE_SENTINEL:
            yield payload


def _events_from_payload(payload: str, state: _StreamState) -> list[LLMEvent]:
    """Turn one SSE payload into the events it completes.

    Args:
        payload: JSON text of a single event.
        state: Accumulator mutated as fragments arrive.

    Returns:
        Zero or more events; a fragment that completes nothing yields none.

    Raises:
        ProviderError: If the event is an upstream error object.
    """
    try:
        chunk: Any = json.loads(payload)
    except json.JSONDecodeError:
        # A malformed frame is not worth failing a turn over: log it and let the
        # rest of the stream through.
        logger.warning("llm.sse_unparsed", chars=len(payload))
        return []

    if not isinstance(chunk, Mapping):
        logger.warning("llm.sse_not_an_object", kind=type(chunk).__name__)
        return []

    error = chunk.get("error")
    if error is not None:
        raise ProviderError(PROVIDER_NAME, f"upstream error: {_error_message(error)}")

    return _events_from_choices(chunk.get("choices"), state)


def _error_message(error: Any) -> str:
    """Extract a human-readable message from an upstream error object.

    Args:
        error: The ``error`` value of an SSE payload, in any shape.

    Returns:
        The provider's message when there is one, else the object rendered.
    """
    if isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    return str(error)


def _events_from_choices(choices: Any, state: _StreamState) -> list[LLMEvent]:
    """Read content and tool-call fragments out of a chunk's ``choices``.

    Args:
        choices: The ``choices`` value, expected to be a list of objects.
        state: Accumulator mutated as fragments arrive.

    Returns:
        The events completed by this chunk, in stream order.
    """
    if not isinstance(choices, list):
        return []

    events: list[LLMEvent] = []
    for choice in choices:
        if not isinstance(choice, Mapping):
            continue
        finish_reason = choice.get("finish_reason")
        if isinstance(finish_reason, str) and finish_reason:
            state.finish_reason = finish_reason

        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            continue

        content = delta.get("content")
        if isinstance(content, str) and content:
            state.text_chars += len(content)
            events.append(TokenDelta(text=content))

        raw_calls = delta.get("tool_calls")
        if isinstance(raw_calls, list):
            events.extend(_accumulate_tool_calls(raw_calls, state))
    return events


def _accumulate_tool_calls(raw_calls: list[Any], state: _StreamState) -> list[ToolCallDelta]:
    """Fold tool-call fragments into the accumulator, emitting completed calls.

    Args:
        raw_calls: The ``tool_calls`` array of one delta.
        state: Accumulator mutated in place.

    Returns:
        The calls whose arguments became valid JSON with this fragment.
    """
    completed: list[ToolCallDelta] = []
    for raw in raw_calls:
        if not isinstance(raw, Mapping):
            continue
        accumulator = _accumulator_for(raw, state)
        _absorb_fragment(raw, accumulator)
        emitted = _try_emit(accumulator, state)
        if emitted is not None:
            completed.append(emitted)
    return completed


def _accumulator_for(raw: Mapping[str, Any], state: _StreamState) -> _ToolCallAccumulator:
    """Find or create the accumulator a fragment belongs to.

    Providers that stream several calls at once always send ``index``; those
    that send one call in a single delta sometimes send neither ``index`` nor a
    repeated ``id``, so arrival order is the last resort.

    Args:
        raw: One entry of a ``tool_calls`` array.
        state: Accumulator store, keyed by stream-local identity.

    Returns:
        The accumulator for this fragment.
    """
    index = raw.get("index")
    call_id = raw.get("id")
    key: int | str
    if isinstance(index, int):
        key = index
    elif isinstance(call_id, str) and call_id:
        key = call_id
    else:
        key = len(state.calls)

    accumulator = state.calls.get(key)
    if accumulator is None:
        accumulator = _ToolCallAccumulator(key=key)
        state.calls[key] = accumulator
    return accumulator


def _absorb_fragment(raw: Mapping[str, Any], accumulator: _ToolCallAccumulator) -> None:
    """Merge one fragment into the call being reassembled.

    The name is assigned rather than appended: every OpenAI-compatible provider
    sends it complete in the opening fragment, and appending would duplicate it
    for the providers that repeat it on later fragments.

    Args:
        raw: One entry of a ``tool_calls`` array.
        accumulator: The call being reassembled; mutated in place.
    """
    call_id = raw.get("id")
    if isinstance(call_id, str) and call_id:
        accumulator.call_id = call_id

    function = raw.get("function")
    if not isinstance(function, Mapping):
        return

    name = function.get("name")
    if isinstance(name, str) and name:
        accumulator.name = name

    arguments = function.get("arguments")
    if isinstance(arguments, str):
        accumulator.arguments += arguments


def _try_emit(accumulator: _ToolCallAccumulator, state: _StreamState) -> ToolCallDelta | None:
    """Emit a tool call once its accumulated arguments parse (TR-031).

    Args:
        accumulator: The call being reassembled.
        state: Stream state, whose emitted counter is bumped on success.

    Returns:
        The completed call, or ``None`` while it is still incomplete or already
        emitted.
    """
    if accumulator.emitted or not accumulator.name:
        return None
    arguments = _parse_arguments(accumulator.arguments)
    if arguments is None:
        return None
    return _emit(accumulator, state, arguments)


def _emit(
    accumulator: _ToolCallAccumulator,
    state: _StreamState,
    arguments: dict[str, Any],
) -> ToolCallDelta:
    """Mark a call emitted and build its event.

    Args:
        accumulator: The completed call.
        state: Stream state, whose emitted counter is bumped.
        arguments: Parsed arguments.

    Returns:
        The event to yield.
    """
    accumulator.emitted = True
    state.tool_calls_emitted += 1
    logger.info("llm.tool_call", name=accumulator.name, call_id=accumulator.public_id)
    return ToolCallDelta(
        call_id=accumulator.public_id,
        name=accumulator.name,
        arguments=arguments,
    )


def _parse_arguments(raw: str) -> dict[str, Any] | None:
    """Parse accumulated argument text, if it is yet a complete JSON object.

    Args:
        raw: Concatenated argument fragments.

    Returns:
        The arguments, or ``None`` while the text is empty, truncated, or not a
        JSON object.
    """
    if not raw.strip():
        return None
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _flush_tool_calls(state: _StreamState) -> list[ToolCallDelta]:
    """Emit calls the stream never completed, once it has ended.

    A tool with no required arguments legitimately streams an empty argument
    string, which cannot be recognised as complete while fragments may still
    arrive; end of stream is when it becomes ``{}``. Anything else still
    unparsed is a truncated call, dropped with a warning rather than crashing
    the turn.

    Args:
        state: Stream state after the last event.

    Returns:
        The calls completed by end of stream.
    """
    completed: list[ToolCallDelta] = []
    for accumulator in state.calls.values():
        if accumulator.emitted:
            continue
        if not accumulator.name:
            logger.warning("llm.tool_call_unnamed", call_id=accumulator.call_id)
            continue
        if accumulator.arguments.strip():
            logger.warning(
                "llm.tool_args_unparsed",
                name=accumulator.name,
                chars=len(accumulator.arguments),
            )
            continue
        completed.append(_emit(accumulator, state, {}))
    return completed


def _resolve_finish_reason(
    raw: str | None,
    tool_calls_emitted: int,
) -> Literal["stop", "tool_calls", "length"]:
    """Map the provider's finish reason onto the protocol's vocabulary.

    ``[DONE]`` can arrive without any chunk having carried a finish reason, and
    providers add reasons of their own (``content_filter``). Both fall back to
    what the stream actually contained, so a turn that called tools is reported
    as such and the pipeline knows to send the results back.

    Args:
        raw: The last ``finish_reason`` seen, if any.
        tool_calls_emitted: How many tool calls the stream produced.

    Returns:
        A reason :class:`~app.providers.base.LLMDone` accepts. ``cancelled`` and
        ``error`` are never returned here: cancellation propagates as
        :class:`asyncio.CancelledError` and failure as
        :class:`~app.errors.ProviderError`, so only the session that catches
        them can report them.
    """
    if raw == "stop":
        return "stop"
    if raw == "tool_calls":
        return "tool_calls"
    if raw == "length":
        return "length"
    if raw:
        logger.warning("llm.unknown_finish_reason", finish_reason=raw)
    return "tool_calls" if tool_calls_emitted else "stop"
