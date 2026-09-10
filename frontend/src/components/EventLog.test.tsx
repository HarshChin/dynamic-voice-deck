import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { EventsExport, LogEvent, MetricsSample } from "../store";

import { EventLog } from "./EventLog";

/** `Omit` collapses a union into its common members, so distribute it by hand. */
type DistributiveOmit<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never;

let nextId = 0;

/**
 * Stamp a draft entry the way the store would.
 *
 * @param draft - The entry without its identity fields.
 * @returns A complete log entry with a unique id.
 */
function entry(draft: DistributiveOmit<LogEvent, "id" | "clientTs" | "message">): LogEvent {
  nextId += 1;
  return { ...draft, id: nextId, clientTs: 1_700_000_000_000 + nextId, message: null };
}

/**
 * Build a metrics sample.
 *
 * @param turnId - The turn the sample belongs to.
 * @returns A sample with the two server stages Phase 1 can actually measure.
 */
function sample(turnId: number): MetricsSample {
  return {
    turnId,
    sttMs: null,
    llmTtftMs: 210,
    llmTotalMs: 640,
    ttsTtfbMs: null,
    firstAudioMs: null,
    interruptStopMs: null,
    sentences: 3,
  };
}

/** An empty envelope, for the tests that do not care what "Copy log" copied. */
const EMPTY_EXPORT: EventsExport = {
  session_id: "sess-1",
  deck_id: "anatomy_of_a_voice_agent",
  started_at: "2026-09-10T09:00:00.000Z",
  events: [],
};

/**
 * Replace `navigator.clipboard` for one test.
 *
 * `vi.stubGlobal` cannot reach it — the property lives on the prototype and the DOM types insist
 * it is always present — so define it directly and hand back the restore.
 *
 * @param writeText - The stub to install.
 * @returns A function that removes the stub again.
 */
function stubClipboard(writeText: () => Promise<void>): () => void {
  const descriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  return () => {
    if (descriptor === undefined) {
      Reflect.deleteProperty(navigator, "clipboard");
    } else {
      Object.defineProperty(navigator, "clipboard", descriptor);
    }
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("EventLog", () => {
  it("TC-FE-128: renders one entry per kind, in arrival order", () => {
    const events: readonly LogEvent[] = [
      entry({
        kind: "session",
        sessionId: "sess-1",
        deckTitle: "Anatomy of a Voice Agent",
        providers: { stt: "groq", llm: "groq", tts: "kokoro" },
      }),
      entry({ kind: "state", turnId: 1, from: "listening", to: "thinking", elapsedMs: 312 }),
      entry({ kind: "user", turnId: 1, text: "how do you handle interruptions?" }),
      entry({
        kind: "tool",
        turnId: 1,
        name: "go_to_slide",
        args: { index: 4, reason: "asked about interruption handling" },
        source: "llm",
      }),
      entry({ kind: "slide", index: 4, highlight: null, reason: "barge-in question" }),
      entry({ kind: "agent", turnId: 1, sentenceId: 0, text: "Two tiers.", cancelled: false }),
      entry({ kind: "interrupt", turnId: 1, heardSentences: 2 }),
      entry({ kind: "error", code: "llm_failed", text: "upstream said 503", recoverable: true }),
    ];

    render(
      <EventLog
        events={events}
        debug={false}
        onDebugChange={vi.fn()}
        exportEvents={() => EMPTY_EXPORT}
      />,
    );

    expect(screen.getByText("Anatomy of a Voice Agent")).toBeInTheDocument();
    expect(screen.getByText("listening → thinking")).toBeInTheDocument();
    expect(screen.getByText("312 ms")).toBeInTheDocument();
    expect(screen.getByText("how do you handle interruptions?")).toBeInTheDocument();
    // Twice: the model's tool call and the deck move it produced.
    expect(screen.getAllByText("Slide 4")).toHaveLength(2);
    expect(screen.getByText("asked about interruption handling")).toBeInTheDocument();
    expect(screen.getByText("barge-in question")).toBeInTheDocument();
    expect(screen.getByText("Two tiers.")).toBeInTheDocument();
    expect(screen.getByText("Interrupted after 2 sentences")).toBeInTheDocument();
    expect(screen.getByText("llm_failed")).toBeInTheDocument();
    expect(screen.getByText("upstream said 503")).toBeInTheDocument();

    const kinds = screen.getAllByRole("listitem").map((item) => item.dataset.kind);
    expect(kinds).toEqual([
      "session",
      "state",
      "user",
      "tool",
      "slide",
      "agent",
      "interrupt",
      "error",
    ]);
  });

  it("TC-FE-129: tells a model tool call apart from a keyword fallback", () => {
    // TR-062: the fallback exists because the model sometimes answers about another slide without
    // calling the tool. A demo that renders both the same way hides exactly the thing worth seeing.
    const events: readonly LogEvent[] = [
      entry({
        kind: "tool",
        turnId: 1,
        name: "go_to_slide",
        args: { index: 4, reason: "user asked about interruption handling" },
        source: "llm",
      }),
      entry({
        kind: "tool",
        turnId: 2,
        name: "go_to_slide",
        args: { index: 2, keyword: "latency" },
        source: "fallback",
      }),
    ];

    render(
      <EventLog
        events={events}
        debug={false}
        onDebugChange={vi.fn()}
        exportEvents={() => EMPTY_EXPORT}
      />,
    );

    const [first, second] = screen.getAllByRole("listitem");
    expect(first?.querySelector("[data-source]")).toHaveAttribute("data-source", "llm");
    expect(second?.querySelector("[data-source]")).toHaveAttribute("data-source", "fallback");
    expect(first).toHaveTextContent("model");
    expect(second).toHaveTextContent("keyword");
    expect(second).toHaveTextContent("latency");
  });

  it("TC-FE-130: strikes through the sentences the user never heard", () => {
    const events: readonly LogEvent[] = [
      entry({ kind: "agent", turnId: 1, sentenceId: 0, text: "heard this", cancelled: false }),
      entry({ kind: "agent", turnId: 1, sentenceId: 2, text: "never heard this", cancelled: true }),
    ];

    render(
      <EventLog
        events={events}
        debug={false}
        onDebugChange={vi.fn()}
        exportEvents={() => EMPTY_EXPORT}
      />,
    );

    expect(screen.getByText("heard this")).not.toHaveAttribute("data-cancelled");
    expect(screen.getByText("never heard this")).toHaveAttribute("data-cancelled", "true");
  });

  it("TC-FE-131: keeps metrics and client notices behind the debug toggle", () => {
    const events: readonly LogEvent[] = [
      entry({ kind: "user", turnId: 1, text: "what is this?" }),
      entry({ kind: "metrics", turnId: 1, sample: sample(1) }),
      entry({ kind: "notice", text: "slide 42 is outside the deck (1-6); showing 6" }),
    ];
    const onDebugChange = vi.fn();

    const view = render(
      <EventLog
        events={events}
        debug={false}
        onDebugChange={onDebugChange}
        exportEvents={() => EMPTY_EXPORT}
      />,
    );

    expect(screen.queryByText(/ttft 210ms/)).not.toBeInTheDocument();
    expect(screen.queryByText(/outside the deck/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("debug"));
    expect(onDebugChange).toHaveBeenCalledExactlyOnceWith(true);

    view.rerender(
      <EventLog
        events={events}
        debug
        onDebugChange={onDebugChange}
        exportEvents={() => EMPTY_EXPORT}
      />,
    );

    expect(screen.getByText(/ttft 210ms/)).toHaveTextContent("llm 640ms");
    expect(screen.getByText(/outside the deck/)).toBeInTheDocument();
  });

  it("TC-FE-132: copies the replayable envelope and says so", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    const restore = stubClipboard(writeText);
    const exported: EventsExport = {
      session_id: "sess-1",
      deck_id: "anatomy_of_a_voice_agent",
      started_at: "2026-09-10T09:00:00.000Z",
      events: [{ type: "transcript.user", turn_id: 1, text: "hello", client_ts: 1 }],
    };

    try {
      render(
        <EventLog
          events={[entry({ kind: "user", turnId: 1, text: "hello" })]}
          debug={false}
          onDebugChange={vi.fn()}
          exportEvents={() => exported}
        />,
      );

      fireEvent.click(screen.getByRole("button", { name: "Copy log" }));

      await waitFor(() => {
        expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();
      });
      expect(writeText).toHaveBeenCalledExactlyOnceWith(JSON.stringify(exported, null, 2));
    } finally {
      restore();
    }
  });

  it("reports a clipboard the browser refused rather than pretending it worked", async () => {
    const restore = stubClipboard(() => Promise.reject(new Error("not allowed")));

    try {
      render(
        <EventLog
          events={[]}
          debug={false}
          onDebugChange={vi.fn()}
          exportEvents={() => EMPTY_EXPORT}
        />,
      );

      fireEvent.click(screen.getByRole("button", { name: "Copy log" }));

      expect(await screen.findByRole("button", { name: "Copy failed" })).toBeInTheDocument();
    } finally {
      restore();
    }
  });

  it("invites the first question when nothing has happened yet", () => {
    render(
      <EventLog
        events={[]}
        debug={false}
        onDebugChange={vi.fn()}
        exportEvents={() => EMPTY_EXPORT}
      />,
    );

    expect(screen.getByText(/Nothing yet/)).toBeInTheDocument();
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
  });
});
