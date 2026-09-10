/**
 * Gapless playback of the agent's voice, and the flush that makes barge-in feel instant.
 *
 * Audio arrives as a stream of 100 ms PCM16 frames tagged with the sentence they belong to. Each is
 * scheduled on the Web Audio clock rather than played on arrival, because `AudioContext.currentTime`
 * advances smoothly while `setTimeout` does not: scheduling each frame to begin exactly where the
 * previous one ended is what removes the clicks between them (TR-121).
 *
 * Two responsibilities beyond playing sound:
 *
 * - **Flush (TR-122).** The first tier of barge-in happens here, entirely in the browser. Stopping
 *   every scheduled source takes microseconds and needs no round trip, which is why the interrupt
 *   feels immediate even though the server is still being told.
 * - **Completion (TR-123).** Only this class knows when a sentence actually reached the speakers,
 *   so it reports each one. The server uses that to know how much of its answer was really heard,
 *   and truncates its memory to exactly that on an interrupt.
 */

import { AUDIO_RATES } from "../config";

/** Seconds of lead time before the first frame of a run is scheduled. */
const SCHEDULING_LEAD_S = 0.02;

/** Samples the analyser reads to estimate output level. */
const ANALYSER_FFT_SIZE = 256;

/** What the queue reports about one sentence reaching the speakers. */
export interface SentenceCompletion {
  readonly turnId: number;
  readonly sentenceId: number;
}

/** Construction options, all injectable so tests need no real audio device. */
export interface PlaybackQueueOptions {
  /** Called once per sentence, after its last frame has finished playing. */
  readonly onSentenceComplete: (completion: SentenceCompletion) => void;
  /** Called with output level in `[0, 1]` while audio plays, for the orb. */
  readonly onLevel?: (level: number) => void;
  /** Builds the output context. Overridden in tests. */
  readonly createContext?: () => AudioContext;
  /** Clock used for the interrupt measurement. Overridden in tests. */
  readonly now?: () => number;
}

interface ScheduledSentence {
  scheduled: number;
  ended: number;
  sealed: boolean;
  turnId: number;
}

/**
 * Schedules incoming audio frames back to back and can silence them at once.
 */
export class PlaybackQueue {
  readonly #options: PlaybackQueueOptions;
  readonly #now: () => number;
  #context: AudioContext | null = null;
  #gain: GainNode | null = null;
  #analyser: AnalyserNode | null = null;
  #levelTimer: number | null = null;
  #sources = new Set<AudioBufferSourceNode>();
  #sentences = new Map<number, ScheduledSentence>();
  #nextStartTime = 0;
  #lastCompleted: number | null = null;
  #playing = false;

  /**
   * @param options - Callbacks and injectable dependencies.
   */
  constructor(options: PlaybackQueueOptions) {
    this.#options = options;
    this.#now = options.now ?? (() => performance.now());
  }

  /** Whether any audio is currently scheduled or sounding. */
  get isPlaying(): boolean {
    return this.#playing;
  }

  /** Id of the last sentence heard in full, or `null` if none has been. */
  get lastCompletedSentenceId(): number | null {
    return this.#lastCompleted;
  }

  /**
   * Schedule one frame of a sentence.
   *
   * @param turnId - Turn the audio belongs to.
   * @param sentenceId - Sentence within that turn.
   * @param samples - Signed 16-bit mono samples, as decoded from the wire frame.
   */
  enqueue(turnId: number, sentenceId: number, samples: Int16Array): void {
    if (samples.length === 0) {
      return;
    }
    const context = this.#ensureContext();

    // A frame of a later sentence proves the previous one is finished, which is what lets a
    // sentence be reported complete without the server having to say so.
    for (const [id, entry] of this.#sentences) {
      if (id < sentenceId && !entry.sealed) {
        entry.sealed = true;
        this.#reportIfComplete(id);
      }
    }

    const entry = this.#sentences.get(sentenceId) ?? {
      scheduled: 0,
      ended: 0,
      sealed: false,
      turnId,
    };
    entry.scheduled += 1;
    this.#sentences.set(sentenceId, entry);

    const buffer = context.createBuffer(1, samples.length, AUDIO_RATES.output);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i += 1) {
      // 32768 rather than 32767: the negative end of the int16 range is one larger, and dividing by
      // the positive maximum would clip the loudest negative sample.
      channel[i] = (samples[i] ?? 0) / 32768;
    }

    const source = context.createBufferSource();
    source.buffer = buffer;
    // `#ensureContext` has just run, so the gain node exists.
    source.connect(this.#gain!);

    // A late frame must never overlap the one before it. Starting from `currentTime` when the queue
    // has fallen behind inserts a short silence instead, which is far less noticeable than two
    // frames playing over each other.
    const startAt = Math.max(context.currentTime + SCHEDULING_LEAD_S, this.#nextStartTime);
    source.onended = () => {
      this.#sources.delete(source);
      const tracked = this.#sentences.get(sentenceId);
      if (tracked) {
        tracked.ended += 1;
        this.#reportIfComplete(sentenceId);
      }
      if (this.#sources.size === 0) {
        this.#playing = false;
        this.#stopLevelPolling();
      }
    };
    source.start(startAt);
    this.#sources.add(source);
    this.#nextStartTime = startAt + buffer.duration;
    this.#playing = true;
    this.#startLevelPolling();
  }

  /**
   * Mark a sentence finished, so it can be reported once its frames have played.
   *
   * Called when the turn ends, for the final sentence, which no later frame can seal.
   *
   * @param sentenceId - The sentence that has no more frames coming.
   */
  seal(sentenceId: number): void {
    const entry = this.#sentences.get(sentenceId);
    if (entry === undefined || entry.sealed) {
      return;
    }
    entry.sealed = true;
    this.#reportIfComplete(sentenceId);
  }

  /** Mark every scheduled sentence finished. Used when a turn ends. */
  sealAll(): void {
    for (const id of [...this.#sentences.keys()]) {
      this.seal(id);
    }
  }

  /**
   * Stop everything immediately. This is tier one of barge-in.
   *
   * @returns Milliseconds this took, measured on the caller's clock, for the latency panel.
   */
  flush(): number {
    const started = this.#now();
    for (const source of this.#sources) {
      source.onended = null;
      try {
        source.stop();
      } catch {
        // Already stopped or never started; either way there is nothing to silence.
      }
      source.disconnect();
    }
    this.#sources.clear();
    this.#sentences.clear();
    this.#nextStartTime = this.#context?.currentTime ?? 0;
    this.#playing = false;
    this.#stopLevelPolling();
    this.#options.onLevel?.(0);
    return this.#now() - started;
  }

  /** Release the audio device. Safe to call more than once. */
  async close(): Promise<void> {
    this.flush();
    const context = this.#context;
    this.#context = null;
    this.#gain = null;
    this.#analyser = null;
    if (context !== null && context.state !== "closed") {
      await context.close();
    }
  }

  #ensureContext(): AudioContext {
    if (this.#context !== null) {
      return this.#context;
    }
    const context =
      this.#options.createContext?.() ?? new AudioContext({ sampleRate: AUDIO_RATES.output });
    const gain = context.createGain();
    const analyser = context.createAnalyser();
    analyser.fftSize = ANALYSER_FFT_SIZE;
    gain.connect(analyser);
    analyser.connect(context.destination);
    this.#context = context;
    this.#gain = gain;
    this.#analyser = analyser;
    this.#nextStartTime = context.currentTime;
    return context;
  }

  #reportIfComplete(sentenceId: number): void {
    const entry = this.#sentences.get(sentenceId);
    if (entry === undefined || !entry.sealed || entry.ended < entry.scheduled) {
      return;
    }
    this.#sentences.delete(sentenceId);
    this.#lastCompleted = sentenceId;
    this.#options.onSentenceComplete({ turnId: entry.turnId, sentenceId });
  }

  #startLevelPolling(): void {
    if (this.#levelTimer !== null || this.#options.onLevel === undefined) {
      return;
    }
    const analyser = this.#analyser;
    if (analyser === null || typeof analyser.getByteTimeDomainData !== "function") {
      return;
    }
    const samples = new Uint8Array(analyser.fftSize);
    this.#levelTimer = window.setInterval(() => {
      analyser.getByteTimeDomainData(samples);
      let sum = 0;
      for (const sample of samples) {
        const centred = (sample - 128) / 128;
        sum += centred * centred;
      }
      this.#options.onLevel?.(Math.min(1, Math.sqrt(sum / samples.length) * 2));
    }, 50);
  }

  #stopLevelPolling(): void {
    if (this.#levelTimer !== null) {
      window.clearInterval(this.#levelTimer);
      this.#levelTimer = null;
    }
    this.#options.onLevel?.(0);
  }
}
