import { type JSX } from "react";

import styles from "./FallbackBanner.module.css";

/** Inputs to the banner. */
export interface FallbackBannerProps {
  /** The model that could not answer, or `null` when nothing has been substituted. */
  readonly fromModel: string | null;
  /** The model answering instead. */
  readonly toModel: string | null;
}

/**
 * Say plainly that a different model is answering, and why (TR-085, PRD F12).
 *
 * This is the one piece of the fallback the listener actually experiences: the voice gets slower.
 * Without an explanation that reads as degradation rather than breakage, a slow answer looks like
 * a fault.
 *
 * It says who is speaking and nothing about time. The countdown belongs to `RateLimitChip`, which
 * stays up beside this while the hosted model is still refusing: one element owns *when the usual
 * model returns*, the other owns *who is answering meanwhile*, and neither repeats the other.
 *
 * @param props - Which model was substituted for which.
 * @returns The banner, or nothing when no substitution is in force.
 */
export function FallbackBanner({ fromModel, toModel }: FallbackBannerProps): JSX.Element | null {
  if (fromModel === null || toModel === null) {
    return null;
  }
  return (
    <p className={styles.banner} role="status" aria-live="polite">
      <span className={styles.dot} aria-hidden="true" />
      <span>
        <strong>{fromModel}</strong> hit its rate limit — answering with <strong>{toModel}</strong>{" "}
        on this machine
      </span>
    </p>
  );
}
