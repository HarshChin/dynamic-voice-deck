import type { JSX } from "react";

import type { SessionState } from "../protocol";

import styles from "./Orb.module.css";

/**
 * What each state is called in the label beneath the orb (PRD F11).
 *
 * The words are the accessible name of the indicator, so they are phrased for a person hearing
 * them read out rather than as protocol enum values.
 */
const ORB_LABELS: Readonly<Record<SessionState, string>> = {
  idle: "Idle",
  connecting: "Connecting",
  listening: "Listening",
  hearing: "Hearing you",
  thinking: "Thinking",
  speaking: "Speaking",
  interrupted: "Interrupted",
  error: "Error",
};

/** One line of context under the label, so the orb explains what it wants from the user. */
const ORB_HINTS: Readonly<Record<SessionState, string>> = {
  idle: "no session",
  connecting: "opening the socket",
  listening: "ask a question",
  hearing: "go ahead",
  thinking: "reading the deck",
  speaking: "cut in any time",
  interrupted: "stopped",
  error: "see the log",
};

/** Inputs to the agent state indicator. */
export interface OrbProps {
  /** The state to portray; in Phase 1 this comes from the session, not from audio levels. */
  readonly state: SessionState;
  /** Hide the hint line where space is tight. */
  readonly compact?: boolean;
}

/**
 * The agent state indicator (PRD F11, TR-134).
 *
 * Phase 1 has no audio, so every state animates from the state alone; the amplitude-reactive
 * variants of `hearing` and `speaking` arrive with the audio pipeline and will feed the same
 * `data-state` hook. Colour is never the only signal: the state is also written underneath in a
 * live region, which is what a screen reader announces and what the tests assert on.
 *
 * @param props - The state to show and whether to drop the hint line.
 * @returns The indicator element.
 */
export function Orb({ state, compact = false }: OrbProps): JSX.Element {
  return (
    <div className={styles.orb} data-state={state} data-compact={compact ? "true" : undefined}>
      <span className={styles.halo} aria-hidden="true" />
      <span className={styles.ring} aria-hidden="true" />
      <span className={styles.core} aria-hidden="true" />
      <span className={styles.caption} role="status" aria-live="polite">
        <span className={styles.label}>{ORB_LABELS[state]}</span>
        {!compact && <span className={styles.hint}>{ORB_HINTS[state]}</span>}
      </span>
    </div>
  );
}
