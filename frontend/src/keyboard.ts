/**
 * Shared keyboard helpers.
 *
 * The app binds two global shortcuts -- the arrow keys to move the deck and the space bar to hold
 * a turn open -- and both have to answer the same question before acting: would the focused
 * element have done something with this key itself? A shortcut that fires while someone is writing
 * a question, or that swallows the space that was meant to tick a checkbox, is a bug.
 *
 * The two cases are separate because the keys are. Arrow keys are text navigation, so they stand
 * aside for anything that holds text. Space activates buttons and checkboxes, so it stands aside
 * for those as well.
 */

/**
 * Input types that hold text a caret can move through.
 *
 * Everything absent from this set -- a checkbox, a radio, a file picker -- is an input that arrow
 * keys do nothing useful in, so a global shortcut is free to act while one is focused.
 */
const TEXT_INPUT_TYPES: ReadonlySet<string> = new Set([
  "text",
  "search",
  "email",
  "url",
  "password",
  "tel",
  "number",
  "date",
  "datetime-local",
  "month",
  "time",
  "week",
]);

/** Input types the space bar operates, where a push-to-talk binding would steal the keypress. */
const SPACE_ACTIVATED_INPUT_TYPES: ReadonlySet<string> = new Set([
  "checkbox",
  "radio",
  "button",
  "submit",
  "reset",
  "file",
  "image",
  "color",
]);

/**
 * Whether a key event came from somewhere text is being entered.
 *
 * @param target - The event's target.
 * @returns `true` when a text-navigation shortcut should stand aside.
 */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  if (target.isContentEditable) {
    return true;
  }
  const tag = target.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") {
    return true;
  }
  if (tag !== "INPUT") {
    return false;
  }
  // A missing or unrecognised type is a text field: that is what the HTML default is.
  const type = (target as HTMLInputElement).type.toLowerCase();
  return TEXT_INPUT_TYPES.has(type) || !SPACE_ACTIVATED_INPUT_TYPES.has(type);
}

/**
 * Whether the space bar would operate the focused element itself.
 *
 * Buttons, checkboxes and radios are all driven by space, and so is anything a component has
 * declared a button through its role. Holding space to talk must not also press them.
 *
 * @param target - The event's target.
 * @returns `true` when a space-bar shortcut should stand aside.
 */
export function activatesOnSpace(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  const tag = target.tagName;
  if (tag === "BUTTON" || tag === "SUMMARY" || tag === "SELECT") {
    return true;
  }
  if (target.getAttribute("role") === "button" || target.getAttribute("role") === "checkbox") {
    return true;
  }
  if (tag !== "INPUT") {
    return false;
  }
  return SPACE_ACTIVATED_INPUT_TYPES.has((target as HTMLInputElement).type.toLowerCase());
}
