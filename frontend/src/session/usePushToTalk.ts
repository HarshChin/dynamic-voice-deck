/**
 * Hold a key to talk (PRD F3, TR-115).
 *
 * Detection by loudness has one failure a person cannot work around: a room noisy enough that
 * every frame reads as speech, where the agent is interrupted by the room itself. A held key is
 * the escape hatch, and it is also simply what some people prefer -- it makes turn-taking explicit
 * rather than inferred.
 *
 * The binding is on the window rather than on a button so the key works wherever the user is
 * looking, which means it has to stand aside for typing, for browser and system shortcuts, and for
 * the key auto-repeating while it is held.
 */

import { useEffect, useState } from "react";

import { isTypingTarget } from "../keyboard";

/** The key that holds a turn open. */
export const PUSH_TO_TALK_KEY = " ";

/** What the hook needs to drive a held turn. */
export interface PushToTalkOptions {
  /** Whether the binding is live at all. */
  readonly enabled: boolean;
  /** The key went down: start capturing. */
  readonly onPress: () => void;
  /** The key came up, or the window lost focus: finish the turn. */
  readonly onRelease: () => void;
}

/**
 * Bind the space bar to a turn while push-to-talk is on.
 *
 * @param options - Whether the binding is live and what to call.
 * @returns Whether the key is down right now, so the UI can show it.
 */
export function usePushToTalk({ enabled, onPress, onRelease }: PushToTalkOptions): boolean {
  const [held, setHeld] = useState(false);

  useEffect(() => {
    if (!enabled) {
      return;
    }
    // Held state as a closure variable and not the React one: the listeners are registered once,
    // and reading state through a stale closure would let a second keydown open a second turn.
    let down = false;

    function release(): void {
      if (!down) {
        return;
      }
      down = false;
      setHeld(false);
      onRelease();
    }

    function handleKeyDown(event: KeyboardEvent): void {
      if (event.key !== PUSH_TO_TALK_KEY || event.repeat || down) {
        return;
      }
      if (event.altKey || event.ctrlKey || event.metaKey || isTypingTarget(event.target)) {
        return;
      }
      // Without this the page scrolls, and a focused button would be pressed again on release.
      event.preventDefault();
      down = true;
      setHeld(true);
      onPress();
    }

    function handleKeyUp(event: KeyboardEvent): void {
      if (event.key !== PUSH_TO_TALK_KEY) {
        return;
      }
      // Not guarded by `isTypingTarget`: focus can move between press and release, and a turn that
      // never ends is far worse than one ended by a key the composer also saw.
      release();
    }

    window.addEventListener("keydown", handleKeyDown);
    window.addEventListener("keyup", handleKeyUp);
    // A key held while switching windows never reports its release, which would leave the
    // microphone open indefinitely; treat losing focus as the release.
    window.addEventListener("blur", release);

    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("keyup", handleKeyUp);
      window.removeEventListener("blur", release);
      // Turning the mode off mid-hold must not strand the turn either.
      release();
    };
  }, [enabled, onPress, onRelease]);

  return held && enabled;
}
