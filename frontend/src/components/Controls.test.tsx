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
    isAnswering: false,
    decks: ONE_DECK,
    deckId: "anatomy_of_a_voice_agent",
    onSelectDeck: vi.fn(),
    onStart: vi.fn(),
    onStop: vi.fn(),
    onSend: vi.fn(),
    onPresent: vi.fn(),
    muted: false,
    onToggleMute: vi.fn(),
    pushToTalk: false,
    onTogglePushToTalk: vi.fn(),
    pushHeld: false,
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

  it("TC-FE-151: waits for the answer rather than cancelling it with a second question", () => {
    const { props, view } = renderControls();
    const input = screen.getByLabelText(COMPOSER_LABEL);

    fireEvent.change(input, { target: { value: "and what about the LLM?" } });
    expect(screen.getByRole("button", { name: "Ask" })).toBeEnabled();

    view.rerender(<Controls {...props} isAnswering />);

    // The field keeps the draft and stays typable; only sending waits, and the placeholder says
    // what it is waiting for rather than leaving a dead button unexplained.
    expect(screen.getByLabelText(COMPOSER_LABEL)).toBeEnabled();
    expect(screen.getByLabelText(COMPOSER_LABEL)).toHaveValue("and what about the LLM?");
    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Ask" })).toHaveAttribute(
      "title",
      "Wait for the answer to finish",
    );
    expect(screen.getByPlaceholderText("The presenter is answering…")).toBeInTheDocument();

    fireEvent.submit(screen.getByLabelText(COMPOSER_LABEL).closest("form")!);
    expect(props.onSend).not.toHaveBeenCalled();

    // When the answer ends the draft is still there to send, so nothing the user typed is lost.
    view.rerender(<Controls {...props} isAnswering={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(props.onSend).toHaveBeenCalledExactlyOnceWith("and what about the LLM?");
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

describe("walkthrough and mute", () => {
  it("TC-FE-130: offers a walkthrough while a session is open", () => {
    const { props } = renderControls({ isActive: true });

    fireEvent.click(screen.getByRole("button", { name: /walk me through/i }));

    expect(props.onPresent).toHaveBeenCalledOnce();
  });

  it("TC-FE-131: hides the walkthrough and mute before a session starts", () => {
    renderControls({ isActive: false });

    expect(screen.queryByRole("button", { name: /walk me through/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /mute/i })).toBeNull();
  });

  it("TC-FE-132: mute is a visible state, not just a toggle", () => {
    const { props } = renderControls({ isActive: true, muted: false });

    const button = screen.getByRole("button", { name: /^mute$/i });
    expect(button).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(button);

    expect(props.onToggleMute).toHaveBeenCalledWith(true);
  });

  it("TC-FE-133: offers to unmute once muted", () => {
    renderControls({ isActive: true, muted: true });

    expect(screen.getByRole("button", { name: /unmute/i })).toHaveAttribute("aria-pressed", "true");
  });
});

describe("push to talk (TR-115)", () => {
  it("offers the mode while a session is open and reports the switch", () => {
    const { props } = renderControls();

    fireEvent.click(screen.getByRole("button", { name: "Push to talk" }));

    expect(props.onTogglePushToTalk).toHaveBeenCalledExactlyOnceWith(true);
  });

  it("says which key to hold, and says when it is being held", () => {
    const { props, view } = renderControls({ pushToTalk: true });

    expect(screen.getByText("hold space to talk")).toBeInTheDocument();

    view.rerender(<Controls {...props} pushToTalk pushHeld />);
    expect(screen.getByText("listening — release to send")).toBeInTheDocument();
  });

  it("says nothing about a key when the mode is off, or when no session is open", () => {
    const { props, view } = renderControls({ pushToTalk: false });
    expect(screen.queryByText(/hold space/)).not.toBeInTheDocument();

    view.rerender(<Controls {...props} pushToTalk isActive={false} />);
    expect(screen.queryByText(/hold space/)).not.toBeInTheDocument();
  });
});

describe("push-to-talk focus (TR-115)", () => {
  it("TC-FE-197: a mouse click releases the button, so the space bar is free to talk", () => {
    renderControls();
    const toggle = screen.getByRole("button", { name: "Push to talk" });
    toggle.focus();

    fireEvent.click(toggle, { detail: 1 });

    expect(toggle).not.toHaveFocus();
  });

  it("TC-FE-198: a keyboard activation keeps focus, so the same key can turn it off again", () => {
    renderControls();
    const toggle = screen.getByRole("button", { name: "Push to talk" });
    toggle.focus();

    // A click event with `detail: 0` is what a browser dispatches for Space or Enter on a button.
    fireEvent.click(toggle, { detail: 0 });

    expect(toggle).toHaveFocus();
  });
});

describe("every bar button releases focus after a pointer click (TR-115)", () => {
  it.each(["Walk me through it", "Mute", "End session"])(
    "TC-FE-236: clicking %s with the mouse leaves the space bar free to talk",
    (name) => {
      renderControls();
      const button = screen.getByRole("button", { name });
      button.focus();

      fireEvent.click(button, { detail: 1 });

      // The bug: "click Walk me through it, then hold space to interrupt" did nothing, because
      // the held space went to the still-focused button.
      expect(button).not.toHaveFocus();
    },
  );

  it("TC-FE-237: a keyboard activation keeps focus, so the same key can press it again", () => {
    renderControls();
    const button = screen.getByRole("button", { name: "Walk me through it" });
    button.focus();

    fireEvent.click(button, { detail: 0 });

    expect(button).toHaveFocus();
  });
});
