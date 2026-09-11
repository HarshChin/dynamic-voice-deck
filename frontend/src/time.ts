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
