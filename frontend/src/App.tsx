import { useEffect, useState, type JSX } from "react";

import "./App.css";

/** Reachability of the FastAPI backend, as seen from the browser. */
type BackendStatus = "checking" | "ok" | "unreachable";

/** Relative URL; the Vite dev server proxies it to the backend (see vite.config.ts). */
const HEALTH_URL = "/api/health";

/**
 * Phase 0 shell for Dynamic Voice Deck.
 *
 * It renders the deck title and a backend reachability indicator. The indicator exists to prove the
 * dev-server proxy and the async lint rules are wired correctly; the real deck UI lands later.
 */
export function App(): JSX.Element {
  const [status, setStatus] = useState<BackendStatus>("checking");

  useEffect(() => {
    const controller = new AbortController();

    // Named async function + explicit `void` call: an inline async IIFE inside useEffect would be a
    // floating promise, which the ESLint type-checked rules reject.
    async function checkBackend(): Promise<void> {
      try {
        const response = await fetch(HEALTH_URL, { signal: controller.signal });
        setStatus(response.ok ? "ok" : "unreachable");
      } catch {
        // An abort is the normal teardown path, not a backend failure.
        if (!controller.signal.aborted) {
          setStatus("unreachable");
        }
      }
    }

    void checkBackend();

    return () => {
      controller.abort();
    };
  }, []);

  return (
    <main className="app">
      <h1 className="app__title">Dynamic Voice Deck</h1>
      <p className="app__subtitle">A voice-first slide presenter you can interrupt mid-sentence.</p>
      <p className={`app__status app__status--${status}`} role="status" aria-live="polite">
        <span className="app__status-dot" aria-hidden="true" />
        backend: {status}
      </p>
    </main>
  );
}

export default App;
