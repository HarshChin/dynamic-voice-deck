import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FallbackBanner } from "./FallbackBanner";

let clock = 0;
const now = (): number => clock;

beforeEach(() => {
  clock = 1_000_000;
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("FallbackBanner", () => {
  it("TC-FE-210: names both models and says the answer is coming from this machine", () => {
    render(<FallbackBanner fromModel="qwen/qwen3.8-27b" toModel="qwen2.5:7b" now={now} />);

    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent("qwen/qwen3.8-27b hit its rate limit");
    expect(banner).toHaveTextContent("answering with qwen2.5:7b on this machine");
  });

  it("TC-FE-211: adds when the usual model is expected back, when that is known", () => {
    render(
      <FallbackBanner
        fromModel="qwen/qwen3.8-27b"
        toModel="qwen2.5:7b"
        until={clock + 900_000}
        now={now}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("qwen/qwen3.8-27b back in 15 min");
  });

  it("TC-FE-212: counts that estimate down and drops it once it has passed", () => {
    render(
      <FallbackBanner
        fromModel="qwen/qwen3.8-27b"
        toModel="qwen2.5:7b"
        until={clock + 5_000}
        now={now}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("qwen/qwen3.8-27b back in 5s");

    act(() => {
      clock += 6_000;
      vi.advanceTimersByTime(6_000);
    });

    // The substitution is still in force, so the banner stays; only the estimate goes.
    expect(screen.getByRole("status")).toHaveTextContent("on this machine");
    expect(screen.getByRole("status")).not.toHaveTextContent("back in");
  });

  it("TC-FE-213: shows nothing when no model has been substituted", () => {
    render(<FallbackBanner fromModel={null} toModel={null} now={now} />);

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(vi.getTimerCount()).toBe(0);
  });
});
