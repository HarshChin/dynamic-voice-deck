import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { secondsLeft } from "../time";

import { RateLimitChip } from "./RateLimitChip";

/** A clock the test moves by hand, so no test waits in real time. */
let clock = 0;
const now = (): number => clock;

beforeEach(() => {
  clock = 1_000_000;
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

/**
 * Advance both the clock and the timers the chip is using.
 *
 * @param ms - How far to move.
 */
function advance(ms: number): void {
  act(() => {
    clock += ms;
    vi.advanceTimersByTime(ms);
  });
}

describe("RateLimitChip", () => {
  it("TC-FE-180: says how long the free tier has asked us to wait", () => {
    render(<RateLimitChip until={clock + 12_000} now={now} />);

    expect(screen.getByRole("status")).toHaveTextContent("rate limited — free tier, ready in 12s");
  });

  it("TC-FE-181: counts down as the wait passes", () => {
    render(<RateLimitChip until={clock + 5_000} now={now} />);

    advance(2_000);
    expect(screen.getByRole("status")).toHaveTextContent("ready in 3s");

    advance(2_500);
    expect(screen.getByRole("status")).toHaveTextContent("ready in 1s");
  });

  it("TC-FE-182: disappears when the wait is over, and stops its timer", () => {
    render(<RateLimitChip until={clock + 3_000} now={now} />);

    advance(3_100);

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    // Nothing is left running to redraw a chip that is gone.
    expect(vi.getTimerCount()).toBe(0);
  });

  it("TC-FE-183: shows nothing at all when no wait was reported", () => {
    render(<RateLimitChip until={null} now={now} />);

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("TC-FE-184: a wait that has already elapsed is not shown", () => {
    render(<RateLimitChip until={clock - 1} now={now} />);

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("TC-FE-185: a second, longer wait replaces the first", () => {
    const view = render(<RateLimitChip until={clock + 2_000} now={now} />);

    view.rerender(<RateLimitChip until={clock + 30_000} now={now} />);

    expect(screen.getByRole("status")).toHaveTextContent("ready in 30s");
  });

  it("rounds up, so the chip never sits on a number it has passed", () => {
    expect(secondsLeft(1)).toBe(1);
    expect(secondsLeft(1_400)).toBe(2);
    expect(secondsLeft(0)).toBe(0);
    expect(secondsLeft(-500)).toBe(0);
  });
});
