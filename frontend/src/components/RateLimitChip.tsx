import { useEffect, useState, type JSX } from "react";

import { describeWait } from "../time";

import styles from "./RateLimitChip.module.css";

/** How often the remaining time is redrawn. */
const TICK_MS = 500;

/** Inputs to the chip. */
export interface RateLimitChipProps {
  /** Epoch milliseconds until which the provider asked us to wait, or `null`. */
  readonly until: number | null;
  /** Clock seam, so a test does not have to wait in real time. */
  readonly now?: () => number;
}

/**
 * Say how long the free tier has asked us to wait (PRD F12, TR-171).
 *
 * The free tier's per-minute ceiling is genuinely reachable in a demo: a handful of questions in
 * quick succession is enough. Without this the agent simply goes quiet and the only explanation is
 * a line in a log the user may not have open, which reads as the thing being broken. A countdown
 * turns an outage into a wait, which is what it actually is.
 *
 * @param props - The instant to count down to, and an optional clock.
 * @returns The chip, or nothing when there is no wait to report.
 */
export function RateLimitChip({ until, now = Date.now }: RateLimitChipProps): JSX.Element | null {
  // The remaining time is read from the clock during render rather than held in state, so it is
  // never stale after a re-render; this state exists only to ask for those redraws.
  const [, redraw] = useState(0);

  useEffect(() => {
    if (until === null) {
      return;
    }
    const timer = window.setInterval(() => {
      redraw((count) => count + 1);
      if (now() >= until) {
        window.clearInterval(timer);
      }
    }, TICK_MS);
    return () => {
      window.clearInterval(timer);
    };
  }, [until, now]);

  const remaining = until === null ? 0 : until - now();
  if (remaining <= 0) {
    return null;
  }

  return (
    <p className={styles.chip} role="status" aria-live="polite">
      <span className={styles.dot} aria-hidden="true" />
      rate limited — free tier, ready in {describeWait(remaining)}
    </p>
  );
}
