import { useState, type FormEvent, type JSX } from "react";

import { MAX_TEXT_INPUT_CHARS, type SessionState } from "../protocol";
import type { DeckSummary } from "../session/useSession";

import { Orb } from "./Orb";
import styles from "./Controls.module.css";

/** Show the character counter only once the limit is close enough to matter. */
const COUNTER_VISIBLE_FROM = Math.floor(MAX_TEXT_INPUT_CHARS * 0.8);

/**
 * The composer's placeholder, which is where it explains itself.
 *
 * @param canSend - Whether a question would reach the server.
 * @param isAnswering - Whether the agent is mid-answer.
 * @returns The placeholder text.
 */
function placeholderFor(canSend: boolean, isAnswering: boolean): string {
  if (isAnswering) {
    return "The presenter is answering…";
  }
  return canSend ? "Ask about the deck…" : "Start a session, then ask about the deck…";
}

/**
 * The send button's tooltip, so a disabled button always says why.
 *
 * @param canSend - Whether a question would reach the server.
 * @param isAnswering - Whether the agent is mid-answer.
 * @returns The tooltip text.
 */
function sendHintFor(canSend: boolean, isAnswering: boolean): string {
  if (isAnswering) {
    return "Wait for the answer to finish";
  }
  return canSend ? "Send the question" : "Start a session first";
}

/** Inputs to the session control bar. */
export interface ControlsProps {
  /** What the orb portrays (PRD F11). */
  readonly orbState: SessionState;
  /** Whether a session is open or opening; decides Start vs End. */
  readonly isActive: boolean;
  /** Whether a question sent right now would reach the server. */
  readonly canSend: boolean;
  /** Whether the agent is mid-answer, in which case a new question would cancel it (TR-022). */
  readonly isAnswering: boolean;
  /** Decks the backend offers; the picker appears only when there is a choice. */
  readonly decks: readonly DeckSummary[];
  /** The deck the next session will open. */
  readonly deckId: string;
  /** Called with the id of the deck the user picked. */
  readonly onSelectDeck: (deckId: string) => void;
  /** Open a session. */
  readonly onStart: () => void;
  /** Close the session. */
  readonly onStop: () => void;
  /** Send a typed question. */
  readonly onSend: (text: string) => void;
}

/**
 * The session control bar: orb, composer, deck picker, start/stop (PRD F2, F13).
 *
 * The text composer is the widest thing in the bar because in Phase 1 it is the *only* way in —
 * the microphone path lands with the audio pipeline. It stays visible and focusable even with no
 * session open, so the first thing a new user does is type rather than hunt for a button.
 *
 * Sending waits while the agent is answering. A second question does not read as an interruption
 * over text — the server would cancel the first turn without ever saying so, leaving an answer in
 * the transcript that stops mid-thought and looks finished — so the composer says what it is
 * waiting for instead (PRD F13; barge-in is the voice path's job, PRD F7).
 *
 * @param props - Session state and the callbacks that change it.
 * @returns The control bar element.
 */
export function Controls({
  orbState,
  isActive,
  canSend,
  isAnswering,
  decks,
  deckId,
  onSelectDeck,
  onStart,
  onStop,
  onSend,
}: ControlsProps): JSX.Element {
  const [draft, setDraft] = useState("");
  const question = draft.trim();

  const acceptsQuestion = canSend && !isAnswering;

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (question.length === 0 || !acceptsQuestion) {
      return;
    }
    onSend(question);
    setDraft("");
  }

  return (
    <div className={styles.bar}>
      <Orb state={orbState} />

      <form className={styles.composer} onSubmit={handleSubmit}>
        <div className={styles.field}>
          <input
            className={styles.input}
            type="text"
            value={draft}
            maxLength={MAX_TEXT_INPUT_CHARS}
            autoComplete="off"
            aria-label="Ask the presenter a question"
            placeholder={placeholderFor(canSend, isAnswering)}
            onChange={(event) => {
              setDraft(event.target.value);
            }}
          />
          {draft.length >= COUNTER_VISIBLE_FROM && (
            <span className={styles.counter} aria-hidden="true">
              {draft.length} / {MAX_TEXT_INPUT_CHARS}
            </span>
          )}
        </div>
        <button
          className={styles.send}
          type="submit"
          disabled={!acceptsQuestion || question.length === 0}
          title={sendHintFor(canSend, isAnswering)}
        >
          Ask
        </button>
      </form>

      <div className={styles.session}>
        {decks.length > 1 && (
          <label className={styles.deck}>
            <span className={styles.deckLabel}>Deck</span>
            <select
              className={styles.select}
              value={deckId}
              disabled={isActive}
              onChange={(event) => {
                onSelectDeck(event.target.value);
              }}
            >
              {decks.map((summary) => (
                <option key={summary.id} value={summary.id}>
                  {summary.title}
                </option>
              ))}
            </select>
          </label>
        )}
        <button
          className={styles.primary}
          type="button"
          data-active={isActive ? "true" : undefined}
          onClick={isActive ? onStop : onStart}
        >
          {isActive ? "End session" : "Start session"}
        </button>
      </div>
    </div>
  );
}
