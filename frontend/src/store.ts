/**
 * The session store (TR-130) and the event log that makes a session legible (PRD F10).
 *
 * A single zustand store holds everything the UI renders. Server messages enter through one door,
 * `applyServerMessage`, which is where the two rules that matter live:
 *
 * - TR-131 stale-turn guard: anything carrying a `turn_id` older than the current turn is dropped,
 *   because a barge-in makes the previous turn's tail irrelevant. `state` is exempt from being
 *   dropped — it is the message that advances the turn, so guarding it would freeze the machine —
 *   but it cannot move the counter backwards either: only `session.ready`, which begins a new
 *   session with its own turn numbering, restarts it.
 * - TR-133 clamping: an out-of-range `slide.goto` is clamped into the deck and logged, never
 *   allowed to index past the end of the slide list.
 *
 * Message field names stay in wire form (`turn_id`) only inside `protocol.ts`; everything this
 * module exposes is camelCase, except the `exportEvents()` envelope, which is deliberately raw
 * protocol JSON so it can be replayed (TRD §7.2).
 */

import { create } from "zustand";

import type {
  Deck,
  ErrorCode,
  ProviderNames,
  ServerMessage,
  SessionState,
  Slide,
  ToolSource,
} from "./protocol";
import type { ConnectionStatus } from "./session/client";

// --------------------------------------------------------------------------- //
// Event log
// --------------------------------------------------------------------------- //

/** The kinds of entry the event log renders (PRD F10). */
export type LogEventKind =
  | "session"
  | "state"
  | "user"
  | "agent"
  | "tool"
  | "slide"
  | "interrupt"
  | "metrics"
  | "error"
  | "notice";

interface LogEventCommon {
  /** Monotonic within a session; stable React key. */
  readonly id: number;
  /** Epoch milliseconds, taken from the client clock. */
  readonly clientTs: number;
  /** The protocol message that produced this entry, or `null` for client-side entries. */
  readonly message: ServerMessage | null;
}

/** The session was accepted and the deck is on screen. */
export interface SessionLogEvent extends LogEventCommon {
  readonly kind: "session";
  readonly sessionId: string;
  readonly deckTitle: string;
  readonly providers: ProviderNames;
}

/** A turn-taking transition, rendered as a thin divider with its dwell time. */
export interface StateLogEvent extends LogEventCommon {
  readonly kind: "state";
  readonly turnId: number;
  readonly from: SessionState;
  readonly to: SessionState;
  /** Time spent in `from`, in milliseconds; zero for the first transition of a session. */
  readonly elapsedMs: number;
}

/** What the user was heard to say. */
export interface UserLogEvent extends LogEventCommon {
  readonly kind: "user";
  readonly turnId: number;
  readonly text: string;
}

/** One agent sentence. `cancelled` entries are rendered struck-through (TR-132). */
export interface AgentLogEvent extends LogEventCommon {
  readonly kind: "agent";
  readonly turnId: number;
  readonly sentenceId: number;
  readonly text: string;
  readonly cancelled: boolean;
}

/** A navigation decision, with the source that distinguishes the chip's colour. */
export interface ToolLogEvent extends LogEventCommon {
  readonly kind: "tool";
  readonly turnId: number;
  readonly name: string;
  readonly args: Readonly<Record<string, unknown>>;
  readonly source: ToolSource;
}

/** The deck actually moved. `index` is the clamped index, not necessarily the requested one. */
export interface SlideLogEvent extends LogEventCommon {
  readonly kind: "slide";
  readonly index: number;
  readonly highlight: number | null;
  readonly reason: string;
}

/** The agent was cut off; `heardSentences` is how much of the answer the user actually heard. */
export interface InterruptLogEvent extends LogEventCommon {
  readonly kind: "interrupt";
  readonly turnId: number;
  readonly heardSentences: number;
}

/** Per-turn latencies, shown only when the debug toggle is on. */
export interface MetricsLogEvent extends LogEventCommon {
  readonly kind: "metrics";
  readonly turnId: number;
  readonly sample: MetricsSample;
}

/** A failure the user should see. */
export interface ErrorLogEvent extends LogEventCommon {
  readonly kind: "error";
  readonly code: ErrorCode;
  readonly text: string;
  readonly recoverable: boolean;
}

/**
 * A client-side remark, such as a clamped slide index or a reconnect.
 *
 * Most notices are debugging detail and stay behind the debug toggle (PRD F10). `alert` marks the
 * ones a user has to see regardless: a connection the client has given up on is not a detail, and
 * hiding its explanation leaves the amber orb pointing at an empty log (TR-175, PRD F2).
 */
export interface NoticeLogEvent extends LogEventCommon {
  readonly kind: "notice";
  readonly text: string;
  /** Render the entry even when the debug toggle is off. */
  readonly alert: boolean;
}

/** Any entry in the event log. */
export type LogEvent =
  | SessionLogEvent
  | StateLogEvent
  | UserLogEvent
  | AgentLogEvent
  | ToolLogEvent
  | SlideLogEvent
  | InterruptLogEvent
  | MetricsLogEvent
  | ErrorLogEvent
  | NoticeLogEvent;

/** `Omit` collapses a union into its common members, so distribute it by hand. */
type DistributiveOmit<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never;

/** A log entry before the store stamps it with an id and a timestamp. */
type LogEventDraft = DistributiveOmit<LogEvent, "id" | "clientTs">;

// --------------------------------------------------------------------------- //
// Metrics
// --------------------------------------------------------------------------- //

/** One turn's latencies. Server-measured stages plus the two the client owns (PRD F12). */
export interface MetricsSample {
  readonly turnId: number;
  readonly sttMs: number | null;
  readonly llmTtftMs: number | null;
  readonly llmTotalMs: number | null;
  readonly ttsTtfbMs: number | null;
  /** `speech.end` → first sample played; measured in the browser. */
  readonly firstAudioMs: number | null;
  /** Speech onset during playback → audio silent; measured in the browser (TR-125). */
  readonly interruptStopMs: number | null;
  readonly sentences: number;
}

/** The timed fields of a sample, i.e. everything the HUD takes a median of. */
export type MetricsField =
  "sttMs" | "llmTtftMs" | "llmTotalMs" | "ttsTtfbMs" | "firstAudioMs" | "interruptStopMs";

const METRICS_FIELDS: readonly MetricsField[] = [
  "sttMs",
  "llmTtftMs",
  "llmTotalMs",
  "ttsTtfbMs",
  "firstAudioMs",
  "interruptStopMs",
];

/** Rolling medians across the session; `null` where no turn has reported that stage yet. */
export type MetricsMedians = Readonly<Record<MetricsField, number | null>>;

/** The client-measured timings a caller may push into a turn's sample. */
export interface ClientTimings {
  readonly firstAudioMs?: number;
  readonly interruptStopMs?: number;
}

/** The metrics slice of the store. */
export interface MetricsState {
  readonly last: MetricsSample | null;
  readonly medians: MetricsMedians;
  /** Every sample this session, needed to recompute medians on each update. */
  readonly history: readonly MetricsSample[];
}

// --------------------------------------------------------------------------- //
// Store shape
// --------------------------------------------------------------------------- //

/** User-controlled toggles (PRD F13). */
export interface SessionSettings {
  /** Push-to-talk instead of VAD. */
  readonly ptt: boolean;
  /** Show raw metrics and misfires in the event log. */
  readonly debug: boolean;
  /** Stop sending microphone audio; the agent may still speak. */
  readonly muted: boolean;
}

/** Everything the UI renders (TR-130). */
export interface SessionData {
  readonly connection: ConnectionStatus;
  readonly agentState: SessionState;
  readonly turnId: number;
  readonly currentSlide: number;
  readonly highlight: number | null;
  readonly deck: Deck | null;
  readonly sessionId: string | null;
  readonly providers: ProviderNames | null;
  readonly protocolVersion: number | null;
  /** ISO-8601 timestamp of `session.ready`, used by the export envelope. */
  readonly startedAt: string | null;
  readonly events: readonly LogEvent[];
  readonly metrics: MetricsState;
  readonly settings: SessionSettings;
  /** Next event id; part of the state so `reset()` cannot leave a counter behind. */
  readonly eventSeq: number;
  /** When the last `state` message arrived, so the next one can report its dwell time. */
  readonly lastStateAt: number | null;
}

/** The JSON envelope produced by "Copy log" (TRD §7.2). */
export interface EventsExport {
  readonly session_id: string | null;
  readonly deck_id: string | null;
  readonly started_at: string | null;
  readonly events: readonly Readonly<Record<string, unknown>>[];
}

/** How a notice should be surfaced. */
export interface NoticeOptions {
  /** Show the entry even with the debug toggle off; defaults to `false`. */
  readonly alert?: boolean;
}

/** Everything that mutates the store. */
export interface SessionActions {
  /** Record a connection lifecycle change from `SessionClient`. */
  setConnection: (connection: ConnectionStatus) => void;
  /** Apply one server message, honouring TR-131 and TR-133. */
  applyServerMessage: (message: ServerMessage) => void;
  /** Merge client-measured latencies into a turn's sample (PRD F12, TR-125). */
  recordClientTimings: (turnId: number, timings: ClientTimings) => void;
  /** Append a client-side remark to the event log. */
  logNotice: (text: string, options?: NoticeOptions) => void;
  /** Update one or more user toggles. */
  updateSettings: (patch: Partial<SessionSettings>) => void;
  /** Build the replayable JSON envelope of the whole log (TRD §7.2). */
  exportEvents: () => EventsExport;
  /** Drop everything the last session produced, keeping the user's toggles (PRD F13). */
  clearSession: () => void;
  /** Return to the initial state, toggles included; used between tests. */
  reset: () => void;
}

export type SessionStore = SessionData & SessionActions;

const EMPTY_MEDIANS: MetricsMedians = {
  sttMs: null,
  llmTtftMs: null,
  llmTotalMs: null,
  ttsTtfbMs: null,
  firstAudioMs: null,
  interruptStopMs: null,
};

/**
 * Build a fresh initial state.
 *
 * A function rather than a shared constant so `reset()` cannot hand back an object that a previous
 * session already mutated by reference.
 *
 * @returns The state of a store that has never seen a message.
 */
function createInitialData(): SessionData {
  return {
    connection: "idle",
    agentState: "idle",
    turnId: 0,
    currentSlide: 1,
    highlight: null,
    deck: null,
    sessionId: null,
    providers: null,
    protocolVersion: null,
    startedAt: null,
    events: [],
    metrics: { last: null, medians: EMPTY_MEDIANS, history: [] },
    settings: { ptt: false, debug: false, muted: false },
    eventSeq: 0,
    lastStateAt: null,
  };
}

// --------------------------------------------------------------------------- //
// Helpers
// --------------------------------------------------------------------------- //

/**
 * Read the `turn_id` of a message, if it has one.
 *
 * @param message - Any server message.
 * @returns The turn id, or `null` for messages that are not scoped to a turn.
 */
function turnIdOf(message: ServerMessage): number | null {
  if (!("turn_id" in message)) {
    return null;
  }
  const value: unknown = message.turn_id;
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * Decide whether a turn-scoped message belongs to a turn that has already been superseded.
 *
 * Exported because the audio path needs the same rule: frames that arrive after a barge-in must be
 * dropped rather than played over the next answer.
 *
 * @param currentTurnId - The store's current turn.
 * @param turnId - The turn the message claims, or `null` when it claims none.
 * @returns `true` when the message should be ignored (TR-131).
 */
export function isStaleTurn(currentTurnId: number, turnId: number | null): boolean {
  return turnId !== null && turnId < currentTurnId;
}

/**
 * Clamp a requested slide index into the deck (TR-133).
 *
 * @param index - The requested 1-based index, possibly out of range or not an integer.
 * @param slideCount - Number of slides available.
 * @returns An index that is safe to render.
 */
function clampSlide(index: number, slideCount: number): number {
  if (!Number.isFinite(index)) {
    return 1;
  }
  return Math.min(Math.max(Math.trunc(index), 1), Math.max(slideCount, 1));
}

/**
 * Stamp a draft entry with its identity.
 *
 * @param draft - The entry without id or timestamp.
 * @param id - Monotonic event id.
 * @param clientTs - Epoch milliseconds.
 * @returns The complete log entry.
 */
function stampEvent(draft: LogEventDraft, id: number, clientTs: number): LogEvent {
  return { ...draft, id, clientTs };
}

/**
 * Append entries to the log, allocating ids as it goes.
 *
 * @param events - The log to extend.
 * @param eventSeq - The next free id.
 * @param clientTs - Timestamp to stamp on every new entry.
 * @param drafts - Entries to append, in order.
 * @returns The `events` and `eventSeq` slice of the next state.
 */
function appendEvents(
  events: readonly LogEvent[],
  eventSeq: number,
  clientTs: number,
  drafts: readonly LogEventDraft[],
): Pick<SessionData, "events" | "eventSeq"> {
  const next = [...events];
  let seq = eventSeq;
  for (const draft of drafts) {
    next.push(stampEvent(draft, seq, clientTs));
    seq += 1;
  }
  return { events: next, eventSeq: seq };
}

/**
 * Median of a list of numbers.
 *
 * @param values - The samples; may be empty.
 * @returns The median rounded to a whole millisecond, or `null` when there is nothing to average.
 */
function median(values: readonly number[]): number | null {
  if (values.length === 0) {
    return null;
  }
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  const upper = sorted[middle] ?? 0;
  if (sorted.length % 2 === 1) {
    return upper;
  }
  const lower = sorted[middle - 1] ?? 0;
  return Math.round((lower + upper) / 2);
}

/**
 * Recompute every rolling median from the session's samples.
 *
 * @param history - All samples recorded this session.
 * @returns One median per timed field.
 */
function computeMedians(history: readonly MetricsSample[]): MetricsMedians {
  const medians: Record<MetricsField, number | null> = { ...EMPTY_MEDIANS };
  for (const field of METRICS_FIELDS) {
    const values: number[] = [];
    for (const sample of history) {
      const value = sample[field];
      if (value !== null) {
        values.push(value);
      }
    }
    medians[field] = median(values);
  }
  return medians;
}

/**
 * Insert or replace the sample for a turn, keeping the history in arrival order.
 *
 * @param history - Existing samples.
 * @param sample - The sample to store.
 * @returns A new history array.
 */
function upsertSample(
  history: readonly MetricsSample[],
  sample: MetricsSample,
): readonly MetricsSample[] {
  const index = history.findIndex((entry) => entry.turnId === sample.turnId);
  if (index === -1) {
    return [...history, sample];
  }
  const next = [...history];
  next[index] = sample;
  return next;
}

/**
 * Report a server message the store does not handle.
 *
 * The parameter is typed `never`, so adding a message type to `protocol.ts` without teaching the
 * store about it is a compile error rather than a message that silently disappears.
 *
 * @param message - Unreachable by construction.
 */
function logUnhandledMessage(message: never): void {
  console.error("store: unhandled server message", message);
}

/**
 * Compute the state change one server message produces.
 *
 * @param state - The current state.
 * @param message - The message to apply.
 * @param now - Epoch milliseconds to stamp on any resulting log entries.
 * @returns The partial state to merge, or `null` when the message is ignored (TR-131).
 */
function reduceServerMessage(
  state: SessionData,
  message: ServerMessage,
  now: number,
): Partial<SessionData> | null {
  // TR-131. `state` is exempt: it carries the turn forward, so guarding it would deadlock the
  // machine at the first turn.
  if (message.type !== "state" && isStaleTurn(state.turnId, turnIdOf(message))) {
    return null;
  }

  switch (message.type) {
    case "session.ready":
      return {
        sessionId: message.session_id,
        protocolVersion: message.protocol_version,
        deck: message.deck,
        providers: message.providers,
        startedAt: new Date(now).toISOString(),
        connection: "connected",
        currentSlide: 1,
        highlight: null,
        // A reconnect opens a *new* server session whose turns start at zero again (TR-175), so
        // the counter the stale-turn guard compares against has to start over with it. Without
        // this the guard would drop every message of the reconnected session.
        turnId: 0,
        ...appendEvents(state.events, state.eventSeq, now, [
          {
            kind: "session",
            message,
            sessionId: message.session_id,
            deckTitle: message.deck.title,
            providers: message.providers,
          },
        ]),
      };

    case "state":
      return {
        agentState: message.value,
        // Monotonic, and only ever from a turn id that is really a number: a `state` frame from a
        // superseded turn must not reopen the TR-131 gate for the rest of that dead turn's
        // messages, and a malformed one must not leave `NaN` in the counter, which compares false
        // against everything and would switch the guard off for the whole session. Turn numbering
        // restarts only at `session.ready`, which is a new session rather than a late frame.
        turnId: Math.max(state.turnId, turnIdOf(message) ?? state.turnId),
        lastStateAt: now,
        ...appendEvents(state.events, state.eventSeq, now, [
          {
            kind: "state",
            message,
            turnId: message.turn_id,
            from: state.agentState,
            to: message.value,
            elapsedMs: state.lastStateAt === null ? 0 : now - state.lastStateAt,
          },
        ]),
      };

    case "transcript.user":
      return appendEvents(state.events, state.eventSeq, now, [
        { kind: "user", message, turnId: message.turn_id, text: message.text },
      ]);

    case "transcript.agent":
      return appendEvents(state.events, state.eventSeq, now, [
        {
          kind: "agent",
          message,
          turnId: message.turn_id,
          sentenceId: message.sentence_id,
          text: message.text,
          cancelled: false,
        },
      ]);

    case "tool.call":
      return appendEvents(state.events, state.eventSeq, now, [
        {
          kind: "tool",
          message,
          turnId: message.turn_id,
          name: message.name,
          args: message.args,
          source: message.source,
        },
      ]);

    case "slide.goto": {
      // With no deck loaded there is nothing to clamp against, so the request is taken at face
      // value and the deck's own arrival will correct the view.
      const slideCount = state.deck === null ? message.index : state.deck.slides.length;
      const index = clampSlide(message.index, slideCount);
      const drafts: LogEventDraft[] = [];
      if (index !== message.index) {
        drafts.push({
          kind: "notice",
          message: null,
          alert: false,
          text: `slide ${message.index} is outside the deck (1-${slideCount}); showing ${index}`,
        });
      }
      drafts.push({
        kind: "slide",
        message,
        index,
        highlight: message.highlight,
        reason: message.reason,
      });
      return {
        currentSlide: index,
        highlight: message.highlight,
        ...appendEvents(state.events, state.eventSeq, now, drafts),
      };
    }

    case "agent.cancelled": {
      const truncatedAt = message.truncated_at_sentence_id;
      // `null` means playback never started, so nothing of this turn was heard (TR-051).
      const marked = state.events.map((event) =>
        event.kind === "agent" &&
        event.turnId === message.turn_id &&
        (truncatedAt === null || event.sentenceId > truncatedAt)
          ? { ...event, cancelled: true }
          : event,
      );
      return appendEvents(marked, state.eventSeq, now, [
        {
          kind: "interrupt",
          message,
          turnId: message.turn_id,
          heardSentences: truncatedAt === null ? 0 : truncatedAt + 1,
        },
      ]);
    }

    case "metrics": {
      // Client-measured timings may already have landed for this turn; keep them.
      const existing = state.metrics.history.find((entry) => entry.turnId === message.turn_id);
      const sample: MetricsSample = {
        turnId: message.turn_id,
        sttMs: message.stt_ms,
        llmTtftMs: message.llm_ttft_ms,
        llmTotalMs: message.llm_total_ms,
        ttsTtfbMs: message.tts_ttfb_ms,
        firstAudioMs: existing?.firstAudioMs ?? null,
        interruptStopMs: existing?.interruptStopMs ?? null,
        sentences: message.sentences,
      };
      const history = upsertSample(state.metrics.history, sample);
      return {
        metrics: { last: sample, medians: computeMedians(history), history },
        ...appendEvents(state.events, state.eventSeq, now, [
          { kind: "metrics", message, turnId: message.turn_id, sample },
        ]),
      };
    }

    case "error":
      return appendEvents(state.events, state.eventSeq, now, [
        {
          kind: "error",
          message,
          code: message.code,
          text: message.message,
          recoverable: message.recoverable,
        },
      ]);

    default:
      logUnhandledMessage(message);
      return null;
  }
}

/**
 * Reduce a log entry to the raw JSON the export envelope carries.
 *
 * Server-produced entries export their original protocol message verbatim, which is what makes the
 * export replayable (TRD §7.2). Client-side entries get a synthetic `client.*` type so the two are
 * distinguishable without being confused for wire messages.
 *
 * @param event - The entry to serialise.
 * @returns A plain JSON object including `client_ts` in epoch seconds.
 */
function toExportedEvent(event: LogEvent): Readonly<Record<string, unknown>> {
  const payload: Record<string, unknown> =
    event.message !== null
      ? { ...event.message }
      : { type: `client.${event.kind}`, ...(event.kind === "notice" ? { text: event.text } : {}) };
  // Seconds, matching the `client_ts` convention of the protocol itself.
  payload.client_ts = event.clientTs / 1000;
  return payload;
}

// --------------------------------------------------------------------------- //
// Store
// --------------------------------------------------------------------------- //

/**
 * The application's session store.
 *
 * Components subscribe with selectors; non-React code (`SessionClient`, the audio pipeline) uses
 * `useSessionStore.getState()`. Tests must call `reset()` between cases, because the store is a
 * module singleton.
 */
export const useSessionStore = create<SessionStore>()((set, get) => ({
  ...createInitialData(),

  setConnection(connection) {
    set({ connection });
  },

  applyServerMessage(message) {
    set((state) => reduceServerMessage(state, message, Date.now()) ?? {});
  },

  recordClientTimings(turnId, timings) {
    set((state) => {
      const existing = state.metrics.history.find((entry) => entry.turnId === turnId);
      const base: MetricsSample = existing ?? {
        turnId,
        sttMs: null,
        llmTtftMs: null,
        llmTotalMs: null,
        ttsTtfbMs: null,
        firstAudioMs: null,
        interruptStopMs: null,
        sentences: 0,
      };
      const sample: MetricsSample = {
        ...base,
        firstAudioMs: timings.firstAudioMs ?? base.firstAudioMs,
        interruptStopMs: timings.interruptStopMs ?? base.interruptStopMs,
      };
      const history = upsertSample(state.metrics.history, sample);
      const last =
        state.metrics.last === null || sample.turnId >= state.metrics.last.turnId
          ? sample
          : state.metrics.last;
      return { metrics: { last, medians: computeMedians(history), history } };
    });
  },

  logNotice(text, options) {
    set((state) =>
      appendEvents(state.events, state.eventSeq, Date.now(), [
        { kind: "notice", message: null, text, alert: options?.alert ?? false },
      ]),
    );
  },

  updateSettings(patch) {
    set((state) => ({ settings: { ...state.settings, ...patch } }));
  },

  exportEvents() {
    const { sessionId, deck, startedAt, events } = get();
    return {
      session_id: sessionId,
      deck_id: deck?.id ?? null,
      started_at: startedAt,
      events: events.map(toExportedEvent),
    };
  },

  clearSession() {
    // Everything a session produced goes; the toggles are the user's, not the session's, so
    // starting a second session must not silently turn debug or push-to-talk back off (PRD F13).
    set((state) => ({ ...createInitialData(), settings: state.settings }));
  },

  reset() {
    set(createInitialData());
  },
}));

/**
 * Select the slide currently on screen.
 *
 * @param state - The store state.
 * @returns The slide, or `null` before a deck has arrived.
 */
export function selectCurrentSlide(state: SessionData): Slide | null {
  return state.deck?.slides[state.currentSlide - 1] ?? null;
}
