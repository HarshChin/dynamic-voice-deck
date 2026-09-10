import type { JSX } from "react";

import type { MetricsField, MetricsMedians, MetricsSample } from "../store";

import styles from "./LatencyHUD.module.css";

/**
 * p50 targets from the latency budget (TRD §8.1, TR-160).
 *
 * They are the thresholds the colour bands are measured against, not aspirations: green means the
 * stage is inside its p50 budget, amber means it has slipped past 1.5×, red means the budget is
 * gone.
 */
const TARGETS_MS: Readonly<Record<MetricsField, number>> = {
  sttMs: 300,
  llmTtftMs: 250,
  llmTotalMs: 1_200,
  ttsTtfbMs: 250,
  firstAudioMs: 1_450,
  interruptStopMs: 150,
};

/** Multiplier past the target where a stage stops being merely slow and becomes a problem. */
const AMBER_FACTOR = 1.5;

/** The rows, in pipeline order, with the words the PRD's HUD uses (PRD F12). */
const ROWS: readonly (readonly [MetricsField, string])[] = [
  ["sttMs", "stt"],
  ["llmTtftMs", "llm ttft"],
  ["llmTotalMs", "llm total"],
  ["ttsTtfbMs", "tts ttfb"],
  ["firstAudioMs", "first audio"],
  ["interruptStopMs", "interrupt stop"],
];

/** Inputs to the latency panel. */
export interface LatencyHUDProps {
  /** The most recent turn's sample, or `null` before any turn has reported. */
  readonly last: MetricsSample | null;
  /** Rolling medians across the session. */
  readonly medians: MetricsMedians;
}

/**
 * Classify a measurement against its budget.
 *
 * @param field - Which stage the measurement belongs to.
 * @param value - The measurement in milliseconds, or `null` when the stage did not report.
 * @returns A band name used as a `data-band` hook, or `undefined` when there is nothing to grade.
 */
function band(field: MetricsField, value: number | null): "ok" | "warn" | "bad" | undefined {
  if (value === null) {
    return undefined;
  }
  const target = TARGETS_MS[field];
  if (value <= target) {
    return "ok";
  }
  return value <= target * AMBER_FACTOR ? "warn" : "bad";
}

/**
 * Render a millisecond value.
 *
 * @param value - The measurement, or `null`.
 * @returns The rounded value with its unit, or an em dash.
 */
function format(value: number | null): string {
  return value === null ? "—" : `${String(Math.round(value))} ms`;
}

/**
 * The latency HUD (PRD F12, TR-163).
 *
 * A corner panel rather than a page section: latency is something you glance at while presenting,
 * and the moment it needs its own screen it has stopped being a live instrument. Each row shows
 * the last turn next to the session median, because a single slow turn and a consistently slow
 * stage are different problems.
 *
 * @param props - The last sample and the session medians.
 * @returns The panel element.
 */
export function LatencyHUD({ last, medians }: LatencyHUDProps): JSX.Element {
  return (
    <aside className={styles.hud} aria-label="Latency">
      <header className={styles.header}>
        <span className={styles.title}>latency</span>
        <span className={styles.turn}>{last === null ? "no turns" : `turn ${last.turnId}`}</span>
      </header>
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col" className={styles.stage}>
              stage
            </th>
            <th scope="col" className={styles.value}>
              last
            </th>
            <th scope="col" className={styles.value}>
              median
            </th>
          </tr>
        </thead>
        <tbody>
          {ROWS.map(([field, label]) => {
            const lastValue = last === null ? null : last[field];
            return (
              <tr key={field}>
                <th scope="row" className={styles.stage}>
                  {label}
                </th>
                <td className={styles.value} data-band={band(field, lastValue)}>
                  {format(lastValue)}
                </td>
                <td className={styles.value} data-band={band(field, medians[field])}>
                  {format(medians[field])}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </aside>
  );
}
