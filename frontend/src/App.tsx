import { useEffect, useState, type JSX } from "react";

import { Controls } from "./components/Controls";
import { EventLog } from "./components/EventLog";
import { LatencyHUD } from "./components/LatencyHUD";
import { SlideDeck } from "./components/SlideDeck";
import { usePushToTalk } from "./session/usePushToTalk";
import { useSession } from "./session/useSession";
import { useSessionStore } from "./store";

import "./App.css";

/** Reachability of the FastAPI backend, as seen from the browser. */
type BackendStatus = "checking" | "ok" | "unreachable";

/** Relative URL; the Vite dev server proxies it to the backend (see vite.config.ts). */
const HEALTH_URL = "/api/health";

/**
 * The Phase 1 shell (PRD F1, F10, F11, F12; TRD §5.5).
 *
 * Three regions, in the order they matter: the deck fills the viewport, the event log is a column
 * that can be folded away when the deck is the point, and the control bar carries the orb and the
 * text composer that is Phase 1's only way in. Everything below this component is a pure view over
 * `useSession`; the only state App owns is the backend probe and whether the log is open.
 *
 * @returns The application shell.
 */
export function App(): JSX.Element {
  const [backend, setBackend] = useState<BackendStatus>("checking");
  const [logOpen, setLogOpen] = useState(true);

  // Declared before `useSession` so the health probe is the first request the page makes; a
  // reviewer opening the network panel should see the reachability check, then the deck.
  useEffect(() => {
    const controller = new AbortController();

    // Named async function + explicit `void` call: an inline async IIFE inside useEffect would be a
    // floating promise, which the ESLint type-checked rules reject.
    async function checkBackend(): Promise<void> {
      try {
        const response = await fetch(HEALTH_URL, { signal: controller.signal });
        setBackend(response.ok ? "ok" : "unreachable");
      } catch {
        // An abort is the normal teardown path, not a backend failure.
        if (!controller.signal.aborted) {
          setBackend("unreachable");
        }
      }
    }

    void checkBackend();

    return () => {
      controller.abort();
    };
  }, []);

  const session = useSession();
  // Bound here rather than inside the control bar: the key has to work wherever the user is
  // looking, and the bar is only one part of the page.
  const pushHeld = usePushToTalk({
    enabled: session.pushToTalk && session.isActive,
    onPress: session.startPush,
    onRelease: session.endPush,
  });
  const events = useSessionStore((state) => state.events);
  const debug = useSessionStore((state) => state.settings.debug);
  const updateSettings = useSessionStore((state) => state.updateSettings);
  const exportEvents = useSessionStore((state) => state.exportEvents);
  const metrics = useSessionStore((state) => state.metrics);

  const { deck } = session;

  return (
    <div className="app" data-log-open={logOpen ? "true" : "false"}>
      <header className="app__bar">
        <div className="app__brand">
          <h1 className="app__wordmark">Dynamic Voice Deck</h1>
          <p className="app__tagline">A voice-first slide presenter you can interrupt.</p>
        </div>
        <p className="app__deck">{deck?.title ?? "No deck loaded"}</p>
        <p className={`app__health app__health--${backend}`} role="status" aria-live="polite">
          <span className="app__health-dot" aria-hidden="true" />
          backend: {backend}
        </p>
        <button
          className="app__log-toggle"
          type="button"
          aria-pressed={logOpen}
          onClick={() => {
            setLogOpen((open) => !open);
          }}
        >
          {logOpen ? "Hide log" : "Show log"}
        </button>
      </header>

      <main className="app__stage">
        {deck === null ? (
          <section className="app__cover">
            <h2 className="app__cover-title">Nothing loaded yet</h2>
            <p className="app__cover-body">
              The deck is served by the backend. Start it with <code>make backend</code>, then start
              a session and ask a question — the agent answers and moves the deck itself.
            </p>
          </section>
        ) : (
          <SlideDeck
            slides={deck.slides}
            current={session.currentSlide}
            highlight={session.highlight}
            onNavigate={session.goToSlide}
          />
        )}
      </main>

      {logOpen && (
        <aside className="app__rail">
          <EventLog
            events={events}
            debug={debug}
            exportEvents={exportEvents}
            onDebugChange={(next) => {
              updateSettings({ debug: next });
            }}
          />
          {metrics.last !== null && (
            <div className="app__hud">
              <LatencyHUD last={metrics.last} medians={metrics.medians} />
            </div>
          )}
        </aside>
      )}

      <footer className="app__controls">
        <Controls
          orbState={session.orbState}
          outputLevel={session.outputLevel}
          onPresent={session.present}
          muted={session.muted}
          onToggleMute={session.setMuted}
          pushToTalk={session.pushToTalk}
          onTogglePushToTalk={session.setPushToTalk}
          pushHeld={pushHeld}
          isActive={session.isActive}
          canSend={session.canSend}
          isAnswering={session.isAnswering}
          decks={session.decks}
          deckId={session.deckId}
          onSelectDeck={session.selectDeck}
          onStart={session.start}
          onStop={session.stop}
          onSend={session.sendText}
        />
      </footer>
    </div>
  );
}

export default App;
