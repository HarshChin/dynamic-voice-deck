import { useEffect, useRef, useState, type JSX } from "react";

import type {
  AgentLogEvent,
  ErrorLogEvent,
  EventsExport,
  InterruptLogEvent,
  LogEvent,
  MetricsLogEvent,
  NoticeLogEvent,
  SessionLogEvent,
  SlideLogEvent,
  StateLogEvent,
  ToolLogEvent,
  UserLogEvent,
} from "../store";

import styles from "./EventLog.module.css";

/** How long the "Copied" confirmation stays on the button. */
const COPY_FEEDBACK_MS = 2_000;

/** Entry kinds that are noise during a demo and detail during debugging (PRD F10). */
const DEBUG_ONLY_KINDS: ReadonlySet<LogEvent["kind"]> = new Set(["metrics", "notice"]);

/** Inputs to the event log panel. */
export interface EventLogProps {
  /** The log, oldest first. */
  readonly events: readonly LogEvent[];
  /** Whether raw metrics and client-side notices are shown. */
  readonly debug: boolean;
  /** Called when the user flips the debug toggle. */
  readonly onDebugChange: (debug: boolean) => void;
  /** Builds the replayable JSON envelope for "Copy log" (TRD §7.2). */
  readonly exportEvents: () => EventsExport;
}

/**
 * Read a numeric tool argument.
 *
 * @param value - The raw argument, which came off the wire unvalidated.
 * @returns The number, or `null` when the argument was absent or another type.
 */
function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * Read a string tool argument.
 *
 * @param value - The raw argument.
 * @returns The trimmed string, or `null` when absent, empty, or another type.
 */
function asText(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  return trimmed.length === 0 ? null : trimmed;
}

/**
 * Describe a tool call in the words the PRD's mock-up uses.
 *
 * @param event - The tool entry.
 * @returns A short subject line, e.g. "Slide 4".
 */
function describeTool(event: ToolLogEvent): string {
  const index = asNumber(event.args.index);
  if (index === null) {
    return event.name;
  }
  if (event.name === "go_to_slide") {
    return `Slide ${String(index)}`;
  }
  if (event.name === "highlight_bullet") {
    return `Bullet ${String(index + 1)}`;
  }
  return `${event.name} ${String(index)}`;
}

/**
 * Format one metrics sample as a compact mono line.
 *
 * @param event - The metrics entry.
 * @returns One `key=value` string per measured stage.
 */
function describeMetrics(event: MetricsLogEvent): string {
  const { sample } = event;
  const parts: string[] = [];
  const stages: readonly (readonly [string, number | null])[] = [
    ["stt", sample.sttMs],
    ["ttft", sample.llmTtftMs],
    ["llm", sample.llmTotalMs],
    ["tts", sample.ttsTtfbMs],
    ["audio", sample.firstAudioMs],
    ["stop", sample.interruptStopMs],
  ];
  for (const [name, value] of stages) {
    if (value !== null) {
      parts.push(`${name} ${String(Math.round(value))}ms`);
    }
  }
  parts.push(`${String(sample.sentences)} sentences`);
  return parts.join(" · ");
}

/**
 * Render one log entry.
 *
 * A switch rather than a lookup table so the `never` in the default arm makes a new entry kind a
 * compile error here as well as in the store.
 *
 * @param event - The entry to render.
 * @returns The element for that entry.
 */
function LogEntry({ event }: { readonly event: LogEvent }): JSX.Element {
  switch (event.kind) {
    case "session":
      return <SessionEntry event={event} />;
    case "state":
      return <StateEntry event={event} />;
    case "user":
      return <UserEntry event={event} />;
    case "agent":
      return <AgentEntry event={event} />;
    case "tool":
      return <ToolEntry event={event} />;
    case "slide":
      return <SlideEntry event={event} />;
    case "interrupt":
      return <InterruptEntry event={event} />;
    case "metrics":
      return <MetricsEntry event={event} />;
    case "error":
      return <ErrorEntry event={event} />;
    case "notice":
      return <NoticeEntry event={event} />;
  }
}

/**
 * The session banner: which deck opened, and on which providers.
 *
 * @param props - The session entry.
 * @returns The element.
 */
function SessionEntry({ event }: { readonly event: SessionLogEvent }): JSX.Element {
  const { stt, llm, tts } = event.providers;
  return (
    <li className={styles.banner} data-kind="session">
      <span className={styles.bannerTitle}>{event.deckTitle}</span>
      <span className={styles.bannerMeta}>
        {event.sessionId} · {stt} / {llm} / {tts}
      </span>
    </li>
  );
}

/**
 * A turn-taking transition, drawn as a thin rule with its dwell time (PRD F10).
 *
 * @param props - The state entry.
 * @returns The element.
 */
function StateEntry({ event }: { readonly event: StateLogEvent }): JSX.Element {
  return (
    <li className={styles.divider} data-kind="state" data-to={event.to}>
      <span className={styles.dividerText}>{`${event.from} → ${event.to}`}</span>
      <span className={styles.dividerTime}>{Math.round(event.elapsedMs)} ms</span>
    </li>
  );
}

/**
 * What the user said or typed, right-aligned.
 *
 * @param props - The user entry.
 * @returns The element.
 */
function UserEntry({ event }: { readonly event: UserLogEvent }): JSX.Element {
  return (
    <li className={styles.row} data-kind="user" data-align="end">
      <p className={styles.bubbleUser}>{event.text}</p>
    </li>
  );
}

/**
 * One agent sentence. Sentences the user never heard are struck through (TR-132).
 *
 * @param props - The agent entry.
 * @returns The element.
 */
function AgentEntry({ event }: { readonly event: AgentLogEvent }): JSX.Element {
  return (
    <li className={styles.row} data-kind="agent">
      <p className={styles.bubbleAgent} data-cancelled={event.cancelled ? "true" : undefined}>
        {event.text}
      </p>
    </li>
  );
}

/**
 * A navigation decision. `llm` and `fallback` are two different chips, because telling them apart
 * is the whole point of showing tool calls during a demo (TR-062, PRD F10).
 *
 * @param props - The tool entry.
 * @returns The element.
 */
function ToolEntry({ event }: { readonly event: ToolLogEvent }): JSX.Element {
  const reason = asText(event.args.reason) ?? asText(event.args.keyword);
  return (
    <li className={styles.row} data-kind="tool">
      <p className={styles.chipTool} data-source={event.source}>
        <span className={styles.chipIcon} aria-hidden="true">
          →
        </span>
        <span className={styles.chipBody}>
          <span className={styles.chipTitle}>{describeTool(event)}</span>
          {reason !== null && <span className={styles.chipDetail}>{reason}</span>}
        </span>
        <span className={styles.badge}>{event.source === "llm" ? "model" : "keyword"}</span>
      </p>
    </li>
  );
}

/**
 * The deck actually moved.
 *
 * @param props - The slide entry.
 * @returns The element.
 */
function SlideEntry({ event }: { readonly event: SlideLogEvent }): JSX.Element {
  return (
    <li className={styles.row} data-kind="slide">
      <p className={styles.chipSlide}>
        <span className={styles.chipIcon} aria-hidden="true">
          ▸
        </span>
        <span className={styles.chipBody}>
          <span className={styles.chipTitle}>Slide {event.index}</span>
          <span className={styles.chipDetail}>{event.reason}</span>
        </span>
      </p>
    </li>
  );
}

/**
 * A barge-in, with how much of the answer the user actually heard.
 *
 * @param props - The interrupt entry.
 * @returns The element.
 */
function InterruptEntry({ event }: { readonly event: InterruptLogEvent }): JSX.Element {
  const heard = event.heardSentences;
  return (
    <li className={styles.row} data-kind="interrupt">
      <p className={styles.chipInterrupt}>
        <span className={styles.chipIcon} aria-hidden="true">
          ⏹
        </span>
        <span className={styles.chipBody}>
          <span className={styles.chipTitle}>
            Interrupted after {heard} {heard === 1 ? "sentence" : "sentences"}
          </span>
        </span>
      </p>
    </li>
  );
}

/**
 * A failure the user should see.
 *
 * @param props - The error entry.
 * @returns The element.
 */
function ErrorEntry({ event }: { readonly event: ErrorLogEvent }): JSX.Element {
  return (
    <li className={styles.row} data-kind="error">
      <p className={styles.chipError} data-recoverable={event.recoverable ? "true" : "false"}>
        <span className={styles.chipBody}>
          <span className={styles.chipTitle}>{event.code}</span>
          <span className={styles.chipDetail}>{event.text}</span>
        </span>
      </p>
    </li>
  );
}

/**
 * One turn's latencies, shown only with debug on.
 *
 * @param props - The metrics entry.
 * @returns The element.
 */
function MetricsEntry({ event }: { readonly event: MetricsLogEvent }): JSX.Element {
  return (
    <li className={styles.rawRow} data-kind="metrics">
      <span className={styles.rawLabel}>turn {event.turnId}</span>
      <span className={styles.rawText}>{describeMetrics(event)}</span>
    </li>
  );
}

/**
 * A client-side remark, shown only with debug on.
 *
 * @param props - The notice entry.
 * @returns The element.
 */
function NoticeEntry({ event }: { readonly event: NoticeLogEvent }): JSX.Element {
  return (
    <li className={styles.rawRow} data-kind="notice">
      <span className={styles.rawLabel}>client</span>
      <span className={styles.rawText}>{event.text}</span>
    </li>
  );
}

/**
 * The right-hand panel that makes a session legible (PRD F10).
 *
 * It is a pure view over the store's log: every entry the store recorded appears here in arrival
 * order, and nothing is derived a second time. "Copy log" hands out the same replayable envelope
 * the export endpoint describes (TRD §7.2), which is what turns a surprising session into a bug
 * report someone else can read.
 *
 * @param props - The log, the debug toggle, and the export function.
 * @returns The panel element.
 */
export function EventLog({
  events,
  debug,
  onDebugChange,
  exportEvents,
}: EventLogProps): JSX.Element {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const scrollRef = useRef<HTMLOListElement>(null);
  const visible = events.filter((event) => debug || !DEBUG_ONLY_KINDS.has(event.kind));

  useEffect(() => {
    const element = scrollRef.current;
    if (element !== null) {
      // Assigning `scrollTop` rather than calling `scrollTo` keeps this working in jsdom, where
      // the smooth-scroll API is not implemented.
      element.scrollTop = element.scrollHeight;
    }
  }, [visible.length]);

  useEffect(() => {
    if (copyState === "idle") {
      return undefined;
    }
    const timer = setTimeout(() => {
      setCopyState("idle");
    }, COPY_FEEDBACK_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [copyState]);

  function handleCopy(): void {
    async function copy(): Promise<void> {
      try {
        // `navigator.clipboard` is absent outside a secure context and in jsdom; the try/catch is
        // the guard, because the DOM types insist it is always there.
        await navigator.clipboard.writeText(JSON.stringify(exportEvents(), null, 2));
        setCopyState("copied");
      } catch {
        setCopyState("failed");
      }
    }
    void copy();
  }

  return (
    <section className={styles.panel} aria-label="Event log">
      <header className={styles.header}>
        <h2 className={styles.title}>Event log</h2>
        <label className={styles.toggle}>
          <input
            type="checkbox"
            checked={debug}
            onChange={(changeEvent) => {
              onDebugChange(changeEvent.target.checked);
            }}
          />
          debug
        </label>
        <button className={styles.copy} type="button" onClick={handleCopy}>
          {copyState === "idle" ? "Copy log" : copyState === "copied" ? "Copied" : "Copy failed"}
        </button>
      </header>
      {visible.length === 0 ? (
        <p className={styles.empty}>
          Nothing yet. Start a session and ask a question — every message, tool call, and state
          change shows up here.
        </p>
      ) : (
        <ol className={styles.entries} ref={scrollRef}>
          {visible.map((event) => (
            <LogEntry event={event} key={event.id} />
          ))}
        </ol>
      )}
    </section>
  );
}
