import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ClientMessage, ServerMessage } from "../protocol";
import { SessionClient, type ConnectionStatus, type GiveUpDetail } from "./client";

const RECONNECT_DELAY_MS = 10;
const SOCKET_CLOSED = 3;

/**
 * A WebSocket stand-in that records what was sent and lets a test drive the socket's events.
 *
 * It extends `EventTarget` so `addEventListener(..., { signal })` behaves exactly as it does for a
 * real socket — which is the mechanism `SessionClient.close()` relies on to remove its listeners.
 */
class FakeSocket extends EventTarget {
  static instances: FakeSocket[] = [];

  url: string;
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
    this.readyState = 1;
    this.dispatchEvent(new Event("open"));
  }

  /** Simulate an inbound text frame. */
  receiveText(payload: string): void {
    this.dispatchEvent(new MessageEvent("message", { data: payload }));
  }

  /** Simulate an inbound binary frame, as delivered with `binaryType = "arraybuffer"`. */
  receiveBinary(payload: ArrayBuffer): void {
    this.dispatchEvent(new MessageEvent("message", { data: payload }));
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

interface Harness {
  readonly client: SessionClient;
  readonly messages: ServerMessage[];
  readonly audio: ArrayBuffer[];
  readonly statuses: ConnectionStatus[];
  readonly invalid: string[];
  readonly giveUps: GiveUpDetail[];
}

/**
 * Build a client wired to `FakeSocket` with every callback recorded.
 *
 * @param url - Endpoint override; pass `null` to exercise the same-origin default.
 * @returns The client and the recorded callback arguments.
 */
function makeHarness(url: string | null = "ws://test.invalid/ws/session"): Harness {
  const messages: ServerMessage[] = [];
  const audio: ArrayBuffer[] = [];
  const statuses: ConnectionStatus[] = [];
  const invalid: string[] = [];
  const giveUps: GiveUpDetail[] = [];
  const client = new SessionClient({
    deckId: "anatomy_of_a_voice_agent",
    url: url ?? undefined,
    onMessage: (message) => messages.push(message),
    onAudio: (frame) => audio.push(frame),
    onStatus: (status) => statuses.push(status),
    onInvalidFrame: (detail) => invalid.push(detail),
    onGiveUp: (detail) => giveUps.push(detail),
    createSocket: (url) => new FakeSocket(url) as unknown as WebSocket,
    reconnectDelayMs: RECONNECT_DELAY_MS,
  });
  return { client, messages, audio, statuses, invalid, giveUps };
}

/**
 * Read a fake socket by creation order.
 *
 * @param index - Zero-based creation index.
 * @returns The socket.
 */
function socket(index: number): FakeSocket {
  const instance = FakeSocket.instances[index];
  if (!instance) {
    throw new Error(`no fake socket at index ${index}`);
  }
  return instance;
}

beforeEach(() => {
  FakeSocket.instances = [];
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("reconnect policy (TR-175)", () => {
  it("TC-FE-034: reconnects once after an abnormal close, with a fresh session.start", () => {
    const { client } = makeHarness();
    client.connect();
    socket(0).openFromServer();

    expect(socket(0).sentMessages).toEqual([
      {
        type: "session.start",
        deck_id: "anatomy_of_a_voice_agent",
        mode: "qa",
        client_ts: expect.any(Number),
      },
    ]);

    socket(0).closeFromServer(1006, "abnormal");
    // The retry is scheduled, not immediate: a synchronous reconnect would spin against a backend
    // that is still restarting.
    expect(FakeSocket.instances).toHaveLength(1);
    expect(client.status).toBe("reconnecting");

    vi.advanceTimersByTime(RECONNECT_DELAY_MS);
    expect(FakeSocket.instances).toHaveLength(2);

    socket(1).openFromServer();
    expect(socket(1).sentMessages).toHaveLength(1);
    expect(socket(1).sentMessages[0]).toMatchObject({ type: "session.start" });
    expect(client.status).toBe("connected");

    // Nothing further is scheduled once the reconnect has landed.
    vi.advanceTimersByTime(RECONNECT_DELAY_MS * 10);
    expect(FakeSocket.instances).toHaveLength(2);
  });

  it("TC-FE-112: gives up and reports after the automatic attempt also fails", () => {
    const { client, statuses, giveUps } = makeHarness();
    client.connect();
    socket(0).openFromServer();
    socket(0).closeFromServer(1006);
    vi.advanceTimersByTime(RECONNECT_DELAY_MS);

    socket(1).closeFromServer(1006, "still down");
    vi.advanceTimersByTime(RECONNECT_DELAY_MS * 10);

    expect(FakeSocket.instances).toHaveLength(2);
    expect(client.status).toBe("failed");
    expect(giveUps).toEqual([{ code: 1006, reason: "still down", attempts: 1 }]);
    expect(statuses).toEqual(["connecting", "connected", "reconnecting", "failed"]);

    // The retry button is the only way out, and it restores the allowance.
    client.retry();
    expect(FakeSocket.instances).toHaveLength(3);
  });

  it("TC-FE-113: close() removes every listener, cancels the retry, and never reconnects", () => {
    const { client, statuses } = makeHarness();
    client.connect();
    socket(0).openFromServer();
    socket(0).closeFromServer(1006);
    expect(client.status).toBe("reconnecting");

    client.close();

    expect(socket(0).closeCalls).toEqual([]);
    expect(client.status).toBe("closed");

    // Neither the pending timer nor a late close event may resurrect the session.
    vi.advanceTimersByTime(RECONNECT_DELAY_MS * 10);
    socket(0).closeFromServer(1006);
    vi.advanceTimersByTime(RECONNECT_DELAY_MS * 10);

    expect(FakeSocket.instances).toHaveLength(1);
    expect(statuses).toEqual(["connecting", "connected", "reconnecting", "closed"]);
    expect(client.send({ type: "interrupt.cancel" })).toBe(false);
  });

  it("TC-FE-114: an explicit close of an open socket is normal, so no retry is due", () => {
    const { client } = makeHarness();
    client.connect();
    socket(0).openFromServer();

    client.close();

    expect(socket(0).closeCalls).toEqual([{ code: 1000, reason: "client closed" }]);
    expect(FakeSocket.instances).toHaveLength(1);
  });
});

describe("frame dispatch", () => {
  it("TC-FE-115: parses text frames, drops unknown ones, and passes binary through", () => {
    const { client, messages, audio, invalid } = makeHarness(null);
    client.connect();

    // With no override the endpoint comes from the page's own origin, which is what lets the Vite
    // proxy work in development and the same build work anywhere else (config.websocketUrl).
    expect(socket(0).url).toBe(`ws://${window.location.host}/ws/session`);
    expect(socket(0).binaryType).toBe("arraybuffer");
    socket(0).openFromServer();

    socket(0).receiveText('{"type":"state","value":"speaking","turn_id":3,"server_ts":9}');
    socket(0).receiveText('{"type":"transcript.alien","turn_id":3}');
    socket(0).receiveText("{oops");
    const frame = new Uint8Array([1, 0, 0, 0, 0, 0, 0, 0]).buffer;
    socket(0).receiveBinary(frame);

    expect(messages).toEqual([{ type: "state", value: "speaking", turn_id: 3, server_ts: 9 }]);
    expect(invalid).toHaveLength(2);
    expect(audio).toEqual([frame]);
  });
});
