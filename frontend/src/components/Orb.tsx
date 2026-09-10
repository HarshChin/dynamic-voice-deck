import type { JSX } from "react";

import type { SessionState } from "../protocol";

import styles from "./Orb.module.css";

/** The words shown under the orb for one state. */
interface OrbCopy {
  /** The accessible name of the indicator, phrased for a screen reader rather than as an enum. */
  readonly label: string;
  /** One line of context, so the orb explains what it wants from the user. */
  readonly hint: string;
}

/**
 * What each state says under the orb (PRD F11).
 *
 * Keyed by plain strings rather than by `SessionState` on purpose. `interrupted` is drawn by the
 * state machine in PRD §7 but may or may not reach the client as a `state` frame — the backend is
 * deciding whether to emit it or fold it into `listening` — and the orb must neither depend on the
 * protocol still listing it nor lose the copy if it does. Coverage of the states the protocol
 * *does* list is asserted by TC-FE-120, which walks `SESSION_STATES`.
 */
const ORB_COPY: Readonly<Record<string, OrbCopy>> = {
  idle: { label: "Idle", hint: "no session" },
  connecting: { label: "Connecting", hint: "opening the socket" },
  listening: { label: "Listening", hint: "ask a question" },
  hearing: { label: "Hearing you", hint: "go ahead" },
  thinking: { label: "Thinking", hint: "reading the deck" },
  speaking: { label: "Speaking", hint: "cut in any time" },
  interrupted: { label: "Interrupted", hint: "stopped" },
  error: { label: "Error", hint: "see the log" },
};

/**
 * Copy for a state this build has never heard of.
 *
 * `SessionState` is a compile-time union, but the value itself arrives over a socket from a server
 * that may be a version ahead. Naming the state is more use to whoever is watching than a blank
 * indicator, and it can never throw.
 *
 * @param state - The unrecognised state, as received.
 * @returns Copy that shows the raw value.
 */
function unknownCopy(state: string): OrbCopy {
  return { label: state, hint: "unrecognised state" };
}

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
  const copy = ORB_COPY[state] ?? unknownCopy(state);
  return (
    <div className={styles.orb} data-state={state} data-compact={compact ? "true" : undefined}>
      <span className={styles.halo} aria-hidden="true" />
      <span className={styles.ring} aria-hidden="true" />
      <span className={styles.core} aria-hidden="true" />
      <span className={styles.caption} role="status" aria-live="polite">
        <span className={styles.label}>{copy.label}</span>
        {!compact && <span className={styles.hint}>{copy.hint}</span>}
      </span>
    </div>
  );
}
