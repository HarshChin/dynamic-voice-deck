import { useEffect, type JSX } from "react";

import type { Slide as SlideModel } from "../protocol";

import { ProgressDots } from "./ProgressDots";
import { Slide } from "./Slide";
import styles from "./SlideDeck.module.css";

/** Inputs to the deck viewport. */
export interface SlideDeckProps {
  /** Every slide in the deck, in order. */
  readonly slides: readonly SlideModel[];
  /** The 1-based index on screen; callers are expected to have clamped it (TR-133). */
  readonly current: number;
  /** Zero-based bullet to emphasise on the current slide, or `null`. */
  readonly highlight: number | null;
  /** Called with a 1-based index whenever the user navigates by hand (PRD F9). */
  readonly onNavigate: (index: number) => void;
}

/**
 * Whether a keystroke belongs to something the user is typing into.
 *
 * Arrow keys move the caret inside the composer, so the deck must not also swallow them; without
 * this check, typing a question would flick through the slides.
 *
 * @param target - The event target that received the keystroke.
 * @returns `true` when the key should be left alone.
 */
function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  if (target.isContentEditable) {
    return true;
  }
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

/**
 * The deck viewport: one slide, its neighbours a keypress away (PRD F1).
 *
 * Navigation is deliberately reported upward rather than applied here, because a manual move is
 * also a protocol event: the session tells the agent where the user went so its next answer talks
 * about what is on screen (PRD F9). The 200 ms transition comes from remounting the slide on each
 * index change — the `key` — which is enough to replay the entry animation without a transition
 * library, and which the global reduced-motion rule collapses to nothing.
 *
 * @param props - The slides, the current index, the highlight, and the navigation callback.
 * @returns The deck element.
 */
export function SlideDeck({ slides, current, highlight, onNavigate }: SlideDeckProps): JSX.Element {
  const slideCount = slides.length;
  const slide = slides[current - 1] ?? null;

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent): void {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") {
        return;
      }
      if (event.altKey || event.ctrlKey || event.metaKey || isTypingTarget(event.target)) {
        return;
      }
      const target = current + (event.key === "ArrowRight" ? 1 : -1);
      if (target < 1 || target > slideCount) {
        return;
      }
      event.preventDefault();
      onNavigate(target);
    }

    // Bound to the window, not the deck: the user should be able to arrow through the slides
    // without first clicking on them.
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [current, slideCount, onNavigate]);

  return (
    <section className={styles.deck} aria-label="Slide deck">
      <div className={styles.frame}>
        {slide === null ? (
          <p className={styles.empty}>No slide to show.</p>
        ) : (
          <div className={styles.pane} key={slide.index}>
            <Slide slide={slide} highlight={highlight} />
          </div>
        )}
      </div>
      <div className={styles.controls}>
        <button
          className={styles.arrow}
          type="button"
          aria-label="Previous slide"
          disabled={current <= 1}
          onClick={() => {
            onNavigate(current - 1);
          }}
        >
          <span aria-hidden="true">&#8592;</span>
        </button>
        <ProgressDots
          titles={slides.map((entry) => entry.title)}
          current={current}
          onSelect={onNavigate}
        />
        <button
          className={styles.arrow}
          type="button"
          aria-label="Next slide"
          disabled={current >= slideCount}
          onClick={() => {
            onNavigate(current + 1);
          }}
        >
          <span aria-hidden="true">&#8594;</span>
        </button>
        <p className={styles.counter} aria-hidden="true">
          {current} / {slideCount}
        </p>
      </div>
    </section>
  );
}
