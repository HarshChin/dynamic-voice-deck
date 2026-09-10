import { render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

// This file is `.ts` rather than `.tsx` (it is the agreed Phase 0 filename), so components are
// instantiated with `createElement` instead of JSX.

afterEach(() => {
  vi.unstubAllGlobals();
});

/**
 * Build a fetch stub that resolves with the given response shape.
 *
 * @param init - Partial response; only the fields App reads are needed.
 * @returns The mock, so a test can assert on how it was called.
 */
function stubFetch(init: Partial<Response>) {
  const mock = vi.fn(() => Promise.resolve(init as unknown as Response));
  vi.stubGlobal("fetch", mock);
  return mock;
}

describe("test harness", () => {
  it("TC-FE-090: loads the jsdom environment, setup file, and jest-dom matchers", () => {
    // Asserting the harness rather than arithmetic: each of these fails loudly if the vitest
    // environment, the setupFiles entry, or the jest-dom matcher registration is misconfigured.
    expect(typeof document).toBe("object");
    expect(document.body).toBeInTheDocument();
    expect(typeof window.matchMedia === "function" || window.matchMedia === undefined).toBe(true);
  });
});

describe("App", () => {
  it("TC-FE-091: renders the deck title and reports a healthy backend", async () => {
    const fetchMock = stubFetch({ ok: true });

    render(createElement(App));

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Dynamic Voice Deck");
    expect(await screen.findByText(/backend: ok/)).toBeInTheDocument();
    // Phase 1 also fetches the deck listing and the deck itself, so count the health probe rather
    // than every request: probing twice is the bug this assertion has always been about.
    const healthCalls = (fetchMock.mock.calls as unknown as [string][]).filter(
      ([url]) => url === "/api/health",
    );
    expect(healthCalls).toHaveLength(1);
  });

  it("TC-FE-093: probes the documented health path, not some other URL", async () => {
    // Phase 0 shipped a real bug where the proxy pointed at the wrong origin. A test that never
    // asserts the requested URL would not have caught it, so pin the path explicitly.
    const fetchMock = stubFetch({ ok: true });

    render(createElement(App));
    await screen.findByText(/backend: ok/);

    const [url, options] = fetchMock.mock.calls[0] as unknown as [string, RequestInit | undefined];
    expect(url).toBe("/api/health");
    expect(options?.signal).toBeInstanceOf(AbortSignal);
  });

  it("TC-FE-094: treats a non-ok HTTP response as unreachable", async () => {
    // Distinct from a rejected promise: the server answered, but with 500. Without this case the
    // `response.ok` branch in App is never executed by any test.
    stubFetch({ ok: false, status: 503 });

    render(createElement(App));

    expect(await screen.findByText(/backend: unreachable/)).toBeInTheDocument();
  });

  it("TC-FE-092: reports an unreachable backend when the health probe rejects", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))),
    );

    render(createElement(App));

    expect(await screen.findByText(/backend: unreachable/)).toBeInTheDocument();
  });

  it("TC-FE-095: aborts the in-flight probe on unmount without an unhandled rejection", async () => {
    // Guards the AbortController cleanup. A leaked probe that resolves after unmount would call
    // setState on an unmounted component, which is exactly the leak class TR-103 cares about.
    let capturedSignal: AbortSignal | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, options?: RequestInit) => {
        capturedSignal = options?.signal ?? undefined;
        return new Promise<Response>(() => {
          // never settles: simulates a hung backend
        });
      }),
    );

    const view = render(createElement(App));
    expect(screen.getByText(/backend: checking/)).toBeInTheDocument();

    view.unmount();

    await waitFor(() => {
      expect(capturedSignal?.aborted).toBe(true);
    });
  });
});
