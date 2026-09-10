import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SESSION_STATES, type SessionState } from "../protocol";

import { Orb } from "./Orb";

/**
 * What each state must say in words, so colour is never the only signal (PRD F11).
 *
 * Keyed by plain strings, like the orb's own table: `interrupted` may or may not survive in the
 * protocol (F24), and this file has to compile either way.
 */
const EXPECTED_LABELS: Readonly<Record<string, string>> = {
  idle: "Idle",
  connecting: "Connecting",
  listening: "Listening",
  hearing: "Hearing you",
  thinking: "Thinking",
  speaking: "Speaking",
  interrupted: "Interrupted",
  error: "Error",
};

/**
 * Name a session state without depending on the protocol still listing it.
 *
 * `interrupted` may leave the enum (F24) and a server one version ahead may name a state that was
 * never in it, so the two cases this file cares about are written as plain strings and laundered
 * here rather than as assertions that would go stale in either direction.
 *
 * @param value - The state as it would arrive on the wire.
 * @returns The same value, typed as a state.
 */
function asState(value: string): SessionState {
  return value as SessionState;
}

describe("Orb", () => {
  it("TC-FE-120: gives every session state its own visual state and label", () => {
    // Driven off SESSION_STATES rather than a hand-written list: adding a state to the protocol
    // without teaching the orb about it must fail here, not silently render a blank indicator.
    for (const state of SESSION_STATES) {
      const expected = EXPECTED_LABELS[state];
      expect(expected, `the orb has no copy for "${state}"`).toBeDefined();

      const view = render(<Orb state={state} />);

      const label = screen.getByText(expected ?? "");
      const orb = label.closest("[data-state]");
      expect(orb).toHaveAttribute("data-state", state);
      expect(screen.queryByText("unrecognised state")).not.toBeInTheDocument();

      view.unmount();
    }
  });

  it("TC-FE-121: announces the state through a live region, not only through colour", () => {
    render(<Orb state="thinking" />);

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Thinking");
    expect(status).toHaveAttribute("aria-live", "polite");
  });

  it("TC-FE-147: portrays an interrupted turn, and names a state it does not recognise", () => {
    // F24: `interrupted` is drawn in the PRD's state machine but may never be emitted — the
    // backend is deciding whether to send it or fold it into `listening`. The cast keeps this case
    // compiling either way; what it asserts is that a frame naming the state is portrayed, not
    // that the protocol still lists it.
    const view = render(<Orb state={asState("interrupted")} />);

    expect(screen.getByText("Interrupted").closest("[data-state]")).toHaveAttribute(
      "data-state",
      "interrupted",
    );
    view.unmount();

    // A server a version ahead can name a state this build has never heard of. Saying so beats a
    // blank indicator, and it must not throw.
    render(<Orb state={asState("daydreaming")} />);

    expect(screen.getByRole("status")).toHaveTextContent("daydreaming");
    expect(screen.getByText("unrecognised state")).toBeInTheDocument();
  });

  it("TC-FE-122: drops the hint line when asked for a compact orb", () => {
    const view = render(<Orb state="listening" />);
    expect(screen.getByText("ask a question")).toBeInTheDocument();

    view.rerender(<Orb state="listening" compact />);
    expect(screen.queryByText("ask a question")).not.toBeInTheDocument();
    expect(screen.getByText("Listening")).toBeInTheDocument();
  });
});
