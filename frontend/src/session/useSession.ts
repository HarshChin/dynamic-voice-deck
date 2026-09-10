/**
 * `useSession` — the one place React and the WebSocket meet.
 *
 * `SessionClient` owns the socket and `useSessionStore` owns the state; this hook is the wiring
 * between them plus the handful of commands the UI issues (start, stop, ask, navigate). Keeping
 * that wiring in a hook rather than in a component means the socket's lifetime is tied to a
 * mount/unmount pair, which is what makes "start and stop five times without leaking" testable
 * (TR-103, PRD F2).
 *
 * Phase 1 has no audio: the only way in is `text.input` (PRD F13), and the only way out is the
 * transcript. The seams the audio pipeline will use — client callbacks, store actions — are
 * already the ones used here, so Phase 2 adds callers rather than rewriting this file.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { API_BASE_PATH } from "../config";
import { MAX_TEXT_INPUT_CHARS, type Deck, type SessionMode, type SessionState } from "../protocol";
import { useSessionStore } from "../store";

import { SessionClient, type ConnectionStatus } from "./client";

/** Deck served when the listing is unavailable, matching `backend/app/decks/`. */
const DEFAULT_DECK_ID = "anatomy_of_a_voice_agent";

/** `GET /api/decks` and `GET /api/decks/{id}` (TRD §4.10). */
const DECKS_PATH = `${API_BASE_PATH}/decks`;

/** How much of an unreadable frame to quote in the event log before it stops being useful. */
const FRAME_EXCERPT_CHARS = 120;

/**
 * One row of `GET /api/decks`.
 *
 * Wire field names, like the protocol messages: this is what `JSON.parse` produces, and renaming
 * it here would put a translation layer between the backend's schema and the picker.
 */
export interface DeckSummary {
  readonly id: string;
  readonly title: string;
  readonly slide_count: number;
}

/** Optional seams; every one of them has a working default. */
export interface UseSessionOptions {
  /** Q&A or unprompted walkthrough. Defaults to `"qa"`. */
  readonly mode?: SessionMode;
  /** WebSocket endpoint override; defaults to the page's own origin. */
  readonly url?: string;
  /** Socket constructor seam for tests; defaults to the global `WebSocket`. */
  readonly createSocket?: (url: string) => WebSocket;
}

/** Everything a component needs to render and drive a session. */
export interface SessionController {
  /** Connection lifecycle, as `SessionClient` reports it. */
  readonly connection: ConnectionStatus;
  /** What the orb should portray: the agent's state once connected, the socket's before that. */
  readonly orbState: SessionState;
  /** True from the moment `start()` is called until the socket is closed. */
  readonly isActive: boolean;
  /** True when a message sent right now would reach the server. */
  readonly canSend: boolean;
  /** True while the agent is working on an answer, so a new question would cancel it (TR-022). */
  readonly isAnswering: boolean;
  /** The deck on screen: the session's once it opens, the previewed one before that. */
  readonly deck: Deck | null;
  /** Every deck the backend offers, for the picker. Empty when the listing is unreachable. */
  readonly decks: readonly DeckSummary[];
  /** The deck the next session will open. */
  readonly deckId: string;
  /** The 1-based slide on screen. */
  readonly currentSlide: number;
  /** Zero-based bullet to emphasise, or `null`. */
  readonly highlight: number | null;
  /** Open a session, replacing any that is already open. */
  start: () => void;
  /** Close the session. The transcript stays on screen (PRD F2). */
  stop: () => void;
  /** Choose the deck the *next* session opens. */
  selectDeck: (deckId: string) => void;
  /** Send a typed question (PRD F13). Returns whether it reached the socket. */
  sendText: (text: string) => boolean;
  /** Move the deck by hand and tell the agent where the user went (PRD F9). */
  goToSlide: (index: number) => void;
}

/**
 * Whether a turn is in flight, so a new question would cancel the one already running.
 *
 * Written as explicit comparisons rather than a set of "everything except X" so that a state
 * leaving or joining the protocol cannot quietly change the answer.
 *
 * @param agentState - The last state the server announced.
 * @returns `true` while the agent is producing an answer.
 */
function isTurnInFlight(agentState: SessionState): boolean {
  return agentState === "thinking" || agentState === "speaking";
}

/**
 * Narrow an unknown JSON value to an object.
 *
 * @param value - Anything `JSON.parse` might have produced.
 * @returns `true` when it is a plain object.
 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Convert one row of the deck listing.
 *
 * @param value - One element of the parsed response.
 * @returns The summary, or `null` when the row is not shaped like one.
 */
function toDeckSummary(value: unknown): DeckSummary | null {
  if (!isRecord(value)) {
    return null;
  }
  const { id, title } = value;
  const slideCount = value.slide_count;
  if (typeof id !== "string" || typeof title !== "string" || typeof slideCount !== "number") {
    return null;
  }
  return { id, title, slide_count: slideCount };
}

/**
 * Convert a deck response.
 *
 * The check is shallow on purpose: the backend validated the deck before serving it, so the only
 * failure worth defending against is a proxy answering with something else entirely — an HTML
 * error page, say. A second copy of the schema here would only drift.
 *
 * @param value - The parsed response body.
 * @returns The deck, or `null` when the body is not one.
 */
function toDeck(value: unknown): Deck | null {
  if (!isRecord(value)) {
    return null;
  }
  if (typeof value.id !== "string" || typeof value.title !== "string") {
    return null;
  }
  if (!Array.isArray(value.slides) || value.slides.length === 0) {
    return null;
  }
  return value as unknown as Deck;
}

/**
 * Decide what the orb portrays (PRD F11).
 *
 * The agent's state is only meaningful while the socket is up; before and after that the socket's
 * own lifecycle is the honest thing to show, which is why a closed session reads *idle* rather
 * than freezing on whatever the agent was last doing.
 *
 * @param connection - The socket lifecycle.
 * @param agentState - The last state the server announced.
 * @returns The state to draw.
 */
function deriveOrbState(connection: ConnectionStatus, agentState: SessionState): SessionState {
  switch (connection) {
    case "connected":
      return agentState;
    case "connecting":
    case "reconnecting":
      return "connecting";
    case "failed":
      return "error";
    case "idle":
    case "closed":
      return "idle";
  }
}

/**
 * Wire a `SessionClient` to the session store for the lifetime of a component.
 *
 * @param options - Optional endpoint and socket seams, mainly for tests.
 * @returns The session state the UI renders and the commands it issues.
 */
export function useSession(options: UseSessionOptions = {}): SessionController {
  const { mode = "qa", url, createSocket } = options;

  const connection = useSessionStore((state) => state.connection);
  const agentState = useSessionStore((state) => state.agentState);
  const sessionDeck = useSessionStore((state) => state.deck);
  const currentSlide = useSessionStore((state) => state.currentSlide);
  const highlight = useSessionStore((state) => state.highlight);

  const [decks, setDecks] = useState<readonly DeckSummary[]>([]);
  const [deckId, setDeckId] = useState(DEFAULT_DECK_ID);
  const [previewDeck, setPreviewDeck] = useState<Deck | null>(null);

  const clientRef = useRef<SessionClient | null>(null);

  // Before a session opens there is still a deck to look at (PRD F1: slide 1 is on screen when the
  // page loads), so the HTTP copy stands in until `session.ready` delivers the authoritative one.
  const deck = sessionDeck ?? previewDeck;

  useEffect(() => {
    const controller = new AbortController();

    // A named async function called with `void`: an inline async IIFE would be a floating promise,
    // which the type-checked ESLint rules reject.
    async function loadDecks(): Promise<void> {
      try {
        const response = await fetch(DECKS_PATH, { signal: controller.signal });
        if (!response.ok) {
          return;
        }
        const payload: unknown = await response.json();
        if (!Array.isArray(payload)) {
          return;
        }
        const summaries = payload
          .map((row: unknown) => toDeckSummary(row))
          .filter((row): row is DeckSummary => row !== null);
        setDecks(summaries);
        setDeckId((current) =>
          summaries.some((summary) => summary.id === current)
            ? current
            : (summaries[0]?.id ?? current),
        );
      } catch {
        // Offline, or the backend is not up yet: the picker stays empty and the default deck id
        // is used, which is exactly what a first-run user gets anyway.
      }
    }

    void loadDecks();
    return () => {
      controller.abort();
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();

    async function loadDeck(): Promise<void> {
      try {
        const response = await fetch(`${DECKS_PATH}/${encodeURIComponent(deckId)}`, {
          signal: controller.signal,
        });
        if (!response.ok) {
          return;
        }
        setPreviewDeck(toDeck(await response.json()));
      } catch {
        // Same as above: the deck simply does not appear until a session delivers it.
      }
    }

    void loadDeck();
    return () => {
      controller.abort();
    };
  }, [deckId]);

  useEffect(() => {
    // The socket is created by `start()`, never by an effect, so React 19's double-invoked effects
    // cannot open one. This cleanup is the only teardown path, and `close()` is idempotent.
    return () => {
      clientRef.current?.close();
      clientRef.current = null;
    };
  }, []);

  const start = useCallback((): void => {
    // `SessionClient.connect()` would replace its own socket, but dropping the previous client
    // explicitly keeps "one client per session" true and makes a double start a no-op rather than
    // a race between two sets of callbacks.
    clientRef.current?.close();
    // `clearSession`, not `reset`: the debug, push-to-talk and mute toggles belong to the user and
    // survive a second session (PRD F13).
    useSessionStore.getState().clearSession();

    const client = new SessionClient({
      deckId,
      mode,
      url,
      createSocket,
      onMessage: (message) => {
        useSessionStore.getState().applyServerMessage(message);
      },
      onStatus: (status) => {
        useSessionStore.getState().setConnection(status);
      },
      onInvalidFrame: (detail) => {
        useSessionStore
          .getState()
          .logNotice(`dropped an unreadable frame: ${detail.slice(0, FRAME_EXCERPT_CHARS)}`);
      },
      onGiveUp: (giveUp) => {
        // An alert, not an ordinary notice: the orb has just turned amber and told the user to
        // read the log, so the sentence explaining why has to be there with debug off (TR-175).
        useSessionStore
          .getState()
          .logNotice(
            `connection lost (code ${String(giveUp.code)}) after ${String(giveUp.attempts)} ` +
              `automatic ${giveUp.attempts === 1 ? "attempt" : "attempts"}; ` +
              `press "Start session" to try again`,
            { alert: true },
          );
      },
    });
    clientRef.current = client;
    client.connect();
  }, [createSocket, deckId, mode, url]);

  const stop = useCallback((): void => {
    clientRef.current?.close();
    clientRef.current = null;
  }, []);

  const selectDeck = useCallback((nextDeckId: string): void => {
    setDeckId(nextDeckId);
  }, []);

  const sendText = useCallback((text: string): boolean => {
    const trimmed = text.trim().slice(0, MAX_TEXT_INPUT_CHARS);
    if (trimmed.length === 0) {
      return false;
    }
    const client = clientRef.current;
    if (client?.isOpen !== true) {
      useSessionStore.getState().logNotice("no session is open; start one before asking");
      return false;
    }
    // TR-022: the server cancels the running turn to make room for a new one, but it announces a
    // cancellation only for a barge-in (`agent.cancelled`, TR-051). A second typed question would
    // therefore abandon the first answer mid-sentence with nothing in the transcript to say so, so
    // the question waits for the answer instead. Voice barge-in has a signal of its own and does
    // not come through here (PRD F7, Phase 3).
    if (isTurnInFlight(useSessionStore.getState().agentState)) {
      useSessionStore
        .getState()
        .logNotice("the presenter is still answering; wait for it to finish");
      return false;
    }
    return client.send({ type: "text.input", text: trimmed });
  }, []);

  const goToSlide = useCallback(
    (index: number): void => {
      const slideCount = deck?.slides.length ?? 0;
      if (slideCount === 0 || !Number.isFinite(index)) {
        return;
      }
      const target = Math.min(Math.max(Math.trunc(index), 1), slideCount);
      const store = useSessionStore.getState();
      if (target === store.currentSlide) {
        return;
      }
      // Manual navigation is client truth: the server never echoes a `slide.goto` for it
      // (`Session._on_user_navigation`), so the view moves here and the server is merely told.
      useSessionStore.setState({ currentSlide: target, highlight: null });

      const client = clientRef.current;
      if (client?.isOpen === true) {
        client.send({ type: "slide.changed", index: target, source: "user" });
        store.logNotice(`moved to slide ${String(target)} by hand`);
      }
    },
    [deck],
  );

  return {
    connection,
    orbState: deriveOrbState(connection, agentState),
    isActive:
      connection === "connecting" || connection === "reconnecting" || connection === "connected",
    canSend: connection === "connected",
    isAnswering: connection === "connected" && isTurnInFlight(agentState),
    deck,
    decks,
    deckId,
    currentSlide,
    highlight,
    start,
    stop,
    selectDeck,
    sendText,
    goToSlide,
  };
}
