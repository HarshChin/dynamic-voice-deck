import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PlaybackQueue, type SentenceCompletion } from "./playback";

/** 100 ms of silence at 24 kHz, the size of one real wire frame. */
const FRAME_SAMPLES = 2400;

/** Seconds of audio in one frame, used to predict the schedule. */
const FRAME_SECONDS = FRAME_SAMPLES / 24000;

interface StartedSource {
  readonly when: number;
  readonly duration: number;
  stopped: boolean;
}

/**
 * A stand-in for the Web Audio graph that records what was scheduled and when.
 *
 * Scheduling is the whole behaviour under test, and it is expressed entirely in calls to
 * `source.start(when)`. Recording those is a far stronger assertion than checking that audio
 * "played", and it runs without an audio device.
 */
class FakeAudioContext {
  currentTime = 0;
  state: AudioContextState = "running";
  readonly destination = {} as AudioDestinationNode;
  readonly started: StartedSource[] = [];
  readonly sources: { onended: (() => void) | null; entry: StartedSource }[] = [];
  closed = false;

  createBuffer(_channels: number, length: number, sampleRate: number): AudioBuffer {
    const data = new Float32Array(length);
    return {
      duration: length / sampleRate,
      length,
      sampleRate,
      numberOfChannels: 1,
      getChannelData: () => data,
    } as unknown as AudioBuffer;
  }

  createGain(): GainNode {
    return { connect: () => undefined, disconnect: () => undefined } as unknown as GainNode;
  }

  createAnalyser(): AnalyserNode {
    return {
      fftSize: 256,
      connect: () => undefined,
      disconnect: () => undefined,
    } as unknown as AnalyserNode;
  }

  createBufferSource(): AudioBufferSourceNode {
    // Arrow functions below close over `this`, so the fake needs no alias.
    let entry: StartedSource | null = null;
    const node = {
      buffer: null as AudioBuffer | null,
      onended: null as (() => void) | null,
      connect: () => undefined,
      disconnect: () => undefined,
      start: (when: number) => {
        entry = { when, duration: node.buffer?.duration ?? 0, stopped: false };
        this.started.push(entry);
        this.sources.push({
          get onended() {
            return node.onended;
          },
          entry,
        });
      },
      stop: () => {
        // Identity, not duration: every frame is the same length, so matching on duration would
        // mark the wrong source stopped.
        if (entry !== null) {
          entry.stopped = true;
        }
      },
    };
    return node as unknown as AudioBufferSourceNode;
  }

  async close(): Promise<void> {
    this.closed = true;
    this.state = "closed";
  }

  /** Fire `onended` for every scheduled source, as the real graph would when playback finishes. */
  finishAll(): void {
    for (const source of [...this.sources]) {
      source.onended?.();
    }
    this.sources.length = 0;
  }

  /** Fire `onended` for the first `count` sources still outstanding. */
  finish(count: number): void {
    for (const source of this.sources.splice(0, count)) {
      source.onended?.();
    }
  }
}

function frame(): Int16Array {
  return new Int16Array(FRAME_SAMPLES);
}

let context: FakeAudioContext;
let completions: SentenceCompletion[];
let queue: PlaybackQueue;

beforeEach(() => {
  context = new FakeAudioContext();
  completions = [];
  queue = new PlaybackQueue({
    onSentenceComplete: (completion) => completions.push(completion),
    createContext: () => context as unknown as AudioContext,
    now: () => 0,
  });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("scheduling", () => {
  it("TC-FE-010: schedules consecutive frames back to back, leaving no gap", () => {
    for (let seq = 0; seq < 4; seq += 1) {
      queue.enqueue(1, 0, frame());
    }

    const starts = context.started.map((entry) => entry.when);
    for (let i = 1; i < starts.length; i += 1) {
      const previousEnd = (starts[i - 1] ?? 0) + FRAME_SECONDS;
      expect(starts[i]).toBeCloseTo(previousEnd, 6);
    }
  });

  it("TC-FE-011: a late frame is delayed rather than overlapped", () => {
    queue.enqueue(1, 0, frame());
    const firstEnd = (context.started[0]?.when ?? 0) + FRAME_SECONDS;

    // The clock has run past the end of the first frame: the queue fell behind.
    context.currentTime = firstEnd + 5;
    queue.enqueue(1, 0, frame());

    const second = context.started[1]?.when ?? 0;
    expect(second).toBeGreaterThan(firstEnd);
    expect(second).toBeGreaterThanOrEqual(context.currentTime);
  });

  it("TC-FE-013: reports a sentence once the next one starts and its frames have played", () => {
    queue.enqueue(1, 0, frame());
    queue.enqueue(1, 0, frame());
    // Sentence 0 cannot be complete while its frames are still outstanding.
    expect(completions).toEqual([]);

    queue.enqueue(1, 1, frame());
    context.finish(2);

    expect(completions).toEqual([{ turnId: 1, sentenceId: 0 }]);
    expect(queue.lastCompletedSentenceId).toBe(0);
  });

  it("TC-FE-014: the final sentence is reported once the turn seals it", () => {
    queue.enqueue(1, 0, frame());
    context.finish(1);
    // No later frame will arrive, so nothing has proved sentence 0 finished.
    expect(completions).toEqual([]);

    queue.sealAll();

    expect(completions).toEqual([{ turnId: 1, sentenceId: 0 }]);
  });

  it("TC-FE-120: an empty frame is ignored rather than scheduled", () => {
    queue.enqueue(1, 0, new Int16Array(0));

    expect(context.started).toEqual([]);
    expect(queue.isPlaying).toBe(false);
  });
});

describe("flush", () => {
  it("TC-FE-012: stops every scheduled source and empties the queue", () => {
    for (let seq = 0; seq < 5; seq += 1) {
      queue.enqueue(1, 0, frame());
    }
    expect(queue.isPlaying).toBe(true);

    queue.flush();

    expect(context.started.every((entry) => entry.stopped)).toBe(true);
    expect(queue.isPlaying).toBe(false);
  });

  it("TC-FE-121: a flushed sentence is never reported as heard", () => {
    queue.enqueue(1, 0, frame());
    queue.flush();
    // The real graph still fires `onended` for a stopped source.
    context.finishAll();
    queue.sealAll();

    expect(completions).toEqual([]);
  });

  it("TC-FE-122: audio enqueued after a flush schedules from the current time", () => {
    queue.enqueue(1, 0, frame());
    context.currentTime = 10;
    queue.flush();

    queue.enqueue(2, 0, frame());

    expect(context.started[1]?.when).toBeGreaterThanOrEqual(10);
  });
});

describe("teardown", () => {
  it("TC-FE-123: close silences playback and releases the device", async () => {
    queue.enqueue(1, 0, frame());

    await queue.close();

    expect(context.closed).toBe(true);
    expect(queue.isPlaying).toBe(false);
  });

  it("TC-FE-124: closing twice is safe", async () => {
    queue.enqueue(1, 0, frame());
    await queue.close();
    await queue.close();

    expect(context.closed).toBe(true);
  });
});
