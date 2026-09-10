import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Slide as SlideModel } from "../protocol";

import { Slide } from "./Slide";

/** PRD F1: four seconds, or until the next highlight. */
const HIGHLIGHT_DURATION_MS = 4_000;

/**
 * Build a slide fixture.
 *
 * @param index - The 1-based slide index.
 * @returns A slide with three distinguishable bullets.
 */
function makeSlide(index = 2): SlideModel {
  return {
    index,
    title: "The Latency Budget",
    bullets: ["VAD costs 30 ms", "STT costs 300 ms", "TTS first byte costs 200 ms"],
    notes: "Speaker notes.",
    aliases: ["latency"],
  };
}

/**
 * Find the list item wrapping a bullet's text.
 *
 * @param text - The bullet copy.
 * @returns The `li` element, which is what carries `data-highlighted`.
 */
function bulletItem(text: string): HTMLElement {
  const element = screen.getByText(text).closest("li");
  if (element === null) {
    throw new Error(`bullet "${text}" is not inside a list item`);
  }
  return element;
}

describe("Slide", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("TC-FE-123: emphasises the highlighted bullet, then releases it after four seconds", () => {
    render(<Slide slide={makeSlide()} highlight={1} />);

    expect(bulletItem("STT costs 300 ms")).toHaveAttribute("data-highlighted", "true");
    expect(bulletItem("VAD costs 30 ms")).not.toHaveAttribute("data-highlighted");

    act(() => {
      vi.advanceTimersByTime(HIGHLIGHT_DURATION_MS - 1);
    });
    expect(bulletItem("STT costs 300 ms")).toHaveAttribute("data-highlighted", "true");

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(bulletItem("STT costs 300 ms")).not.toHaveAttribute("data-highlighted");
  });

  it("TC-FE-124: moves the emphasis to the next highlight and restarts its clock", () => {
    const view = render(<Slide slide={makeSlide()} highlight={0} />);

    act(() => {
      vi.advanceTimersByTime(HIGHLIGHT_DURATION_MS - 500);
    });
    view.rerender(<Slide slide={makeSlide()} highlight={2} />);

    // The first bullet lets go immediately rather than waiting out its own timer, and the new one
    // gets a full four seconds rather than the 500 ms left on the old clock.
    expect(bulletItem("VAD costs 30 ms")).not.toHaveAttribute("data-highlighted");
    expect(bulletItem("TTS first byte costs 200 ms")).toHaveAttribute("data-highlighted", "true");

    act(() => {
      vi.advanceTimersByTime(HIGHLIGHT_DURATION_MS - 1);
    });
    expect(bulletItem("TTS first byte costs 200 ms")).toHaveAttribute("data-highlighted", "true");

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(bulletItem("TTS first byte costs 200 ms")).not.toHaveAttribute("data-highlighted");
  });

  it("renders the slide number and title as the slide's heading", () => {
    render(<Slide slide={makeSlide(4)} highlight={null} />);

    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("The Latency Budget");
    expect(screen.getByText("Slide 4")).toBeInTheDocument();
  });
});
