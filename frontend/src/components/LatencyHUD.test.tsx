import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { MetricsMessage } from "../protocol";
import { useSessionStore } from "../store";

import { LatencyHUD } from "./LatencyHUD";

/**
 * Build a `metrics` message.
 *
 * @param turnId - The turn it reports on.
 * @param llmTtftMs - Time to first token, the field these cases vary.
 * @returns The message.
 */
function metrics(turnId: number, llmTtftMs: number): MetricsMessage {
  return {
    type: "metrics",
    turn_id: turnId,
    stt_ms: 280,
    llm_ttft_ms: llmTtftMs,
    llm_total_ms: 900,
    tts_ttfb_ms: null,
    sentences: 2,
  };
}

/**
 * Read the "last" and "median" cells of one HUD row.
 *
 * @param stage - The row's stage label.
 * @returns The two cell texts, in column order.
 */
function row(stage: string): readonly (string | null)[] {
  const cells = screen
    .getByRole("rowheader", { name: stage })
    .closest("tr")
    ?.querySelectorAll("td");
  return Array.from(cells ?? [], (cell) => cell.textContent);
}

describe("LatencyHUD", () => {
  it("TC-FE-033: shows the last turn's value beside the session median", () => {
    // Driven through the real store so the medians are the ones the app will actually show, not a
    // second implementation of the same arithmetic living in the test.
    const store = useSessionStore.getState();
    store.reset();
    for (const [turnId, ttft] of [
      [1, 200],
      [2, 400],
      [3, 900],
    ] as const) {
      useSessionStore.getState().applyServerMessage(metrics(turnId, ttft));
    }
    const { last, medians } = useSessionStore.getState().metrics;

    render(<LatencyHUD last={last} medians={medians} />);

    expect(screen.getByText("turn 3")).toBeInTheDocument();
    expect(row("llm ttft")).toEqual(["900 ms", "400 ms"]);
    expect(row("stt")).toEqual(["280 ms", "280 ms"]);
    useSessionStore.getState().reset();
  });

  it("grades each measurement against its budget and says when a stage never reported", () => {
    render(
      <LatencyHUD
        last={{
          turnId: 7,
          sttMs: 250,
          llmTtftMs: 320,
          llmTotalMs: 4_000,
          ttsTtfbMs: null,
          firstAudioMs: null,
          interruptStopMs: null,
          sentences: 1,
        }}
        medians={{
          sttMs: 250,
          llmTtftMs: 320,
          llmTotalMs: 4_000,
          ttsTtfbMs: null,
          firstAudioMs: null,
          interruptStopMs: null,
        }}
      />,
    );

    const cell = (stage: string): Element | undefined =>
      screen.getByRole("rowheader", { name: stage }).closest("tr")?.querySelectorAll("td")[0];

    expect(cell("stt")).toHaveAttribute("data-band", "ok");
    expect(cell("llm ttft")).toHaveAttribute("data-band", "warn");
    expect(cell("llm total")).toHaveAttribute("data-band", "bad");
    expect(cell("tts ttfb")).not.toHaveAttribute("data-band");
    expect(row("tts ttfb")).toEqual(["—", "—"]);
  });

  it("says there is nothing to report before the first turn", () => {
    render(
      <LatencyHUD
        last={null}
        medians={{
          sttMs: null,
          llmTtftMs: null,
          llmTotalMs: null,
          ttsTtfbMs: null,
          firstAudioMs: null,
          interruptStopMs: null,
        }}
      />,
    );

    expect(screen.getByText("no turns")).toBeInTheDocument();
  });
});
