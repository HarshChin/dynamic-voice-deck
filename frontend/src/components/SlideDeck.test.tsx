import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Slide as SlideModel } from "../protocol";

import { SlideDeck } from "./SlideDeck";

const SLIDE_COUNT = 6;

/**
 * Build a deck's worth of slides with distinguishable titles.
 *
 * @param count - How many slides to build.
 * @returns The slides, indexed from one.
 */
function makeSlides(count = SLIDE_COUNT): readonly SlideModel[] {
  return Array.from({ length: count }, (_, i) => ({
    index: i + 1,
    title: `Chapter ${String(i + 1)}`,
    bullets: [`point ${String(i + 1)}a`, `point ${String(i + 1)}b`],
    notes: "Speaker notes.",
    aliases: [`slide-${String(i + 1)}`],
  }));
}

describe("SlideDeck", () => {
  it("TC-FE-001: renders the current slide and marks its dot as the deck's position", () => {
    render(<SlideDeck slides={makeSlides()} current={3} highlight={null} onNavigate={vi.fn()} />);

    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("Chapter 3");
    expect(screen.getByText("point 3a")).toBeInTheDocument();
    expect(screen.queryByText("Chapter 2")).not.toBeInTheDocument();

    const dot = screen.getByRole("button", { name: "Slide 3: Chapter 3" });
    expect(dot).toHaveAttribute("aria-current", "true");
    for (const other of [1, 2, 4, 5, 6]) {
      const label = `Slide ${String(other)}: Chapter ${String(other)}`;
      expect(screen.getByRole("button", { name: label })).not.toHaveAttribute("aria-current");
    }
  });

  it("TC-FE-125: moves with the arrow keys and stays silent at either end of the deck", () => {
    const onNavigate = vi.fn();
    const view = render(
      <SlideDeck slides={makeSlides()} current={1} highlight={null} onNavigate={onNavigate} />,
    );

    fireEvent.keyDown(window, { key: "ArrowLeft" });
    expect(onNavigate).not.toHaveBeenCalled();

    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(onNavigate).toHaveBeenCalledExactlyOnceWith(2);

    onNavigate.mockClear();
    view.rerender(
      <SlideDeck slides={makeSlides()} current={6} highlight={null} onNavigate={onNavigate} />,
    );

    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(onNavigate).not.toHaveBeenCalled();

    fireEvent.keyDown(window, { key: "ArrowLeft" });
    expect(onNavigate).toHaveBeenCalledExactlyOnceWith(5);
  });

  it("TC-FE-126: leaves arrow keys alone while the user is typing a question", () => {
    // The composer is the Phase 1 input path (PRD F13), and its caret moves with the same keys.
    // Without this the deck would flick through slides as someone edited a question.
    const onNavigate = vi.fn();
    render(
      <>
        <input aria-label="composer" type="text" />
        <SlideDeck slides={makeSlides()} current={2} highlight={null} onNavigate={onNavigate} />
      </>,
    );

    fireEvent.keyDown(screen.getByLabelText("composer"), { key: "ArrowRight" });
    expect(onNavigate).not.toHaveBeenCalled();

    fireEvent.keyDown(window, { key: "ArrowRight", metaKey: true });
    expect(onNavigate).not.toHaveBeenCalled();

    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(onNavigate).toHaveBeenCalledExactlyOnceWith(3);
  });

  it("TC-FE-127: navigates by clicking a dot or an arrow button", () => {
    const onNavigate = vi.fn();
    render(
      <SlideDeck slides={makeSlides()} current={2} highlight={null} onNavigate={onNavigate} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Slide 5: Chapter 5" }));
    expect(onNavigate).toHaveBeenLastCalledWith(5);

    fireEvent.click(screen.getByRole("button", { name: "Next slide" }));
    expect(onNavigate).toHaveBeenLastCalledWith(3);

    fireEvent.click(screen.getByRole("button", { name: "Previous slide" }));
    expect(onNavigate).toHaveBeenLastCalledWith(1);
    expect(onNavigate).toHaveBeenCalledTimes(3);
  });

  it("passes the highlight through to the slide it belongs to", () => {
    render(<SlideDeck slides={makeSlides()} current={4} highlight={1} onNavigate={vi.fn()} />);

    const bullet = screen.getByText("point 4b").closest("li");
    expect(bullet).toHaveAttribute("data-highlighted", "true");
  });

  it("says so rather than crashing when the index has no slide behind it", () => {
    render(<SlideDeck slides={[]} current={1} highlight={null} onNavigate={vi.fn()} />);

    expect(screen.getByText("No slide to show.")).toBeInTheDocument();
  });
});
