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

  it("TC-FE-146: emphasises the same bullet again when it is highlighted a second time", () => {
    const view = render(<Slide slide={makeSlide()} highlight={1} />);

    act(() => {
      vi.advanceTimersByTime(HIGHLIGHT_DURATION_MS);
    });
    expect(bulletItem("STT costs 300 ms")).not.toHaveAttribute("data-highlighted");

    // A navigation with no highlight clears the emphasis, and then the agent comes back to the
    // same bullet. Remembering the expiry by (slide, bullet) alone made that second highlight dead
    // on arrival for as long as the slide stayed mounted.
    view.rerender(<Slide slide={makeSlide()} highlight={null} />);
    view.rerender(<Slide slide={makeSlide()} highlight={1} />);

    expect(bulletItem("STT costs 300 ms")).toHaveAttribute("data-highlighted", "true");

    // And it gets four seconds of its own, not the remains of the first highlight's clock.
    act(() => {
      vi.advanceTimersByTime(HIGHLIGHT_DURATION_MS - 1);
    });
    expect(bulletItem("STT costs 300 ms")).toHaveAttribute("data-highlighted", "true");

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(bulletItem("STT costs 300 ms")).not.toHaveAttribute("data-highlighted");
  });

  it("renders the slide number and title as the slide's heading", () => {
    render(<Slide slide={makeSlide(4)} highlight={null} />);

    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("The Latency Budget");
    expect(screen.getByText("Slide 4")).toBeInTheDocument();
  });
});

/**
 * Build a slide that arranges its bullets rather than listing them.
 *
 * @param kind - Which arrangement.
 * @param items - The cards, columns or steps.
 * @returns The slide.
 */
function arranged(
  kind: "metrics" | "split" | "flow",
  items: { bullets: number[]; heading: string; caption?: string }[],
): SlideModel {
  return {
    ...makeSlide(),
    figure: {
      kind,
      items: items.map((item) => ({ ...item, caption: item.caption ?? "" })),
    },
  };
}

describe("arrangements (PRD F1)", () => {
  it("TC-FE-230: a slide with no figure is still a plain list", () => {
    render(<Slide slide={makeSlide()} highlight={null} />);

    expect(screen.getByText("STT costs 300 ms")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 3 })).not.toBeInTheDocument();
  });

  it("TC-FE-231: metric cards show the value and keep the bullet for a screen reader", () => {
    render(
      <Slide
        slide={arranged("metrics", [
          { bullets: [0], heading: "30 ms", caption: "voice detection" },
          { bullets: [1], heading: "300 ms", caption: "speech to text" },
          { bullets: [2], heading: "200 ms", caption: "first audio" },
        ])}
        highlight={null}
      />,
    );

    expect(screen.getByText("300 ms")).toBeInTheDocument();
    expect(screen.getByText("speech to text")).toBeInTheDocument();
    // The bullet is still in the document: the card is a presentation of it, not a replacement.
    expect(screen.getByText("STT costs 300 ms")).toBeInTheDocument();
  });

  it("TC-FE-232: columns carry their own bullets under their own heading", () => {
    render(
      <Slide
        slide={arranged("split", [
          { bullets: [0, 1], heading: "In the browser" },
          { bullets: [2], heading: "On the server" },
        ])}
        highlight={null}
      />,
    );

    const browser = screen.getByRole("heading", { name: "In the browser" }).closest("section");
    expect(browser).toHaveTextContent("VAD costs 30 ms");
    expect(browser).toHaveTextContent("STT costs 300 ms");
    expect(browser).not.toHaveTextContent("TTS first byte costs 200 ms");
  });

  it("TC-FE-233: steps are numbered in the order the figure gives them", () => {
    render(
      <Slide
        slide={arranged("flow", [
          { bullets: [0], heading: "Hear" },
          { bullets: [1], heading: "Think" },
          { bullets: [2], heading: "Speak" },
        ])}
        highlight={null}
      />,
    );

    const steps = screen.getAllByRole("listitem").filter((item) => item.querySelector("ul"));
    expect(steps).toHaveLength(3);
    for (const [position, heading] of ["Hear", "Think", "Speak"].entries()) {
      // The number is rendered rather than drawn by a CSS counter, so it is assertable here and
      // readable by anything that ignores stylesheets.
      expect(steps[position]).toHaveTextContent(new RegExp(`^${String(position + 1)}${heading}`));
    }
  });

  it.each([
    [
      "metrics" as const,
      [
        { bullets: [0], heading: "30 ms" },
        { bullets: [1, 2], heading: "500 ms" },
      ],
    ],
    [
      "split" as const,
      [
        { bullets: [0], heading: "One" },
        { bullets: [1, 2], heading: "Two" },
      ],
    ],
    [
      "flow" as const,
      [
        { bullets: [0], heading: "First" },
        { bullets: [1, 2], heading: "Then" },
      ],
    ],
  ])("TC-FE-234: highlight_bullet finds its target in a %s arrangement", (kind, items) => {
    render(<Slide slide={arranged(kind, items)} highlight={2} />);

    // Bullet 2 belongs to the second item, so that item is the one emphasised — whatever the
    // arrangement calls its containers. This is what keeps `highlight_bullet` working after a
    // slide is rearranged: the figure points at bullets, so the emphasis follows the bullet.
    const emphasised = document.querySelectorAll('[data-highlighted="true"]');
    expect(emphasised).toHaveLength(1);
    expect(emphasised[0]).toHaveTextContent(
      kind === "metrics" ? "500 ms" : "TTS first byte costs 200 ms",
    );
  });

  it("TC-FE-235: an arrangement this build does not know degrades to a list", () => {
    const slide = {
      ...makeSlide(),
      figure: { kind: "carousel" as unknown as "metrics", items: [] },
    };

    render(<Slide slide={slide} highlight={null} />);

    // A deck authored against a newer schema should render less prettily, not blank.
    expect(screen.getByText("STT costs 300 ms")).toBeInTheDocument();
  });
});
