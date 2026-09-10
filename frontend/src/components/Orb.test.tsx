import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SESSION_STATES, type SessionState } from "../protocol";

import { Orb } from "./Orb";

/** What each state must say in words, so colour is never the only signal (PRD F11). */
const EXPECTED_LABELS: Readonly<Record<SessionState, string>> = {
  idle: "Idle",
  connecting: "Connecting",
  listening: "Listening",
  hearing: "Hearing you",
  thinking: "Thinking",
  speaking: "Speaking",
  interrupted: "Interrupted",
  error: "Error",
};

describe("Orb", () => {
  it("TC-FE-120: gives every session state its own visual state and label", () => {
    // Driven off SESSION_STATES rather than a hand-written list: adding a state to the protocol
    // without teaching the orb about it must fail here, not silently render a blank indicator.
    for (const state of SESSION_STATES) {
      const view = render(<Orb state={state} />);

      const label = screen.getByText(EXPECTED_LABELS[state]);
      const orb = label.closest("[data-state]");
      expect(orb).toHaveAttribute("data-state", state);

      view.unmount();
    }
  });

  it("TC-FE-121: announces the state through a live region, not only through colour", () => {
    render(<Orb state="thinking" />);

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Thinking");
    expect(status).toHaveAttribute("aria-live", "polite");
  });

  it("TC-FE-122: drops the hint line when asked for a compact orb", () => {
    const view = render(<Orb state="listening" />);
    expect(screen.getByText("ask a question")).toBeInTheDocument();

    view.rerender(<Orb state="listening" compact />);
    expect(screen.queryByText("ask a question")).not.toBeInTheDocument();
    expect(screen.getByText("Listening")).toBeInTheDocument();
  });
});
