import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RateLimitChip } from "./RateLimitChip";

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
    render(<FallbackBanner fromModel="qwen/qwen3.8-27b" toModel="qwen2.5:7b" />);

    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent("qwen/qwen3.8-27b hit its rate limit");
    expect(banner).toHaveTextContent("answering with qwen2.5:7b on this machine");
  });

  it("TC-FE-211: says nothing about time, and starts no clock of its own", () => {
    render(<FallbackBanner fromModel="qwen/qwen3.8-27b" toModel="qwen2.5:7b" />);

    expect(screen.getByRole("status")).not.toHaveTextContent("back in");
    expect(vi.getTimerCount()).toBe(0);
  });

  it("TC-FE-212: stands beside the countdown, which keeps saying when the usual model returns", () => {
    render(
      <>
        <RateLimitChip until={clock + 900_000} now={now} />
        <FallbackBanner fromModel="qwen/qwen3.8-27b" toModel="qwen2.5:7b" />
      </>,
    );

    const [chip, banner] = screen.getAllByRole("status");
    expect(chip).toHaveTextContent("15 min");
    expect(banner).toHaveTextContent("answering with qwen2.5:7b on this machine");
    // Said once between them: the chip owns the wait, the banner owns who is speaking.
    expect(banner).not.toHaveTextContent("15 min");
  });

  it("TC-FE-213: shows nothing when no model has been substituted", () => {
    render(<FallbackBanner fromModel={null} toModel={null} />);

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(vi.getTimerCount()).toBe(0);
  });
});
