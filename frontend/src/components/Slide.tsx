import { useEffect, useState, type JSX } from "react";

import type { Figure, FigureItem, Slide as SlideModel } from "../protocol";

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
 * The points an item presents, in the order the item asked for them.
 *
 * @param slide - The slide being rendered.
 * @param item - The card, column or step.
 * @returns The bullet texts paired with their indices, so a highlight can still find them.
 */
function pointsOf(slide: SlideModel, item: FigureItem): { index: number; text: string }[] {
  return item.bullets
    .map((index) => ({ index, text: slide.bullets[index] ?? "" }))
    .filter((point) => point.text !== "");
}

/**
 * The plain list, used when a slide declares no arrangement.
 *
 * @param props - The slide and the bullet to emphasise.
 * @returns The list element.
 */
function BulletList({ slide, active }: { slide: SlideModel; active: number | null }): JSX.Element {
  return (
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
  );
}

/**
 * Value cards, for a slide whose points are mostly measurements.
 *
 * @param props - The slide, its figure, and the bullet to emphasise.
 * @returns The grid element.
 */
function MetricCards({
  slide,
  figure,
  active,
}: {
  slide: SlideModel;
  figure: Figure;
  active: number | null;
}): JSX.Element {
  return (
    <ul className={styles.metrics} data-count={figure.items.length}>
      {figure.items.map((item, position) => {
        const points = pointsOf(slide, item);
        return (
          <li
            className={styles.metric}
            key={`${String(slide.index)}-m-${String(position)}`}
            data-highlighted={points.some((p) => p.index === active) ? "true" : undefined}
          >
            <p className={styles.metricValue}>{item.heading}</p>
            <p className={styles.metricCaption}>{item.caption}</p>
            {/* The bullet itself stays in the DOM: it is what the agent is talking about, and a
                caption is a label rather than a replacement for it. */}
            <p className={styles.metricPoint}>{points.map((p) => p.text).join(" ")}</p>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * Labelled columns, for a slide with two or three sides to it.
 *
 * @param props - The slide, its figure, and the bullet to emphasise.
 * @returns The columns element.
 */
function SplitColumns({
  slide,
  figure,
  active,
}: {
  slide: SlideModel;
  figure: Figure;
  active: number | null;
}): JSX.Element {
  return (
    <div className={styles.split} data-count={figure.items.length}>
      {figure.items.map((item, position) => (
        <section className={styles.column} key={`${String(slide.index)}-c-${String(position)}`}>
          <header className={styles.columnHeader}>
            <h3 className={styles.columnHeading}>{item.heading}</h3>
            {item.caption ? <p className={styles.columnCaption}>{item.caption}</p> : null}
          </header>
          <ul className={styles.columnPoints}>
            {pointsOf(slide, item).map((point) => (
              <li
                className={styles.bullet}
                key={`${String(slide.index)}-${String(point.index)}`}
                data-highlighted={point.index === active ? "true" : undefined}
              >
                <span className={styles.marker} aria-hidden="true" />
                <span className={styles.text}>{point.text}</span>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

/**
 * Numbered steps, for a slide describing a sequence.
 *
 * @param props - The slide, its figure, and the bullet to emphasise.
 * @returns The steps element.
 */
function FlowSteps({
  slide,
  figure,
  active,
}: {
  slide: SlideModel;
  figure: Figure;
  active: number | null;
}): JSX.Element {
  return (
    <ol className={styles.flow}>
      {figure.items.map((item, position) => (
        <li className={styles.step} key={`${String(slide.index)}-s-${String(position)}`}>
          <span className={styles.stepNumber} aria-hidden="true">
            {position + 1}
          </span>
          <div className={styles.stepBody}>
            {/* Heading and caption share a line. A step is a label for the bullets under it, and
                giving the label two lines of its own pushed the last bullet off the slide. */}
            <p className={styles.stepLabel}>
              <span className={styles.stepHeading}>{item.heading}</span>
              {item.caption ? <span className={styles.stepCaption}>{item.caption}</span> : null}
            </p>
            <ul className={styles.columnPoints}>
              {pointsOf(slide, item).map((point) => (
                <li
                  className={styles.bullet}
                  key={`${String(slide.index)}-${String(point.index)}`}
                  data-highlighted={point.index === active ? "true" : undefined}
                >
                  <span className={styles.marker} aria-hidden="true" />
                  <span className={styles.text}>{point.text}</span>
                </li>
              ))}
            </ul>
          </div>
        </li>
      ))}
    </ol>
  );
}

/**
 * Draw a slide's points in whichever arrangement it asked for.
 *
 * A slide with no figure, or one whose kind this build does not know, falls back to the plain
 * list. That is deliberate: a deck authored against a newer schema should render less prettily
 * rather than not at all.
 *
 * @param props - The slide and the bullet to emphasise.
 * @returns The body element.
 */
function SlideBody({ slide, active }: { slide: SlideModel; active: number | null }): JSX.Element {
  const figure = slide.figure;
  if (figure == null) {
    return <BulletList slide={slide} active={active} />;
  }
  switch (figure.kind) {
    case "metrics":
      return <MetricCards slide={slide} figure={figure} active={active} />;
    case "split":
      return <SplitColumns slide={slide} figure={figure} active={active} />;
    case "flow":
      return <FlowSteps slide={slide} figure={figure} active={active} />;
    default:
      return <BulletList slide={slide} active={active} />;
  }
}

/**
 * One slide: a title and its points, arranged as the slide asks, with an optional emphasis.
 *
 * The highlight is owned here rather than in the store because it is a piece of presentation
 * timing, not session state: the server says *which* bullet matters, and this component decides
 * how long that stays true (PRD F1 — four seconds, or until the next highlight arrives). Every
 * highlight gets its own four seconds, including a repeat of a bullet that was emphasised earlier
 * in the life of the slide.
 *
 * A highlight that arrives while the identical one is already on screen is invisible here — the
 * props are unchanged, so React does not re-render — which is the one case the clock does not
 * restart for. Nothing on screen changes either, so the emphasis simply runs out its original four
 * seconds.
 *
 * @param props - The slide and the bullet to emphasise.
 * @returns The slide element.
 */
export function Slide({ slide, highlight }: SlideProps): JSX.Element {
  // One highlight is identified by the slide it lives on and the bullet it points at, but that key
  // repeats: the agent may come back to a bullet it has emphasised before. So the expiry is a flag
  // about *this* highlight, discarded the moment a different one is asked for — remembering which
  // key expired would make any repeat of that key permanently un-emphasisable.
  const key = `${String(slide.index)}:${highlight === null ? "none" : String(highlight)}`;
  const [emphasised, setEmphasised] = useState(key);
  const [expired, setExpired] = useState(false);
  if (emphasised !== key) {
    // React's "adjust state when a prop changes" pattern: setting state during render re-runs this
    // component before anything is committed, so the new highlight is emphasised in the same frame
    // it arrives. An effect would paint one frame of the old, expired state first.
    setEmphasised(key);
    setExpired(false);
  }
  const active = highlight !== null && !expired ? highlight : null;

  useEffect(() => {
    if (highlight === null) {
      return undefined;
    }
    const timer = setTimeout(() => {
      setExpired(true);
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
      <SlideBody slide={slide} active={active} />
    </article>
  );
}
