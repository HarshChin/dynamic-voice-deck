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
 * Build a `slide.goto` message.
 *
 * @param turnId - The turn the navigation belongs to; the stale-turn guard reads it.
 * @param index - The requested 1-based slide index, which may be out of range.
 * @param highlight - Zero-based bullet to emphasise, or `null`.
 * @param reason - The sentence shown beside the entry in the log.
 * @returns The message.
 */
function goto(
  turnId: number,
  index: number,
  highlight: number | null,
  reason: string,
): SlideGotoMessage {
  return { type: "slide.goto", turn_id: turnId, index, highlight, reason };
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

    // `state` is exempt from being *dropped* (TR-131), so the machine can never freeze; it does
    // not rewind the counter, which is TC-FE-140.
    apply(state("listening", 0));
    expect(useSessionStore.getState().agentState).toBe("listening");
    expect(useSessionStore.getState().turnId).toBe(5);
  });

  it("TC-FE-140: a stale state message never rewinds the turn counter", () => {
    apply(ready(), state("thinking", 5));

    // A `state` frame belonging to a turn that has already been superseded. Taking its turn id at
    // face value would reopen the TR-131 gate for every other message of that dead turn.
    apply(state("speaking", 3));

    expect(useSessionStore.getState().turnId).toBe(5);
    const before = events().length;
    apply(agentSentence(3, 0, "tail of the interrupted turn"));
    expect(events()).toHaveLength(before);

    // The counter still follows the server forward, which is what the exemption exists for.
    apply(state("listening", 6));
    expect(useSessionStore.getState().turnId).toBe(6);

    // A malformed turn id must not land in the counter either: `NaN` compares false against
    // everything, so it would switch the guard off for the rest of the session.
    apply({ ...state("listening", 6), turn_id: Number.NaN });
    expect(useSessionStore.getState().turnId).toBe(6);
    const beforeGarbage = events().length;
    apply(agentSentence(5, 0, "still from a dead turn"));
    expect(events()).toHaveLength(beforeGarbage);
  });

  it("TC-FE-141: session.ready restarts the turn counter for a reconnected session", () => {
    apply(ready(), state("speaking", 5));

    // TR-175's automatic reconnect sends a fresh `session.start`, and the server answers with a
    // new session whose turns are numbered from zero again.
    apply(ready(), state("thinking", 0));

    expect(useSessionStore.getState().turnId).toBe(0);
    apply(agentSentence(0, 0, "answer from the reconnected session"));
    expect(events().at(-1)).toMatchObject({
      kind: "agent",
      text: "answer from the reconnected session",
    });
  });

  it("TC-FE-143: clearSession drops the session but keeps the user's toggles", () => {
    apply(ready(), state("thinking", 2), agentSentence(2, 0, "an answer"));
    useSessionStore.getState().updateSettings({ debug: true, ptt: true });

    useSessionStore.getState().clearSession();

    const store = useSessionStore.getState();
    expect(store.settings).toEqual({ ptt: true, debug: true, muted: false });
    expect(store.deck).toBeNull();
    expect(store.events).toEqual([]);
    expect(store.turnId).toBe(0);
    expect(store.currentSlide).toBe(1);

    // `reset()` remains the between-tests door and still clears the toggles too.
    useSessionStore.getState().updateSettings({ debug: true });
    useSessionStore.getState().reset();
    expect(useSessionStore.getState().settings).toEqual({ ptt: false, debug: false, muted: false });
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

    // One bubble per turn now, so the strike-through is per segment rather than
    // per event: segments are a synthesis device, not separate replies.
    const turns = events().filter((event): event is AgentLogEvent => event.kind === "agent");
    expect(turns).toHaveLength(1);
    expect(turns[0]?.segments.map((segment) => segment.cancelled)).toEqual([
      false,
      false,
      true,
      true,
    ]);
    // The turn as a whole is not cancelled, because two segments were heard.
    expect(turns[0]?.cancelled).toBe(false);
    expect(turns[0]?.text).toBe("sentence 0 sentence 1 sentence 2 sentence 3");
    expect(events().at(-1)).toMatchObject({ kind: "interrupt", heardSentences: 2 });
  });
});

describe("slide navigation", () => {
  it("TC-FE-002: clamps an out-of-range slide.goto into the deck", () => {
    apply(ready(makeDeck(DECK_SLIDE_COUNT)));

    apply(goto(0, 42, null, "hallucinated slide"));

    expect(useSessionStore.getState().currentSlide).toBe(DECK_SLIDE_COUNT);
  });

  it("TC-FE-142: a slide.goto from a superseded turn does not move the deck", () => {
    apply(ready(), state("speaking", 4), goto(4, 3, null, "answering about the latency budget"));
    expect(useSessionStore.getState().currentSlide).toBe(3);

    // The user cut in and turn 5 began. The navigation the dead turn asked for is the one message
    // that changes what the audience is looking at, so it is exactly the one that must be dropped.
    apply(state("thinking", 5));
    const before = events().length;
    apply(goto(4, 6, 1, "late tool call from the interrupted turn"));

    expect(useSessionStore.getState().currentSlide).toBe(3);
    expect(useSessionStore.getState().highlight).toBeNull();
    expect(events()).toHaveLength(before);

    // The live turn still moves the deck, so the guard is about the turn and not about the type.
    apply(goto(5, 2, 0, "the new question is about slide two"));
    expect(useSessionStore.getState().currentSlide).toBe(2);
    expect(useSessionStore.getState().highlight).toBe(0);
  });

  it("TC-FE-109: records the clamp as an event instead of failing (TR-133)", () => {
    apply(ready());
    const before = events().length;

    apply(goto(0, 0, 2, "off the front"));

    const added = events().slice(before);
    expect(added.map((event) => event.kind)).toEqual(["notice", "slide"]);
    // The clamp is debugging detail, not something the user must act on (PRD F10).
    expect(added[0]).toMatchObject({ kind: "notice", alert: false });
    expect(added[1]).toMatchObject({ kind: "slide", index: 1, highlight: 2 });
    expect(useSessionStore.getState().currentSlide).toBe(1);
    expect(useSessionStore.getState().highlight).toBe(2);

    // An in-range move logs the slide entry alone: the notice is the exception, not the rule.
    apply(goto(0, 3, null, "user asked about latency"));
    expect(events().at(-1)).toMatchObject({ kind: "slide", index: 3 });
    expect(events().filter((event) => event.kind === "notice")).toHaveLength(1);
  });
});

describe("notices", () => {
  it("TC-FE-144: a notice is quiet by default and can be marked as an alert", () => {
    useSessionStore.getState().logNotice("moved to slide 3 by hand");
    useSessionStore.getState().logNotice("connection lost", { alert: true });

    expect(events()[0]).toMatchObject({ kind: "notice", alert: false });
    expect(events()[1]).toMatchObject({ kind: "notice", alert: true });
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
    apply(goto(1, 99, null, "out of range"));

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

describe("rate limiting (TR-171)", () => {
  it("TC-FE-186: records when the provider said it would be ready", () => {
    const store = useSessionStore.getState();
    store.applyServerMessage({
      type: "error",
      code: "rate_limited",
      message: "rate limit reached; try again in 13.9s",
      recoverable: true,
      retry_after_s: 13.9,
    });

    const until = useSessionStore.getState().rateLimitedUntil;
    expect(until).not.toBeNull();
    // An instant, not a duration: about fourteen seconds from now.
    expect((until ?? 0) - Date.now()).toBeGreaterThan(12_000);
    expect((until ?? 0) - Date.now()).toBeLessThanOrEqual(14_000);
  });

  it("TC-FE-187: a failure that is not a rate limit sets no countdown", () => {
    useSessionStore.getState().applyServerMessage({
      type: "error",
      code: "llm_failed",
      message: "upstream refused",
      recoverable: true,
    });

    expect(useSessionStore.getState().rateLimitedUntil).toBeNull();
  });

  it("TC-FE-188: a second, shorter wait does not shorten the first", () => {
    const store = useSessionStore.getState();
    store.applyServerMessage({
      type: "error",
      code: "rate_limited",
      message: "long",
      recoverable: true,
      retry_after_s: 60,
    });
    const first = useSessionStore.getState().rateLimitedUntil;

    store.applyServerMessage({
      type: "error",
      code: "rate_limited",
      message: "short",
      recoverable: true,
      retry_after_s: 2,
    });

    expect(useSessionStore.getState().rateLimitedUntil).toBe(first);
  });

  it("TC-FE-189: the error is still logged like any other", () => {
    useSessionStore.getState().applyServerMessage({
      type: "error",
      code: "rate_limited",
      message: "rate limit reached",
      recoverable: true,
      retry_after_s: 5,
    });

    const errors = useSessionStore.getState().events.filter((entry) => entry.kind === "error");
    expect(errors).toHaveLength(1);
  });
});

describe("a substituted model (TR-085)", () => {
  it("TC-FE-214: records which model is answering, so the banner can say so", () => {
    useSessionStore.getState().applyServerMessage({
      type: "provider.fallback",
      turn_id: 1,
      stage: "llm",
      from_model: "qwen/qwen3.8-27b",
      to_model: "qwen2.5:7b",
      reason: "the hosted model is out of free-tier capacity",
      retry_after_s: 12,
    });

    expect(useSessionStore.getState().fallback).toEqual({
      fromModel: "qwen/qwen3.8-27b",
      toModel: "qwen2.5:7b",
      turnId: 1,
    });
  });

  it("TC-FE-215: logs it as its own kind, not as an error: the agent did answer", () => {
    useSessionStore.getState().applyServerMessage({
      type: "provider.fallback",
      turn_id: 1,
      stage: "llm",
      from_model: "qwen/qwen3.8-27b",
      to_model: "qwen2.5:7b",
      reason: "the hosted model is out of free-tier capacity",
    });

    const events = useSessionStore.getState().events;
    expect(events.map((entry) => entry.kind)).toEqual(["fallback"]);
    expect(events.filter((entry) => entry.kind === "error")).toEqual([]);
  });

  it("TC-FE-216: clearing the session forgets it, so the next one starts honest", () => {
    const store = useSessionStore.getState();
    store.applyServerMessage({
      type: "provider.fallback",
      turn_id: 1,
      stage: "llm",
      from_model: "a",
      to_model: "b",
      reason: "rate limit",
    });
    store.clearSession();

    expect(useSessionStore.getState().fallback).toBeNull();
  });
});

describe("the wait that comes with a substitution (TR-085)", () => {
  it("TC-FE-217: records when the usual model is back, so the banner can say so", () => {
    useSessionStore.getState().applyServerMessage({
      type: "provider.fallback",
      turn_id: 1,
      stage: "llm",
      from_model: "qwen/qwen3.8-27b",
      to_model: "qwen2.5:7b",
      reason: "rate limit",
      retry_after_s: 852,
    });

    const until = useSessionStore.getState().rateLimitedUntil;
    // A turn that fell back succeeds, so no `error` message ever carries this wait.
    expect((until ?? 0) - Date.now()).toBeGreaterThan(840_000);
  });

  it("TC-FE-218: a substitution with no stated wait leaves the countdown alone", () => {
    useSessionStore.getState().applyServerMessage({
      type: "provider.fallback",
      turn_id: 1,
      stage: "llm",
      from_model: "a",
      to_model: "b",
      reason: "rate limit",
    });

    expect(useSessionStore.getState().rateLimitedUntil).toBeNull();
  });
});

describe("the banner stops claiming a substitution that has ended (TR-085)", () => {
  /**
   * Announce that a turn fell back to the local model.
   *
   * @param turnId - The turn being answered.
   */
  function fellBackOn(turnId: number): void {
    useSessionStore.getState().applyServerMessage({
      type: "provider.fallback",
      turn_id: turnId,
      stage: "llm",
      from_model: "qwen/qwen3.8-27b",
      to_model: "qwen2.5:7b",
      reason: "rate limit",
      retry_after_s: 30,
    });
  }

  /**
   * End a turn.
   *
   * @param turnId - The turn that finished.
   */
  function turnEnded(turnId: number): void {
    useSessionStore.getState().applyServerMessage({
      type: "metrics",
      turn_id: turnId,
      stt_ms: null,
      llm_ttft_ms: 200,
      llm_total_ms: 900,
      tts_ttfb_ms: 300,
      sentences: 2,
    });
  }

  it("TC-FE-219: the banner survives the turn it is explaining", () => {
    fellBackOn(1);
    turnEnded(1);

    expect(useSessionStore.getState().fallback).not.toBeNull();
  });

  it("TC-FE-220: and goes once a later turn is answered by the usual model", () => {
    fellBackOn(1);
    turnEnded(1);
    turnEnded(2);

    expect(useSessionStore.getState().fallback).toBeNull();
  });

  it("TC-FE-221: a run of substituted turns keeps it up throughout", () => {
    fellBackOn(1);
    turnEnded(1);
    fellBackOn(2);
    turnEnded(2);

    expect(useSessionStore.getState().fallback?.turnId).toBe(2);
  });
});
