import { fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";

import { MAX_TEXT_INPUT_CHARS } from "../protocol";
import type { DeckSummary } from "../session/useSession";

import { Controls } from "./Controls";

const COMPOSER_LABEL = "Ask the presenter a question";

const ONE_DECK: readonly DeckSummary[] = [
  { id: "anatomy_of_a_voice_agent", title: "Anatomy of a Voice Agent", slide_count: 6 },
];

const TWO_DECKS: readonly DeckSummary[] = [
  ...ONE_DECK,
  { id: "history_of_jazz", title: "The History of Jazz", slide_count: 5 },
];

/**
 * Render the control bar with sensible defaults for the fields a test does not care about.
 *
 * @param overrides - The props this test is actually about.
 * @returns The spies the test asserts on, plus the render result.
 */
function renderControls(overrides: Partial<ComponentProps<typeof Controls>> = {}) {
  const props = {
    orbState: "listening" as const,
    isActive: true,
    canSend: true,
    decks: ONE_DECK,
    deckId: "anatomy_of_a_voice_agent",
    onSelectDeck: vi.fn(),
    onStart: vi.fn(),
    onStop: vi.fn(),
    onSend: vi.fn(),
    ...overrides,
  };
  return { props, view: render(<Controls {...props} />) };
}

describe("Controls", () => {
  it("TC-FE-133: sends the trimmed question and empties the field", () => {
    const { props } = renderControls();
    const input = screen.getByLabelText(COMPOSER_LABEL);

    fireEvent.change(input, { target: { value: "  how do you handle interruptions?  " } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));

    expect(props.onSend).toHaveBeenCalledExactlyOnceWith("how do you handle interruptions?");
    expect(input).toHaveValue("");
  });

  it("TC-FE-134: refuses an empty question, and refuses any question without a session", () => {
    const { props, view } = renderControls();
    const input = screen.getByLabelText(COMPOSER_LABEL);
    const send = screen.getByRole("button", { name: "Ask" });

    expect(send).toBeDisabled();
    fireEvent.change(input, { target: { value: "   " } });
    expect(send).toBeDisabled();

    fireEvent.change(input, { target: { value: "what is this?" } });
    expect(send).toBeEnabled();

    // The composer stays visible and typable with no session, because that is what tells a new
    // user what the app is for (PRD F13); only sending is refused.
    view.rerender(<Controls {...props} canSend={false} isActive={false} />);
    expect(screen.getByLabelText(COMPOSER_LABEL)).toBeEnabled();
    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();

    fireEvent.submit(screen.getByLabelText(COMPOSER_LABEL).closest("form")!);
    expect(props.onSend).not.toHaveBeenCalled();
  });

  it("TC-FE-135: starts and ends the session, and offers a deck picker only when there is a choice", () => {
    const { props, view } = renderControls({ isActive: false });

    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Start session" }));
    expect(props.onStart).toHaveBeenCalledOnce();
    expect(props.onStop).not.toHaveBeenCalled();

    view.rerender(<Controls {...props} isActive={false} decks={TWO_DECKS} />);
    const picker = screen.getByRole("combobox");
    fireEvent.change(picker, { target: { value: "history_of_jazz" } });
    expect(props.onSelectDeck).toHaveBeenCalledExactlyOnceWith("history_of_jazz");

    view.rerender(<Controls {...props} isActive decks={TWO_DECKS} />);
    // Switching decks mid-session would leave the deck on screen disagreeing with the agent's.
    expect(screen.getByRole("combobox")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "End session" }));
    expect(props.onStop).toHaveBeenCalledOnce();
  });

  it("shows the orb's state in words next to the composer", () => {
    renderControls({ orbState: "thinking" });

    expect(screen.getByRole("status")).toHaveTextContent("Thinking");
  });

  it("counts down the remaining characters as the limit approaches", () => {
    renderControls();
    const input = screen.getByLabelText(COMPOSER_LABEL);

    expect(input).toHaveAttribute("maxlength", String(MAX_TEXT_INPUT_CHARS));
    expect(
      screen.queryByText(new RegExp(`/ ${String(MAX_TEXT_INPUT_CHARS)}`)),
    ).not.toBeInTheDocument();

    fireEvent.change(input, { target: { value: "x".repeat(MAX_TEXT_INPUT_CHARS) } });
    expect(
      screen.getByText(`${String(MAX_TEXT_INPUT_CHARS)} / ${String(MAX_TEXT_INPUT_CHARS)}`),
    ).toBeInTheDocument();
  });
});
