import type { JSX } from "react";

import styles from "./ProgressDots.module.css";

/** Inputs to the deck's position indicator. */
export interface ProgressDotsProps {
  /** Titles of every slide, in order; the length is the deck size. */
  readonly titles: readonly string[];
  /** The 1-based index currently on screen. */
  readonly current: number;
  /** Called with the 1-based index of the slide the user picked. */
  readonly onSelect: (index: number) => void;
}

/**
 * The row of dots under the deck (PRD F1).
 *
 * Each dot is a real button rather than a decorated span: clicking one jumps to that slide, and a
 * keyboard user can reach every slide by tabbing. The active dot is marked with `aria-current` so
 * position is announced, not merely drawn.
 *
 * @param props - Slide titles, the current index, and the navigation callback.
 * @returns The indicator element.
 */
export function ProgressDots({ titles, current, onSelect }: ProgressDotsProps): JSX.Element {
  return (
    <nav className={styles.dots} aria-label="Slides">
      {titles.map((title, index) => {
        const slideNumber = index + 1;
        const isCurrent = slideNumber === current;
        return (
          <button
            className={styles.dot}
            key={slideNumber}
            type="button"
            aria-current={isCurrent ? "true" : undefined}
            aria-label={`Slide ${String(slideNumber)}: ${title}`}
            onClick={() => {
              onSelect(slideNumber);
            }}
          >
            <span className={styles.track} aria-hidden="true" />
          </button>
        );
      })}
    </nav>
  );
}
