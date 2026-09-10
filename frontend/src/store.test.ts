import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  AgentCancelledMessage,
  Deck,
  ErrorMessage,
  MetricsMessage,
  ServerMessage,
  SessionReadyMessage,
  SlideGotoMessage,
  StateMessage,
  ToolCallMessage,
  TranscriptAgentMessage,
  TranscriptUserMessage,
} from "./protocol";
import { selectCurrentSlide, useSessionStore, type AgentLogEvent, type LogEvent } from "./store";

const DECK_SLIDE_COUNT = 6;

/**
 * Build a deck that satisfies the backend's validation rules (5-8 slides, contiguous indices).
 *
 * @param slideCount - How many slides the deck should have.
 * @returns A deck fixture.
 */
function makeDeck(slideCount = DECK_SLIDE_COUNT): Deck {
  return {
    id: "anatomy_of_a_voice_agent",
    title: "Anatomy of a Voice Agent",
    voice: "af_heart",
    slides: Array.from({ length: slideCount }, (_, i) => ({
      index: i + 1,
      title: `Slide ${i + 1}`,
      bullets: ["first point", "second point"],
      notes: "Speaker notes the agent treats as ground truth.",
      aliases: [`slide-${i + 1}`, `alias-${i + 1}`],
    })),
  };
}

/**
 * Build a `session.ready` for a deck fixture.
 *
 * @param deck - The deck the session presents.
 * @returns The message.
 */
function ready(deck: Deck = makeDeck()): SessionReadyMessage {
  return {
    type: "session.ready",
    session_id: "sess-1",
    protocol_version: 1,
    deck,
    providers: { stt: "groq", llm: "groq", tts: "kokoro" },
  };
}

/**
 * Build a `state` message.
 *
 * @param value - The new session state.
 * @param turnId - The turn the state belongs to.
 * @returns The message.
 */
function state(value: StateMessage["value"], turnId: number): StateMessage {
  return { type: "state", value, turn_id: turnId, server_ts: 1_000 + turnId };
}

/**
 * Build a `transcript.agent` message.
 *
 * @param turnId - Turn the sentence belongs to.
 * @param sentenceId - Sentence index within the turn.
 * @param text - The spoken text.
 * @returns The message.
 */
function agentSentence(turnId: number, sentenceId: number, text: string): TranscriptAgentMessage {
  return { type: "transcript.agent", turn_id: turnId, sentence_id: sentenceId, text };
}

/**
 * Build a `metrics` message with only the stages a test cares about.
 *
 * @param turnId - Turn the metrics describe.
 * @param sttMs - Speech-to-text latency.
 * @param llmTtftMs - Time to first LLM token.
 * @returns The message.
 */
function metrics(turnId: number, sttMs: number, llmTtftMs: number): MetricsMessage {
  return {
    type: "metrics",
    turn_id: turnId,
    stt_ms: sttMs,
    llm_ttft_ms: llmTtftMs,
    llm_total_ms: null,
    tts_ttfb_ms: null,
    sentences: 2,
  };
}

/**
 * Apply a sequence of server messages to the store.
 *
 * @param messages - The messages, in arrival order.
 */
function apply(...messages: readonly ServerMessage[]): void {
  for (const message of messages) {
    useSessionStore.getState().applyServerMessage(message);
  }
}

/**
 * Read the current event log.
 *
 * @returns The entries.
 */
function events(): readonly LogEvent[] {
  return useSessionStore.getState().events;
}

// The store is a module singleton, so every test starts from a clean one. `reset()` failing would
// surface here as cross-test contamination rather than as a silent pass.
beforeEach(() => {
  useSessionStore.getState().reset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("session lifecycle", () => {
  it("TC-FE-106: session.ready fills in the TR-130 store shape and logs an entry", () => {
    expect(useSessionStore.getState().deck).toBeNull();

    apply(ready());
    const store = useSessionStore.getState();

    expect(store.deck?.id).toBe("anatomy_of_a_voice_agent");
    expect(store.sessionId).toBe("sess-1");
    expect(store.protocolVersion).toBe(1);
    expect(store.connection).toBe("connected");
    expect(store.currentSlide).toBe(1);
    expect(store.startedAt).not.toBeNull();
    expect(selectCurrentSlide(store)?.index).toBe(1);
    expect(events()).toHaveLength(1);
    expect(events()[0]).toMatchObject({ kind: "session", sessionId: "sess-1" });

    // The slices are independent: a connection change must not disturb the deck, and toggling one
    // setting must merge rather than replace the others.
    useSessionStore.getState().setConnection("reconnecting");
    useSessionStore.getState().updateSettings({ muted: true });
    expect(useSessionStore.getState().connection).toBe("reconnecting");
    expect(useSessionStore.getState().settings).toEqual({ ptt: false, debug: false, muted: true });
    expect(useSessionStore.getState().deck?.id).toBe("anatomy_of_a_voice_agent");
  });

  it("TC-FE-107: a state message advances the turn and times the state it left", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-10T09:00:00.000Z"));

    apply(state("listening", 0));
    vi.advanceTimersByTime(312);
    apply(state("thinking", 1));

    const transition = events().at(-1);
    expect(transition).toMatchObject({
      kind: "state",
      from: "listening",
      to: "thinking",
      elapsedMs: 312,
    });
    expect(useSessionStore.getState().turnId).toBe(1);
    expect(useSessionStore.getState().agentState).toBe("thinking");
  });

  it("TC-FE-031: ignores turn-scoped messages from a superseded turn, but not state", () => {
    apply(ready(), state("thinking", 5));
    const countAfterState = events().length;

    apply(agentSentence(4, 0, "stale answer from the interrupted turn"));

    expect(events()).toHaveLength(countAfterState);
    expect(events().some((event) => event.kind === "agent")).toBe(false);

    // The same message for the live turn is kept, proving the guard is about the turn and not the
    // message type.
    apply(agentSentence(5, 0, "current answer"));
    expect(events().at(-1)).toMatchObject({ kind: "agent", text: "current answer" });

    // `state` is exempt (TR-131): a server that resets its turn counter must still be able to
    // drive the machine.
    apply(state("listening", 0));
    expect(useSessionStore.getState().turnId).toBe(0);
  });
});

describe("event log", () => {
  it("TC-FE-108: logs user, agent, tool and error entries in arrival order", () => {
    const user: TranscriptUserMessage = {
      type: "transcript.user",
      turn_id: 0,
      text: "how do you handle interruptions?",
      final: true,
    };
    const tool: ToolCallMessage = {
      type: "tool.call",
      turn_id: 0,
      name: "go_to_slide",
      args: { slide_index: 4, reason: "keyword: latency" },
      source: "fallback",
    };
    const failure: ErrorMessage = {
      type: "error",
      code: "rate_limited",
      message: "retry after 3s",
      recoverable: true,
    };

    apply(ready(), user, tool, agentSentence(0, 0, "We use a VAD."), failure);

    expect(events().map((event) => event.kind)).toEqual([
      "session",
      "user",
      "tool",
      "agent",
      "error",
    ]);
    // The chip's colour depends on this: `fallback` means the keyword matcher, not the model.
    expect(events()[2]).toMatchObject({ source: "fallback", name: "go_to_slide" });
    expect(events()[4]).toMatchObject({ code: "rate_limited", recoverable: true });
  });

  it("TC-FE-032: agent.cancelled strikes through only the sentences never heard", () => {
    apply(ready(), state("speaking", 0));
    for (let sentenceId = 0; sentenceId < 4; sentenceId += 1) {
      apply(agentSentence(0, sentenceId, `sentence ${sentenceId}`));
    }

    const cancelled: AgentCancelledMessage = {
      type: "agent.cancelled",
      turn_id: 0,
      truncated_at_sentence_id: 1,
    };
    apply(cancelled);

    const sentences = events().filter((event): event is AgentLogEvent => event.kind === "agent");
    expect(sentences.map((event) => event.cancelled)).toEqual([false, false, true, true]);
    expect(events().at(-1)).toMatchObject({ kind: "interrupt", heardSentences: 2 });
  });
});

describe("slide navigation", () => {
  it("TC-FE-002: clamps an out-of-range slide.goto into the deck", () => {
    apply(ready(makeDeck(DECK_SLIDE_COUNT)));

    const goto: SlideGotoMessage = {
      type: "slide.goto",
      index: 42,
      highlight: null,
      reason: "hallucinated slide",
    };
    apply(goto);

    expect(useSessionStore.getState().currentSlide).toBe(DECK_SLIDE_COUNT);
  });

  it("TC-FE-109: records the clamp as an event instead of failing (TR-133)", () => {
    apply(ready());
    const before = events().length;

    apply({ type: "slide.goto", index: 0, highlight: 2, reason: "off the front" });

    const added = events().slice(before);
    expect(added.map((event) => event.kind)).toEqual(["notice", "slide"]);
    expect(added[0]).toMatchObject({ kind: "notice" });
    expect(added[1]).toMatchObject({ kind: "slide", index: 1, highlight: 2 });
    expect(useSessionStore.getState().currentSlide).toBe(1);
    expect(useSessionStore.getState().highlight).toBe(2);

    // An in-range move logs the slide entry alone: the notice is the exception, not the rule.
    apply({ type: "slide.goto", index: 3, highlight: null, reason: "user asked about latency" });
    expect(events().at(-1)).toMatchObject({ kind: "slide", index: 3 });
    expect(events().filter((event) => event.kind === "notice")).toHaveLength(1);
  });
});

describe("metrics", () => {
  it("TC-FE-110: keeps the last sample, the rolling medians, and client-measured timings", () => {
    apply(ready(), state("listening", 0));
    apply(metrics(0, 300, 100));
    apply(state("listening", 1), metrics(1, 500, 200));
    apply(state("listening", 2), metrics(2, 400, 900));

    useSessionStore.getState().recordClientTimings(2, { firstAudioMs: 1_200 });

    const { last, medians } = useSessionStore.getState().metrics;
    expect(last?.turnId).toBe(2);
    expect(last?.sttMs).toBe(400);
    expect(last?.firstAudioMs).toBe(1_200);
    expect(medians.sttMs).toBe(400);
    expect(medians.llmTtftMs).toBe(200);
    // Only one turn reported it, so its median is that value; stages nobody reported stay null.
    expect(medians.firstAudioMs).toBe(1_200);
    expect(medians.ttsTtfbMs).toBeNull();
  });
});

describe("exportEvents", () => {
  it("TC-FE-111: produces the TRD §7.2 envelope of raw messages plus client_ts", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-10T09:00:00.000Z"));

    apply(ready(), state("thinking", 1));
    apply({ type: "slide.goto", index: 99, highlight: null, reason: "out of range" });

    const exported = useSessionStore.getState().exportEvents();

    expect(exported.session_id).toBe("sess-1");
    expect(exported.deck_id).toBe("anatomy_of_a_voice_agent");
    expect(exported.started_at).toBe("2026-09-10T09:00:00.000Z");
    expect(exported.events).toHaveLength(4);
    // Server entries export the wire message verbatim so the log can be replayed.
    expect(exported.events[1]).toEqual({
      type: "state",
      value: "thinking",
      turn_id: 1,
      server_ts: 1_001,
      client_ts: Date.now() / 1000,
    });
    // The clamp notice has no wire message, so it is tagged as client-side rather than faked.
    expect(exported.events[2]).toMatchObject({ type: "client.notice" });
    expect(exported.events.every((event) => typeof event.client_ts === "number")).toBe(true);
  });
});
