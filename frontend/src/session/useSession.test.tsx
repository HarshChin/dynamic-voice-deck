import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode, type JSX } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SlideDeck } from "../components/SlideDeck";
import type { ClientMessage, Deck, ServerMessage } from "../protocol";
import { useSessionStore } from "../store";

import { useSession } from "./useSession";

const SOCKET_URL = "ws://test.invalid/ws/session";
/** `RECONNECT.delayMs`; the hook does not expose the seam `SessionClient` has for it. */
const RECONNECT_DELAY_MS = 500;
const DECK_ID = "anatomy_of_a_voice_agent";
const SOCKET_OPEN = 1;
const SOCKET_CLOSED = 3;

/**
 * A WebSocket stand-in that records what was sent and lets a test drive the socket's events.
 *
 * It extends `EventTarget` so `addEventListener(..., { signal })` behaves as it does for a real
 * socket, which is the mechanism `SessionClient` relies on to release its listeners.
 */
class FakeSocket extends EventTarget {
  static instances: FakeSocket[] = [];

  readonly url: string;
  binaryType: BinaryType = "blob";
  readyState = 0;
  readonly sent: unknown[] = [];
  readonly closeCalls: { code?: number; reason?: string }[] = [];

  constructor(url: string) {
    super();
    this.url = url;
    FakeSocket.instances.push(this);
  }

  send(data: unknown): void {
    this.sent.push(data);
  }

  close(code?: number, reason?: string): void {
    this.closeCalls.push({ code, reason });
    this.readyState = SOCKET_CLOSED;
  }

  /** Simulate the handshake completing. */
  openFromServer(): void {
    this.readyState = SOCKET_OPEN;
    this.dispatchEvent(new Event("open"));
  }

  /** Simulate an inbound text frame. */
  receive(message: ServerMessage): void {
    this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(message) }));
  }

  /** Simulate the socket closing from the other end. */
  closeFromServer(code: number, reason = ""): void {
    this.readyState = SOCKET_CLOSED;
    this.dispatchEvent(new CloseEvent("close", { code, reason, wasClean: code === 1000 }));
  }

  /** The messages sent as parsed JSON, for readable assertions. */
  get sentMessages(): ClientMessage[] {
    return this.sent
      .filter((item): item is string => typeof item === "string")
      .map((item) => JSON.parse(item) as ClientMessage);
  }
}

/** Module-level so the hook's callbacks keep a stable identity across renders. */
function createSocket(url: string): WebSocket {
  return new FakeSocket(url) as unknown as WebSocket;
}

/**
 * Build a deck fixture matching the backend's validation rules.
 *
 * @returns A six-slide deck.
 */
function makeDeck(): Deck {
  return {
    id: DECK_ID,
    title: "Anatomy of a Voice Agent",
    voice: "af_heart",
    slides: Array.from({ length: 6 }, (_, i) => ({
      index: i + 1,
      title: `Chapter ${String(i + 1)}`,
      bullets: [`point ${String(i + 1)}a`, `point ${String(i + 1)}b`],
      notes: "Speaker notes.",
      aliases: [`slide-${String(i + 1)}`],
    })),
  };
}

/**
 * Stand in for the HTTP deck endpoints (TRD §4.10).
 *
 * @param deck - The deck the listing and the detail endpoint both serve.
 */
function stubDeckApi(deck: Deck): void {
  const listing = [{ id: deck.id, title: deck.title, slide_count: deck.slides.length }];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: string) => {
      const body: unknown = input === "/api/decks" ? listing : deck;
      const ok = input === "/api/decks" || input === `/api/decks/${deck.id}`;
      return Promise.resolve({
        ok,
        json: () => Promise.resolve(body),
      } as unknown as Response);
    }),
  );
}

/**
 * Build a `session.ready` for a deck.
 *
 * @param deck - The deck the session presents.
 * @returns The message.
 */
function ready(deck: Deck): ServerMessage {
  return {
    type: "session.ready",
    session_id: "sess-1",
    protocol_version: 1,
    deck,
    providers: { stt: "groq", llm: "groq", tts: "kokoro" },
  };
}

/**
 * A component that exposes every part of the hook a test needs to reach.
 *
 * It renders the real `SlideDeck` so the keyboard path under test is the one the app ships, not a
 * synthetic call to `goToSlide`.
 *
 * @returns The harness element.
 */
function Harness(): JSX.Element {
  const session = useSession({ url: SOCKET_URL, createSocket });
  return (
    <div>
      <button type="button" onClick={session.start}>
        start
      </button>
      <button type="button" onClick={session.stop}>
        stop
      </button>
      <button
        type="button"
        onClick={() => {
          session.sendText("   what about latency?   ");
        }}
      >
        ask
      </button>
      <button
        type="button"
        onClick={() => {
          session.sendText("x".repeat(600));
        }}
      >
        ask-long
      </button>
      <p data-testid="connection">{session.connection}</p>
      <p data-testid="answering">{session.isAnswering ? "yes" : "no"}</p>
      <p data-testid="orb">{session.orbState}</p>
      <p data-testid="deck">{session.deck?.title ?? "none"}</p>
      <SlideDeck
        slides={session.deck?.slides ?? []}
        current={session.currentSlide}
        highlight={session.highlight}
        onNavigate={session.goToSlide}
      />
    </div>
  );
}

/**
 * Click "start", complete the handshake, and admit the session.
 *
 * @param deck - The deck `session.ready` carries.
 * @returns The socket the client opened.
 */
function openSession(deck: Deck): FakeSocket {
  fireEvent.click(screen.getByRole("button", { name: "start" }));
  const socket = FakeSocket.instances.at(-1);
  if (socket === undefined) {
    throw new Error("start() did not open a socket");
  }
  act(() => {
    socket.openFromServer();
  });
  act(() => {
    socket.receive(ready(deck));
  });
  return socket;
}

describe("useSession", () => {
  beforeEach(() => {
    FakeSocket.instances = [];
    useSessionStore.getState().reset();
    stubDeckApi(makeDeck());
  });

  afterEach(() => {
    useSessionStore.getState().reset();
    vi.unstubAllGlobals();
  });

  it("TC-FE-003: reports a manual slide change to the agent as source `user`", async () => {
    const deck = makeDeck();
    render(<Harness />);
    const socket = openSession(deck);
    await screen.findByText("Chapter 1");

    fireEvent.keyDown(window, { key: "ArrowRight" });

    expect(socket.sentMessages.at(-1)).toEqual({
      type: "slide.changed",
      index: 2,
      source: "user",
    });
    expect(useSessionStore.getState().currentSlide).toBe(2);
    await screen.findByText("Chapter 2");
  });

  it("TC-FE-136: sends a typed question as `text.input`, trimmed and capped", () => {
    const deck = makeDeck();
    render(<Harness />);
    const socket = openSession(deck);

    fireEvent.click(screen.getByRole("button", { name: "ask" }));
    expect(socket.sentMessages.at(-1)).toEqual({
      type: "text.input",
      text: "what about latency?",
    });

    fireEvent.click(screen.getByRole("button", { name: "ask-long" }));
    const last = socket.sentMessages.at(-1);
    expect(last?.type).toBe("text.input");
    // MAX_TEXT_INPUT_CHARS; sending more would be rejected by the backend's validation.
    expect(last && "text" in last ? last.text : "").toHaveLength(500);
  });

  it("TC-FE-137: closes its socket on unmount and ignores anything that arrives after", async () => {
    // React 19 double-invokes effects in development. The socket is opened by the click handler,
    // never by an effect, so a StrictMode mount must still leave exactly one socket behind.
    const deck = makeDeck();
    const view = render(
      <StrictMode>
        <Harness />
      </StrictMode>,
    );
    const socket = openSession(deck);
    await screen.findByText("Chapter 1");
    expect(FakeSocket.instances).toHaveLength(1);

    act(() => {
      socket.receive({ type: "state", value: "listening", turn_id: 0, server_ts: 1 });
    });
    expect(useSessionStore.getState().agentState).toBe("listening");

    view.unmount();

    expect(socket.closeCalls).toEqual([{ code: 1000, reason: "client closed" }]);
    socket.receive({ type: "state", value: "thinking", turn_id: 1, server_ts: 2 });
    expect(useSessionStore.getState().agentState).toBe("listening");
  });

  it("TC-FE-138: pushes server messages into the store and follows them with the orb", async () => {
    const deck = makeDeck();
    render(<Harness />);
    expect(screen.getByTestId("orb")).toHaveTextContent("idle");

    const socket = openSession(deck);
    expect(screen.getByTestId("connection")).toHaveTextContent("connected");
    expect(socket.sentMessages[0]).toMatchObject({ type: "session.start", deck_id: DECK_ID });

    act(() => {
      socket.receive({ type: "state", value: "thinking", turn_id: 1, server_ts: 1 });
    });
    expect(screen.getByTestId("orb")).toHaveTextContent("thinking");

    act(() => {
      socket.receive({
        type: "slide.goto",
        turn_id: 1,
        index: 3,
        highlight: 1,
        reason: "asked about the latency budget",
      });
    });
    await screen.findByText("Chapter 3");
    expect(screen.getByText("point 3b").closest("li")).toHaveAttribute("data-highlighted", "true");

    fireEvent.click(screen.getByRole("button", { name: "stop" }));
    // PRD F2: ending a session returns the orb to idle and leaves the transcript in place.
    expect(screen.getByTestId("orb")).toHaveTextContent("idle");
    expect(useSessionStore.getState().events.length).toBeGreaterThan(0);
  });

  it("TC-FE-148: starting a session keeps the user's toggles", () => {
    render(<Harness />);
    act(() => {
      useSessionStore.getState().updateSettings({ debug: true, ptt: true });
    });

    openSession(makeDeck());

    // The session's own state is cleared, but the toggles are the user's (PRD F13).
    expect(useSessionStore.getState().settings).toEqual({ ptt: true, debug: true, muted: false });
    expect(useSessionStore.getState().deck?.id).toBe(DECK_ID);
  });

  it("TC-FE-149: explains a connection it has given up on where the user can see it", () => {
    vi.useFakeTimers();
    try {
      render(<Harness />);
      const first = openSession(makeDeck());

      act(() => {
        first.closeFromServer(1006, "abnormal");
      });
      // TR-175 allows exactly one silent retry.
      act(() => {
        vi.advanceTimersByTime(RECONNECT_DELAY_MS);
      });
      expect(FakeSocket.instances).toHaveLength(2);
      const second = FakeSocket.instances[1]!;

      act(() => {
        second.closeFromServer(1006, "abnormal");
      });

      expect(screen.getByTestId("orb")).toHaveTextContent("error");
      expect(useSessionStore.getState().events.at(-1)).toMatchObject({
        kind: "notice",
        // The orb has just turned amber and pointed at the log, so this sentence has to be
        // readable without the debug toggle.
        alert: true,
        text: expect.stringContaining("connection lost (code 1006)"),
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("TC-FE-150: refuses a second question while the agent is still answering", () => {
    render(<Harness />);
    const socket = openSession(makeDeck());

    fireEvent.click(screen.getByRole("button", { name: "ask" }));
    expect(socket.sentMessages.at(-1)).toMatchObject({ type: "text.input" });

    act(() => {
      socket.receive({ type: "state", value: "thinking", turn_id: 1, server_ts: 2 });
    });
    expect(screen.getByTestId("answering")).toHaveTextContent("yes");

    const sentBefore = socket.sentMessages.length;
    fireEvent.click(screen.getByRole("button", { name: "ask" }));

    // The server would cancel the running turn without announcing it (TR-022), leaving the first
    // answer in the transcript looking finished, so the question is refused rather than sent.
    expect(socket.sentMessages).toHaveLength(sentBefore);
    expect(useSessionStore.getState().events.at(-1)).toMatchObject({
      kind: "notice",
      text: expect.stringContaining("still answering"),
    });

    act(() => {
      socket.receive({ type: "state", value: "listening", turn_id: 1, server_ts: 3 });
    });
    expect(screen.getByTestId("answering")).toHaveTextContent("no");
    fireEvent.click(screen.getByRole("button", { name: "ask" }));
    expect(socket.sentMessages).toHaveLength(sentBefore + 1);
  });

  it("shows the deck and navigates it before any session is open (PRD F1)", async () => {
    render(<Harness />);

    await waitFor(() => {
      expect(screen.getByTestId("deck")).toHaveTextContent("Anatomy of a Voice Agent");
    });
    fireEvent.keyDown(window, { key: "ArrowRight" });

    expect(useSessionStore.getState().currentSlide).toBe(2);
    expect(FakeSocket.instances).toHaveLength(0);
  });

  it("refuses to send a question with no session, and says so in the log", () => {
    render(<Harness />);

    fireEvent.click(screen.getByRole("button", { name: "ask" }));

    const events = useSessionStore.getState().events;
    expect(events.at(-1)).toMatchObject({
      kind: "notice",
      text: expect.stringContaining("no session"),
    });
    expect(FakeSocket.instances).toHaveLength(0);
  });

  it("survives a backend that is not running", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))),
    );
    render(<Harness />);

    await waitFor(() => {
      expect(screen.getByTestId("deck")).toHaveTextContent("none");
    });
    expect(screen.getByText("No slide to show.")).toBeInTheDocument();
  });
});
