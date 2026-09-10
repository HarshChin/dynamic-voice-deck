import { useEffect, useState, type JSX } from "react";

import type { Slide as SlideModel } from "../protocol";

import styles from "./Slide.module.css";

/**
 * How long a highlighted bullet stays emphasised (PRD F1).
 *
 * The agent highlights a bullet as it talks about it; the emphasis is a pointer, not a selection,
 * so it fades on its own rather than waiting for something to clear it.
 */
const HIGHLIGHT_DURATION_MS = 4_000;

/** Inputs to one rendered slide. */
export interface SlideProps {
  /** The slide to render. */
  readonly slide: SlideModel;
  /** Zero-based index of the bullet to emphasise, or `null` for none. */
  readonly highlight: number | null;
}

/**
 * One slide: a title and its bullets, with an optional emphasised bullet.
 *
 * The highlight is owned here rather than in the store because it is a piece of presentation
 * timing, not session state: the server says *which* bullet matters, and this component decides
 * how long that stays true (PRD F1 — four seconds, or until the next highlight arrives).
 *
 * @param props - The slide and the bullet to emphasise.
 * @returns The slide element.
 */
export function Slide({ slide, highlight }: SlideProps): JSX.Element {
  // One highlight is identified by the slide it lives on and the bullet it points at. Deriving the
  // emphasis from that key rather than mirroring the prop into state means a new highlight
  // replaces the previous one during the same render, with no intermediate frame showing both.
  const key = `${String(slide.index)}:${highlight === null ? "none" : String(highlight)}`;
  const [expiredKey, setExpiredKey] = useState<string | null>(null);
  const active = highlight !== null && expiredKey !== key ? highlight : null;

  useEffect(() => {
    if (highlight === null) {
      return undefined;
    }
    const timer = setTimeout(() => {
      setExpiredKey(key);
    }, HIGHLIGHT_DURATION_MS);
    // Clearing on cleanup is what makes "or until the next highlight" true: the outgoing timer
    // cannot fire after its highlight has been replaced.
    return () => {
      clearTimeout(timer);
    };
  }, [highlight, key]);

  return (
    <article className={styles.slide} aria-labelledby={`slide-title-${String(slide.index)}`}>
      <header className={styles.header}>
        <p className={styles.eyebrow}>Slide {slide.index}</p>
        <h2 className={styles.title} id={`slide-title-${String(slide.index)}`}>
          {slide.title}
        </h2>
      </header>
      <ul className={styles.bullets}>
        {slide.bullets.map((bullet, index) => (
          <li
            className={styles.bullet}
            key={`${String(slide.index)}-${String(index)}`}
            data-highlighted={index === active ? "true" : undefined}
          >
            <span className={styles.marker} aria-hidden="true" />
            <span className={styles.text}>{bullet}</span>
          </li>
        ))}
      </ul>
    </article>
  );
}
