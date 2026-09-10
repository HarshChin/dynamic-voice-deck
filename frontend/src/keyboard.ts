/**
 * Shared keyboard helpers.
 *
 * The app binds two global shortcuts -- the arrow keys to move the deck and the space bar to hold
 * a turn open -- and both need the same answer to the same question: is the person typing right
 * now? A shortcut that fires while someone is writing a question in the composer is a bug, so the
 * test for it lives in one place rather than being written twice.
 */

/**
 * Whether a key event came from somewhere text is being entered.
 *
 * @param target - The event's target.
 * @returns `true` when a global shortcut should stand aside.
 */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  if (target.isContentEditable) {
    return true;
  }
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}
