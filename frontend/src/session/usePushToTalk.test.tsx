import { act, render, screen } from "@testing-library/react";
import type { JSX } from "react";
import { beforeEach, describe, expect, it } from "vitest";

import { usePushToTalk } from "./usePushToTalk";

let presses = 0;
let releases = 0;

function onPress(): void {
  presses += 1;
}

function onRelease(): void {
  releases += 1;
}

/**
 * A component that binds the key and shows whether it is down.
 *
 * @param props - Whether the binding is live.
 * @returns The harness element.
 */
function Harness({ enabled }: { enabled: boolean }): JSX.Element {
  const held = usePushToTalk({ enabled, onPress, onRelease });
  return (
    <div>
      <p data-testid="held">{held ? "held" : "free"}</p>
      <input aria-label="composer" />
    </div>
  );
}

/** Press the space bar, returning the event so a test can inspect what was prevented. */
function pressSpace(init: KeyboardEventInit = {}): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { key: " ", cancelable: true, ...init });
  act(() => {
    window.dispatchEvent(event);
  });
  return event;
}

function releaseSpace(): void {
  act(() => {
    window.dispatchEvent(new KeyboardEvent("keyup", { key: " " }));
  });
}

beforeEach(() => {
  presses = 0;
  releases = 0;
});

describe("usePushToTalk", () => {
  it("TR-115: opens a turn on the way down and closes it on the way up", () => {
    render(<Harness enabled />);

    pressSpace();
    expect(presses).toBe(1);
    expect(screen.getByTestId("held")).toHaveTextContent("held");

    releaseSpace();
    expect(releases).toBe(1);
    expect(screen.getByTestId("held")).toHaveTextContent("free");
  });

  it("swallows the keypress so the page does not scroll", () => {
    render(<Harness enabled />);

    expect(pressSpace().defaultPrevented).toBe(true);
  });

  it("auto-repeat while the key is held does not open a second turn", () => {
    render(<Harness enabled />);

    pressSpace();
    pressSpace({ repeat: true });
    pressSpace({ repeat: true });

    expect(presses).toBe(1);
  });

  it("a repeat flag the browser forgot to set is still only one turn", () => {
    render(<Harness enabled />);

    pressSpace();
    pressSpace();

    expect(presses).toBe(1);
  });

  it("stands aside while the user is typing a question", () => {
    render(<Harness enabled />);
    const composer = screen.getByLabelText("composer");
    composer.focus();

    const event = new KeyboardEvent("keydown", { key: " ", cancelable: true, bubbles: true });
    act(() => {
      composer.dispatchEvent(event);
    });

    expect(presses).toBe(0);
    expect(event.defaultPrevented).toBe(false);
  });

  it("a released key still ends the turn when focus moved to the composer meanwhile", () => {
    render(<Harness enabled />);
    pressSpace();

    const composer = screen.getByLabelText("composer");
    act(() => {
      composer.dispatchEvent(new KeyboardEvent("keyup", { key: " ", bubbles: true }));
    });

    expect(releases).toBe(1);
  });

  it("ignores a modified space, which belongs to the browser", () => {
    render(<Harness enabled />);

    pressSpace({ metaKey: true });
    pressSpace({ ctrlKey: true });
    pressSpace({ altKey: true });

    expect(presses).toBe(0);
  });

  it("ignores every other key", () => {
    render(<Harness enabled />);

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight" }));
      window.dispatchEvent(new KeyboardEvent("keyup", { key: "ArrowRight" }));
    });

    expect(presses).toBe(0);
    expect(releases).toBe(0);
  });

  it("losing the window while the key is down ends the turn rather than stranding it", () => {
    render(<Harness enabled />);
    pressSpace();

    act(() => {
      window.dispatchEvent(new Event("blur"));
    });

    expect(releases).toBe(1);
    expect(screen.getByTestId("held")).toHaveTextContent("free");
    // The stranded turn is closed once, not once per event.
    releaseSpace();
    expect(releases).toBe(1);
  });

  it("turning the mode off mid-hold ends the turn", () => {
    const view = render(<Harness enabled />);
    pressSpace();

    view.rerender(<Harness enabled={false} />);

    expect(releases).toBe(1);
    expect(screen.getByTestId("held")).toHaveTextContent("free");
  });

  it("does nothing at all while disabled", () => {
    render(<Harness enabled={false} />);

    const event = pressSpace();
    releaseSpace();

    expect(presses).toBe(0);
    expect(releases).toBe(0);
    expect(event.defaultPrevented).toBe(false);
  });

  it("unmounting mid-hold ends the turn and unbinds the key", () => {
    const view = render(<Harness enabled />);
    pressSpace();

    view.unmount();
    expect(releases).toBe(1);

    pressSpace();
    expect(presses).toBe(1);
  });
});

describe("keys the focused element already owns", () => {
  it("TC-FE-195: holding space on a focused button presses it rather than starting a turn", () => {
    render(<Harness enabled />);
    const button = document.createElement("button");
    document.body.append(button);
    button.focus();

    const event = new KeyboardEvent("keydown", { key: " ", cancelable: true, bubbles: true });
    act(() => {
      button.dispatchEvent(event);
    });

    expect(presses).toBe(0);
    expect(event.defaultPrevented).toBe(false);
    button.remove();
  });

  it("TC-FE-196: holding space on a checkbox ticks it rather than starting a turn", () => {
    render(<Harness enabled />);
    const box = document.createElement("input");
    box.type = "checkbox";
    document.body.append(box);
    box.focus();

    act(() => {
      box.dispatchEvent(
        new KeyboardEvent("keydown", { key: " ", cancelable: true, bubbles: true }),
      );
    });

    expect(presses).toBe(0);
    box.remove();
  });
});
