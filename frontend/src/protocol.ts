/**
 * WebSocket message contract (TRD §6) — the TypeScript mirror of `backend/app/protocol.py`.
 *
 * Text frames carry JSON objects with a `type` discriminator; binary frames carry audio. Message
 * field names are the wire names, so they stay `snake_case` here even though the rest of the
 * frontend is `camelCase`: these interfaces describe what `JSON.parse` actually produces, and a
 * translation layer between the socket and the store would be one more place for the two halves of
 * the protocol to drift apart.
 *
 * `CLIENT_MESSAGE_TYPES` and `SERVER_MESSAGE_TYPES` are read by the backend parity test (TR-143),
 * which asserts the two languages describe the same set of `type` strings. Keep the literals one
 * per line inside those arrays so the test can find them, and change this file in the same commit
 * as `protocol.py`.
 */

export const PROTOCOL_VERSION = 1;
/** Size of the binary audio frame header in bytes: two little-endian uint32s. */
export const AUDIO_HEADER_BYTES = 8;
/** Longest typed question accepted, matching the backend's `MAX_TEXT_INPUT_CHARS`. */
export const MAX_TEXT_INPUT_CHARS = 500;

const BYTES_PER_SAMPLE = 2;

/**
 * True when the host stores typed arrays little-endian first.
 *
 * The wire format is little-endian by definition, but `Int16Array` is *platform* endian, so a
 * zero-copy view is only correct on a little-endian host. Every browser platform in practice is
 * little-endian; the check costs one allocation at module load and removes the assumption.
 */
const PLATFORM_IS_LITTLE_ENDIAN = new Uint8Array(new Uint16Array([1]).buffer)[0] === 1;

/** States of a session's turn-taking machine (TRD §7). */
export const SESSION_STATES = [
  "idle",
  "connecting",
  "listening",
  "hearing",
  "thinking",
  "speaking",
  "interrupted",
  "error",
] as const;

export type SessionState = (typeof SESSION_STATES)[number];

/** Stable error codes sent to the client (TRD §6.2). */
export const ERROR_CODES = [
  "bad_message",
  "unexpected_binary",
  "stt_failed",
  "llm_failed",
  "tts_failed",
  "turn_timeout",
  "internal_error",
  "deck_not_found",
  "rate_limited",
] as const;

export type ErrorCode = (typeof ERROR_CODES)[number];

/** Whether the agent waits for questions or walks the deck unprompted. */
export const SESSION_MODES = ["qa", "present"] as const;

export type SessionMode = (typeof SESSION_MODES)[number];

/**
 * Origin of a navigation decision.
 *
 * `llm` means the model called the tool; `fallback` means the keyword matcher inferred the target
 * because the model answered about another slide without calling it (TR-062). The UI shows the two
 * as different chips so the paths can be told apart while demonstrating the system.
 */
export const TOOL_SOURCES = ["llm", "fallback"] as const;

export type ToolSource = (typeof TOOL_SOURCES)[number];

/** Session-level commands issued from the UI. */
export const CONTROL_ACTIONS = ["start_presentation", "pause", "resume", "mute", "unmute"] as const;

export type ControlAction = (typeof CONTROL_ACTIONS)[number];

// --------------------------------------------------------------------------- //
// Deck (mirror of backend/app/decks/models.py)
// --------------------------------------------------------------------------- //

/** One slide, with the notes and aliases the agent reasons over. */
/** How a slide arranges its points on screen (PRD F1). */
export type FigureKind = "metrics" | "split" | "flow";

/**
 * One card, column or step, and the bullets it presents.
 *
 * `bullets` is the whole point of the design: an item does not carry its own text, it points at
 * the slide's bullets by index. Those same bullets are what the agent is given, so the screen and
 * the agent's view of the slide cannot drift apart, and `highlight_bullet(n)` finds its target in
 * any layout by looking for the item that claims bullet `n`.
 */
export interface FigureItem {
  readonly bullets: readonly number[];
  readonly heading: string;
  readonly caption: string;
}

/** The arrangement of a slide's points. Presentation only; the agent never sees it. */
export interface Figure {
  readonly kind: FigureKind;
  readonly items: readonly FigureItem[];
}

export interface Slide {
  readonly index: number;
  readonly title: string;
  readonly bullets: readonly string[];
  readonly notes: string;
  readonly aliases: readonly string[];
  /** How to arrange the bullets, or absent for a plain list. */
  readonly figure?: Figure | null;
}

/** A presentable deck; `voice` overrides the configured default when set. */
export interface Deck {
  readonly id: string;
  readonly title: string;
  readonly voice: string | null;
  readonly slides: readonly Slide[];
}

// --------------------------------------------------------------------------- //
// Client -> Server
// --------------------------------------------------------------------------- //

/** Open a session against a deck. Must be the first message. */
export interface SessionStartMessage {
  readonly type: "session.start";
  readonly deck_id: string;
  readonly mode: SessionMode;
  readonly client_ts?: number;
}

/** Voice activity began. Doubles as an interrupt while the agent speaks. */
export interface SpeechStartMessage {
  readonly type: "speech.start";
  readonly client_ts?: number;
}

/** An utterance finished; exactly one binary frame follows (TR-140). */
export interface SpeechEndMessage {
  readonly type: "speech.end";
  readonly duration_ms: number;
  readonly client_ts?: number;
}

/**
 * Barge-in, carrying how much of the answer the user actually heard.
 *
 * `last_completed_sentence_id` is `null` when playback had not started, in which case nothing of
 * the answer was heard (TR-051).
 */
export interface InterruptMessage {
  readonly type: "interrupt";
  readonly last_completed_sentence_id: number | null;
  readonly client_ts?: number;
}

/** The speech onset that caused an interrupt turned out to be a misfire. */
export interface InterruptCancelMessage {
  readonly type: "interrupt.cancel";
}

/** A sentence finished playing. Drives the return to `listening`. */
export interface PlaybackProgressMessage {
  readonly type: "playback.progress";
  readonly turn_id: number;
  readonly sentence_id: number;
}

/** The user navigated manually; the agent's context follows the screen. */
export interface SlideChangedMessage {
  readonly type: "slide.changed";
  readonly index: number;
  readonly source: "user";
}

/** A typed question, bypassing speech-to-text (F13). */
export interface TextInputMessage {
  readonly type: "text.input";
  readonly text: string;
}

/** Change presentation mode or muting. */
export interface ControlMessage {
  readonly type: "control";
  readonly action: ControlAction;
}

/** Any message the client may send. */
export type ClientMessage =
  | SessionStartMessage
  | SpeechStartMessage
  | SpeechEndMessage
  | InterruptMessage
  | InterruptCancelMessage
  | PlaybackProgressMessage
  | SlideChangedMessage
  | TextInputMessage
  | ControlMessage;

export type ClientMessageType = ClientMessage["type"];

// --------------------------------------------------------------------------- //
// Server -> Client
// --------------------------------------------------------------------------- //

/** Which provider implementation is serving each stage. */
export interface ProviderNames {
  readonly stt: string;
  readonly llm: string;
  readonly tts: string;
}

/** Session accepted; carries the deck so the client can render it. */
export interface SessionReadyMessage {
  readonly type: "session.ready";
  readonly session_id: string;
  readonly protocol_version: number;
  readonly deck: Deck;
  readonly providers: ProviderNames;
}

/** A turn-taking state transition (TR-020). */
export interface StateMessage {
  readonly type: "state";
  readonly value: SessionState;
  readonly turn_id: number;
  readonly server_ts: number;
}

/** What the user was heard to say. */
export interface TranscriptUserMessage {
  readonly type: "transcript.user";
  readonly turn_id: number;
  readonly text: string;
  readonly final: boolean;
}

/** One spoken sentence, sent as its first audio leaves (TR-033). */
export interface TranscriptAgentMessage {
  readonly type: "transcript.agent";
  readonly turn_id: number;
  readonly sentence_id: number;
  readonly text: string;
}

/** A navigation decision, and whether the model or the fallback made it. */
export interface ToolCallMessage {
  readonly type: "tool.call";
  readonly turn_id: number;
  readonly name: string;
  readonly args: Readonly<Record<string, unknown>>;
  readonly source: ToolSource;
}

/** Move the deck. `reason` is shown in the event log. */
export interface SlideGotoMessage {
  readonly type: "slide.goto";
  readonly turn_id: number;
  readonly index: number;
  readonly highlight: number | null;
  readonly reason: string;
}

/** The turn was cut short; history was truncated at this sentence. */
export interface AgentCancelledMessage {
  readonly type: "agent.cancelled";
  readonly turn_id: number;
  readonly truncated_at_sentence_id: number | null;
}

/** Per-stage latencies for one turn, in milliseconds (TR-163). */
export interface MetricsMessage {
  readonly type: "metrics";
  readonly turn_id: number;
  readonly stt_ms: number | null;
  readonly llm_ttft_ms: number | null;
  readonly llm_total_ms: number | null;
  readonly tts_ttfb_ms: number | null;
  readonly sentences: number;
}

/** A failure the client should surface. `recoverable` keeps the session. */
/**
 * The turn is being answered by a different model than usual (TR-085).
 *
 * Sent once, before the answer, when the hosted model is out of free-tier capacity and a model on
 * the user's own machine is answering instead. The voice that follows will be noticeably slower,
 * and saying so is the difference between a system that degraded and one that looks broken.
 */
export interface ProviderFallbackMessage {
  readonly type: "provider.fallback";
  readonly turn_id: number;
  readonly stage: "llm";
  /** The model that could not answer. */
  readonly from_model: string;
  /** The model answering instead. */
  readonly to_model: string;
  /** Short phrase for the user, e.g. "the hosted model is out of free-tier capacity". */
  readonly reason: string;
  /** When the first model is expected back, if it said. */
  readonly retry_after_s?: number | null;
}

export interface ErrorMessage {
  readonly type: "error";
  readonly code: ErrorCode;
  readonly message: string;
  readonly recoverable: boolean;
  /**
   * Seconds the upstream asked us to wait, when it said (TR-171).
   *
   * Only a rate limit carries one. It is a number rather than part of `message` because the UI
   * counts it down, and parsing a wait out of an upstream error string is exactly the kind of
   * thing that breaks when the provider rewords it.
   */
  readonly retry_after_s?: number | null;
}

/** Any message the server may send. */
export type ServerMessage =
  | SessionReadyMessage
  | StateMessage
  | TranscriptUserMessage
  | TranscriptAgentMessage
  | ToolCallMessage
  | SlideGotoMessage
  | AgentCancelledMessage
  | MetricsMessage
  | ProviderFallbackMessage
  | ErrorMessage;

export type ServerMessageType = ServerMessage["type"];

/**
 * Every client message type, for the cross-language parity test (TR-143).
 *
 * `satisfies` rejects a literal that is not a real message type. The opposite direction — a type
 * present in the union but missing from this array — is what the backend parity test catches.
 */
export const CLIENT_MESSAGE_TYPES = [
  "session.start",
  "speech.start",
  "speech.end",
  "interrupt",
  "interrupt.cancel",
  "playback.progress",
  "slide.changed",
  "text.input",
  "control",
] as const satisfies readonly ClientMessageType[];

/** Every server message type, for the cross-language parity test (TR-143). */
export const SERVER_MESSAGE_TYPES = [
  "session.ready",
  "state",
  "transcript.user",
  "transcript.agent",
  "tool.call",
  "slide.goto",
  "agent.cancelled",
  "metrics",
  "provider.fallback",
  "error",
] as const satisfies readonly ServerMessageType[];

const SERVER_MESSAGE_TYPE_SET: ReadonlySet<string> = new Set<string>(SERVER_MESSAGE_TYPES);

// --------------------------------------------------------------------------- //
// Codecs
// --------------------------------------------------------------------------- //

/** A decoded server audio frame. */
export interface AudioFrame {
  /** Which sentence of the current turn this audio belongs to. */
  readonly sentenceId: number;
  /** Position of this chunk within the sentence, from zero. */
  readonly seq: number;
  /** Little-endian 16-bit mono samples at the output rate. */
  readonly pcm: Int16Array;
}

/**
 * Split a binary server frame into its header fields and samples.
 *
 * Mirrors `decode_audio_frame` in `backend/app/protocol.py`: an 8-byte little-endian header of
 * `sentence_id` then `seq`, followed by PCM16 samples. Phase 2's playback queue is the caller;
 * it exists now so the parity with the backend helper is visible in one place.
 *
 * @param buffer - A complete binary frame as received from the socket.
 * @returns The sentence id, the sequence number, and the samples.
 * @throws RangeError If the frame is too short to hold a header, or its payload is not a whole
 *   number of 16-bit samples.
 */
export function decodeAudioFrame(buffer: ArrayBuffer): AudioFrame {
  if (buffer.byteLength < AUDIO_HEADER_BYTES) {
    throw new RangeError(
      `audio frame must be at least ${AUDIO_HEADER_BYTES} bytes, got ${buffer.byteLength}`,
    );
  }
  const header = new DataView(buffer, 0, AUDIO_HEADER_BYTES);
  return {
    sentenceId: header.getUint32(0, true),
    seq: header.getUint32(4, true),
    // Copy rather than view: the socket's buffer may be pooled by the runtime, and playback holds
    // the samples across event-loop turns.
    pcm: toPcm16(buffer.slice(AUDIO_HEADER_BYTES)),
  };
}

/**
 * Reinterpret a little-endian byte payload as signed 16-bit samples.
 *
 * @param payload - The frame body, without its header.
 * @returns The samples in host order.
 * @throws RangeError If the payload length is odd, which means the frame was truncated.
 */
function toPcm16(payload: ArrayBuffer): Int16Array {
  if (payload.byteLength % BYTES_PER_SAMPLE !== 0) {
    throw new RangeError(
      `audio payload must be a whole number of samples, got ${payload.byteLength} bytes`,
    );
  }
  if (PLATFORM_IS_LITTLE_ENDIAN) {
    return new Int16Array(payload);
  }
  const view = new DataView(payload);
  const samples = new Int16Array(payload.byteLength / BYTES_PER_SAMPLE);
  for (let i = 0; i < samples.length; i += 1) {
    samples[i] = view.getInt16(i * BYTES_PER_SAMPLE, true);
  }
  return samples;
}

/**
 * Parse and narrow a text frame received from the server.
 *
 * Only the `type` discriminator is validated, because that is the field the store switches on and
 * the one that decides which shape the rest of the object has. Field-level validation is left to
 * the backend's Pydantic models and to TR-143's parity test; the store additionally guards the
 * values it cannot afford to trust, such as an out-of-range slide index (TR-133).
 *
 * @param raw - The UTF-8 JSON payload of a text frame.
 * @returns The typed message, or `null` when the frame is not valid JSON, not an object, or
 *   carries a `type` this build does not know.
 */
export function parseServerMessage(raw: string): ServerMessage | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return null;
  }
  const type: unknown = (parsed as { type?: unknown }).type;
  if (typeof type !== "string" || !SERVER_MESSAGE_TYPE_SET.has(type)) {
    return null;
  }
  return parsed as ServerMessage;
}
