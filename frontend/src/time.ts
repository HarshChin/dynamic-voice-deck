/** Small time helpers shared by the countdown UI. */

/**
 * Round a remaining span up to whole seconds.
 *
 * Up rather than down: a chip that says "1s" for 1400 ms and then sits there is worse than one
 * that says "2s" and counts honestly to zero.
 *
 * @param ms - Milliseconds remaining.
 * @returns Whole seconds, never negative.
 */
export function secondsLeft(ms: number): number {
  return Math.max(0, Math.ceil(ms / 1000));
}

/** Above this, a countdown in seconds stops being a number anybody reads. */
const MINUTES_FROM_S = 90;

/**
 * Describe a remaining span the way a person would say it.
 *
 * Seconds while the wait is short enough to sit through, minutes once it is not: "ready in 877s"
 * is a number, and "ready in 15 min" is an answer to the question the user is actually asking,
 * which is whether to wait or to do something else.
 *
 * @param ms - Milliseconds remaining.
 * @returns A short phrase such as `"12s"` or `"15 min"`.
 */
export function describeWait(ms: number): string {
  const seconds = secondsLeft(ms);
  if (seconds < MINUTES_FROM_S) {
    return `${String(seconds)}s`;
  }
  return `${String(Math.ceil(seconds / 60))} min`;
}
