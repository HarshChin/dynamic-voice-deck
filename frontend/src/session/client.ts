/**
 * `SessionClient` — the only thing in the app that owns a WebSocket.
 *
 * It converts the socket's untyped event stream into three typed callbacks (`onMessage`,
 * `onAudio`, `onStatus`), sends typed `ClientMessage`s, and implements the single automatic
 * reconnect required by TR-175. It deliberately holds no React or store imports so it can be
 * unit-tested against a fake socket with no DOM beyond `EventTarget`.
 */

import { RECONNECT, websocketUrl } from "../config";
import {
  parseServerMessage,
  type ClientMessage,
  type ServerMessage,
  type SessionMode,
} from "../protocol";

/** Lifecycle of the connection, as the UI understands it. */
export type ConnectionStatus =
  "idle" | "connecting" | "connected" | "reconnecting" | "closed" | "failed";

/** `WebSocket.readyState` values, named so the comparisons read as intent. */
const SOCKET_CONNECTING = 0;
const SOCKET_OPEN = 1;

/** RFC 6455 normal closure. Anything else is abnormal and triggers the TR-175 retry. */
const NORMAL_CLOSURE = 1000;

/** Why a give-up happened, so the UI can explain itself next to the retry button. */
export interface GiveUpDetail {
  readonly code: number;
  readonly reason: string;
  /** Number of automatic attempts spent before giving up. */
  readonly attempts: number;
}

/** Everything `SessionClient` needs from its owner. */
export interface SessionClientOptions {
  /** Deck to open, sent in every `session.start` including the one after a reconnect. */
  readonly deckId: string;
  /** Q&A or unprompted walkthrough; defaults to `"qa"`. */
  readonly mode?: SessionMode;
  /** Endpoint override; defaults to the page's own origin (see `config.websocketUrl`). */
  readonly url?: string;
  /** Called for every well-formed server message. */
  readonly onMessage: (message: ServerMessage) => void;
  /** Called for every binary frame, still framed (see `decodeAudioFrame`). Phase 2. */
  readonly onAudio?: (frame: ArrayBuffer) => void;
  /** Called whenever the connection status changes. */
  readonly onStatus?: (status: ConnectionStatus) => void;
  /** Called when an inbound frame could not be understood; the frame is then dropped. */
  readonly onInvalidFrame?: (detail: string) => void;
  /** Called once the automatic reconnect allowance is spent (TR-175). */
  readonly onGiveUp?: (detail: GiveUpDetail) => void;
  /** Socket constructor seam for tests; defaults to the global `WebSocket`. */
  readonly createSocket?: (url: string) => WebSocket;
  /** Delay before the automatic reconnect; defaults to `RECONNECT.delayMs`. */
  readonly reconnectDelayMs?: number;
}

/**
 * A WebSocket session against the backend.
 *
 * One instance may be reused across sessions: `connect()` tears down any previous socket first and
 * resets the reconnect allowance. `close()` is idempotent and guarantees no further callback fires.
 */
export class SessionClient {
  readonly #options: SessionClientOptions;
  #socket: WebSocket | null = null;
  /** Aborting this removes every listener attached to the current socket in one step. */
  #listeners: AbortController | null = null;
  #reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  #attemptsUsed = 0;
  #closedByUs = false;
  #status: ConnectionStatus = "idle";

  /**
   * Create a client. No socket is opened until `connect()` is called.
   *
   * @param options - Callbacks and connection parameters.
   */
  constructor(options: SessionClientOptions) {
    this.#options = options;
  }

  /** Current connection status. */
  get status(): ConnectionStatus {
    return this.#status;
  }

  /** Whether a message sent right now would reach the server. */
  get isOpen(): boolean {
    return this.#socket !== null && this.#socket.readyState === SOCKET_OPEN;
  }

  /**
   * Open a session, replacing any socket this client already holds.
   *
   * Resets the automatic reconnect allowance, so a user-initiated start is never refused because
   * an earlier session exhausted it.
   */
  connect(): void {
    this.close();
    this.#closedByUs = false;
    this.#attemptsUsed = 0;
    this.#openSocket();
  }

  /**
   * Reconnect after the automatic allowance was spent — the UI's retry button (TR-175).
   *
   * Identical to `connect()`; the separate name keeps the caller's intent visible in the event log.
   */
  retry(): void {
    this.connect();
  }

  /**
   * Send a typed message as a JSON text frame.
   *
   * @param message - The message to send.
   * @returns `true` when it was handed to an open socket, `false` when there was nowhere to send
   *   it. Callers that must not lose a message should check the result rather than assume.
   */
  send(message: ClientMessage): boolean {
    if (!this.isOpen || this.#socket === null) {
      return false;
    }
    this.#socket.send(JSON.stringify(message));
    return true;
  }

  /**
   * Send one utterance as a binary frame (raw PCM16 LE, 16 kHz mono, TRD §6.1).
   *
   * @param pcm16 - The samples, which must already have been announced by a `speech.end` (TR-140).
   * @returns `true` when it was handed to an open socket.
   */
  sendAudio(pcm16: ArrayBuffer): boolean {
    if (!this.isOpen || this.#socket === null) {
      return false;
    }
    this.#socket.send(pcm16);
    return true;
  }

  /**
   * Close the session and release everything.
   *
   * Listeners are removed *before* the socket is closed, so the resulting close event cannot start
   * a reconnect. Safe to call when nothing is open.
   *
   * @param code - Close code; the default marks a deliberate, normal closure.
   * @param reason - Human-readable reason recorded on the socket.
   */
  close(code: number = NORMAL_CLOSURE, reason = "client closed"): void {
    this.#closedByUs = true;
    this.#clearReconnectTimer();
    const socket = this.#socket;
    this.#detachSocket();
    if (
      socket !== null &&
      (socket.readyState === SOCKET_OPEN || socket.readyState === SOCKET_CONNECTING)
    ) {
      socket.close(code, reason);
    }
    if (this.#status !== "idle") {
      this.#setStatus("closed");
    }
  }

  /** Open a socket and wire its listeners to this client. */
  #openSocket(): void {
    const url = this.#options.url ?? websocketUrl();
    const socket = this.#options.createSocket
      ? this.#options.createSocket(url)
      : new WebSocket(url);
    // Without this, browsers hand binary frames over as `Blob`, which cannot be read synchronously.
    socket.binaryType = "arraybuffer";

    const listeners = new AbortController();
    const { signal } = listeners;
    socket.addEventListener("open", () => this.#handleOpen(), { signal });
    socket.addEventListener("message", (event: MessageEvent) => this.#handleMessage(event), {
      signal,
    });
    // No "error" listener: the event carries no detail and is always followed by a close, which is
    // where the reconnect policy lives. Listening for both would report one failure twice.
    socket.addEventListener("close", (event: CloseEvent) => this.#handleClose(event), { signal });

    this.#socket = socket;
    this.#listeners = listeners;
    this.#setStatus(this.#attemptsUsed > 0 ? "reconnecting" : "connecting");
  }

  /** Announce the session as soon as the socket opens; `session.start` must be first (TRD §6). */
  #handleOpen(): void {
    this.#setStatus("connected");
    this.send({
      type: "session.start",
      deck_id: this.#options.deckId,
      mode: this.#options.mode ?? "qa",
      // Epoch seconds, matching the backend's `time.time()` convention for `client_ts`.
      client_ts: Date.now() / 1000,
    });
  }

  /** Route one inbound frame to the right callback, dropping anything unrecognised. */
  #handleMessage(event: MessageEvent): void {
    const data = event.data as unknown;
    if (typeof data === "string") {
      const message = parseServerMessage(data);
      if (message === null) {
        this.#options.onInvalidFrame?.(data);
        return;
      }
      this.#options.onMessage(message);
      return;
    }
    if (data instanceof ArrayBuffer) {
      this.#options.onAudio?.(data);
      return;
    }
    this.#options.onInvalidFrame?.("binary frame of unexpected type");
  }

  /** Apply the TR-175 reconnect policy to a closed socket. */
  #handleClose(event: CloseEvent): void {
    this.#detachSocket();
    if (this.#closedByUs) {
      return;
    }
    if (event.code === NORMAL_CLOSURE) {
      this.#setStatus("closed");
      return;
    }
    const allowance = RECONNECT.maxAutomaticAttempts;
    if (this.#attemptsUsed >= allowance) {
      this.#setStatus("failed");
      this.#options.onGiveUp?.({
        code: event.code,
        reason: event.reason,
        attempts: this.#attemptsUsed,
      });
      return;
    }
    this.#attemptsUsed += 1;
    this.#setStatus("reconnecting");
    const delay = this.#options.reconnectDelayMs ?? RECONNECT.delayMs;
    this.#reconnectTimer = setTimeout(() => {
      this.#reconnectTimer = null;
      this.#openSocket();
    }, delay);
  }

  /** Drop the current socket's listeners and forget it, without closing it. */
  #detachSocket(): void {
    this.#listeners?.abort();
    this.#listeners = null;
    this.#socket = null;
  }

  /** Cancel a pending reconnect, if any. */
  #clearReconnectTimer(): void {
    if (this.#reconnectTimer !== null) {
      clearTimeout(this.#reconnectTimer);
      this.#reconnectTimer = null;
    }
  }

  /** Publish a status change, suppressing repeats so the store does not churn. */
  #setStatus(status: ConnectionStatus): void {
    if (this.#status === status) {
      return;
    }
    this.#status = status;
    this.#options.onStatus?.(status);
  }
}
