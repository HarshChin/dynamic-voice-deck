import { useEffect, useState, type JSX } from "react";

import { describeWait } from "../time";

import styles from "./FallbackBanner.module.css";

/** How often the "back in" estimate is redrawn. */
const TICK_MS = 1_000;

/** Inputs to the banner. */
export interface FallbackBannerProps {
  /** The model that could not answer, or `null` when nothing has been substituted. */
  readonly fromModel: string | null;
  /** The model answering instead. */
  readonly toModel: string | null;
  /** Epoch milliseconds until the usual model is expected back, or `null`. */
  readonly until?: number | null;
  /** Clock seam, so a test does not have to wait in real time. */
  readonly now?: () => number;
}

/**
 * Say plainly that a different model is answering, and why (TR-085, PRD F12).
 *
 * This is the one piece of the fallback the listener actually experiences: the voice gets slower.
 * Without an explanation that reads as degradation rather than breakage, a slow answer looks like
 * a fault. It replaces the rate-limit countdown rather than sitting beside it, because the two say
 * contradictory things -- one means "wait", and this means "carry on, it is already answering".
 *
 * @param props - Which model was substituted for which, and when the usual one returns.
 * @returns The banner, or nothing when no substitution is in force.
 */
export function FallbackBanner({
  fromModel,
  toModel,
  until = null,
  now = Date.now,
}: FallbackBannerProps): JSX.Element | null {
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

  if (fromModel === null || toModel === null) {
    return null;
  }

  const remaining = until === null ? 0 : until - now();
  return (
    <p className={styles.banner} role="status" aria-live="polite">
      <span className={styles.dot} aria-hidden="true" />
      <span>
        <strong>{fromModel}</strong> hit its rate limit — answering with <strong>{toModel}</strong>{" "}
        on this machine
        {remaining > 0 ? `, back in ${describeWait(remaining)}` : ""}
      </span>
    </p>
  );
}
