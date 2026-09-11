import { spawn, type ChildProcess } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

/**
 * Start and stop the backend for the end-to-end suite.
 *
 * Owned by the tests rather than by Playwright's `webServer` because TC-E2E-002 is about what the
 * app does when the backend is *not* there, which means one test has to be able to take it away
 * and give it back.
 */

const BACKEND_DIR = path.resolve(
  fileURLToPath(new URL(".", import.meta.url)),
  "..",
  "..",
  "backend",
);

/** Where the backend listens. Pinned to IPv4: the Vite proxy targets 127.0.0.1. */
export const BACKEND_URL = "http://127.0.0.1:8000";

/** How long to wait for the health probe before giving up. */
const READY_TIMEOUT_MS = 60_000;

let server: ChildProcess | null = null;

/**
 * Whether the backend answers its health probe right now.
 *
 * @returns `true` when something is serving on the backend's port.
 */
async function reachable(): Promise<boolean> {
  try {
    const response = await fetch(`${BACKEND_URL}/api/health`, {
      signal: AbortSignal.timeout(1_000),
    });
    return response.ok;
  } catch {
    return false;
  }
}

/**
 * Wait until the backend answers, or until the timeout.
 *
 * @param up - Whether to wait for it to be reachable or unreachable.
 */
async function waitFor(up: boolean): Promise<void> {
  const deadline = Date.now() + READY_TIMEOUT_MS;
  for (;;) {
    if ((await reachable()) === up) {
      return;
    }
    if (Date.now() > deadline) {
      throw new Error(`backend did not become ${up ? "reachable" : "unreachable"} in time`);
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
}

/**
 * Start the backend with fake providers and wait for it to be ready.
 *
 * Idempotent: starting an already-running backend is a no-op, so a spec can call it in
 * `beforeAll` without knowing what the previous spec left behind.
 */
export async function startBackend(): Promise<void> {
  if (server !== null) {
    return;
  }
  if (await reachable()) {
    // Adopting it would be worse than failing: a backend someone else started is almost certainly
    // running the real providers, and every assertion here depends on the fake ones.
    throw new Error(
      "something is already listening on port 8000. The end-to-end suite starts its own backend " +
        "with fake providers; stop `make backend` and run it again.",
    );
  }
  server = spawn(
    "uv",
    ["run", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
    {
      cwd: BACKEND_DIR,
      env: {
        ...process.env,
        STT_PROVIDER: "fake",
        LLM_PROVIDER: "fake",
        TTS_PROVIDER: "fake",
        // Nothing here should reach Groq, and an absent key would fail startup for the real stack.
        GROQ_API_KEY: "e2e-not-a-real-key",
        LOG_LEVEL: "WARNING",
      },
      stdio: "ignore",
    },
  );
  await waitFor(true);
}

/** Stop the backend and wait until it really is gone. */
export async function stopBackend(): Promise<void> {
  if (server === null) {
    return;
  }
  server.kill("SIGTERM");
  server = null;
  await waitFor(false);
}
