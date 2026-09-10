/**
 * Microphone capture and speech detection, entirely in the browser.
 *
 * Detection runs on-device, which is the decision the whole interruption design rests on (TR-110):
 * the moment you start talking is known locally, so playback can be silenced without waiting for a
 * network round trip. Only a finished utterance is uploaded, so transcription cannot begin until you
 * stop speaking -- the trade the deck states openly on slide 3.
 *
 * ## Why this is energy-based rather than Silero
 *
 * The original design used Silero VAD, a small neural network, through `@ricky0123/vad-web`. It
 * could not be made to load. The ONNX runtime it depends on resolves its own WebAssembly loader by
 * dynamic import, and Vite's dependency pre-bundler rewrites that import into a cache directory the
 * loader was never copied to. Excluding the runtime from pre-bundling fixes that and breaks the
 * detector instead, because the detector is CommonJS and needs pre-bundling to be importable by
 * name. Serving the loader from `public/` fails a third way: Vite refuses to let source import a
 * module from there. Four attempts, each failing differently, all verified in a real browser.
 *
 * So detection is an energy threshold with hysteresis, written here. For the question this product
 * actually asks -- has the person started speaking, and have they stopped -- it is adequate,
 * especially with the browser's echo cancellation suppressing the agent's own voice. It also has no
 * runtime dependency, no WebAssembly, no CDN and no model download, which for a demo that must work
 * on a strange network is worth something on its own.
 *
 * The cost is honest and belongs in the trade-offs: a neural detector is better in a noisy room and
 * far better at distinguishing speech from other sounds. The thresholds below are the same ones the
 * design specified, so swapping the detector back in later changes this file and nothing else.
 */

import { AUDIO_RATES, VAD } from "../config";

/** Where the capture worklet is served from. Loaded with `addModule`, never imported. */
const WORKLET_URL = "/worklets/capture.js";

/** Name the worklet registers itself under. */
const PROCESSOR_NAME = "capture-processor";

/** Samples per frame from the worklet: 32 ms at 16 kHz. */
const FRAME_SAMPLES = 512;

/** Milliseconds of audio in one frame. */
const FRAME_MS = (FRAME_SAMPLES / AUDIO_RATES.input) * 1000;

/**
 * Loudness above which a frame counts as speech.
 *
 * Root-mean-square amplitude, so this is a level rather than a probability, and it is not the
 * `positiveSpeechThreshold` a neural detector would use. Chosen so ordinary speech at a normal
 * distance clears it while room tone does not.
 */
const SPEECH_RMS = 0.02;

/** Loudness below which a frame counts as silence. The gap is the hysteresis. */
const SILENCE_RMS = 0.012;

/** Consecutive speech frames required to declare onset: about 96 ms, so a click cannot trigger it. */
const ONSET_FRAMES = 3;

/** What the microphone reports to the session. */
export interface MicrophoneHandlers {
  /** Speech began. The session decides whether this is a barge-in. */
  readonly onSpeechStart: () => void;
  /** A finished utterance, as 16 kHz mono PCM16 ready for the wire. */
  readonly onUtterance: (pcm16: Int16Array, durationMs: number) => void;
  /** Speech began but was too short to be a turn, so nothing will be uploaded. */
  readonly onMisfire: () => void;
  /** The microphone could not be opened, with a message fit to show a person. */
  readonly onError: (message: string) => void;
}

/**
 * Convert float samples to the wire format.
 *
 * @param samples - Float samples in `[-1, 1]` at 16 kHz.
 * @returns Signed 16-bit samples.
 */
export function floatToPcm16(samples: Float32Array): Int16Array {
  const out = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[i] ?? 0));
    // 0x7fff, not 0x8000: scaling by the negative bound would clip the loudest positive sample.
    out[i] = Math.round(clamped * 0x7fff);
  }
  return out;
}

/** Frames kept before onset so the first syllable is not clipped. */
const PRE_ROLL_FRAMES = Math.ceil(VAD.preSpeechPadMs / FRAME_MS);

/** Silent frames that end a turn. */
const REDEMPTION_FRAMES = Math.ceil(VAD.redemptionMs / FRAME_MS);

/** Shortest run of speech worth uploading. */
const MIN_SPEECH_FRAMES = Math.ceil(VAD.minSpeechMs / FRAME_MS);

/**
 * Owns the microphone and decides when a turn starts and ends.
 */
export class Microphone {
  readonly #handlers: MicrophoneHandlers;
  #context: AudioContext | null = null;
  #stream: MediaStream | null = null;
  #node: AudioWorkletNode | null = null;
  #starting: Promise<void> | null = null;
  #muted = false;

  // Detection state.
  #speaking = false;
  #loudRun = 0;
  #silentRun = 0;
  #preRoll: Float32Array[] = [];
  #utterance: Float32Array[] = [];

  /**
   * @param handlers - Callbacks the session supplies.
   */
  constructor(handlers: MicrophoneHandlers) {
    this.#handlers = handlers;
  }

  /** Whether the microphone is open and not muted. */
  get isListening(): boolean {
    return this.#node !== null && !this.#muted;
  }

  /**
   * Ask for the microphone and start listening.
   *
   * Safe to call twice: the second call awaits the first rather than opening a second device, which
   * matters because React's StrictMode runs effects twice in development.
   *
   * @returns Resolves once capture is running, or after `onError` has been called.
   */
  async start(): Promise<void> {
    if (this.#node !== null) {
      return;
    }
    this.#starting ??= this.#startOnce();
    await this.#starting;
  }

  /** Stop detecting without releasing the device, so it can resume instantly. */
  mute(): void {
    this.#muted = true;
    this.#resetDetection();
  }

  /** Resume detecting after {@link mute}. */
  unmute(): void {
    this.#muted = false;
  }

  /** Release the microphone. The browser's recording indicator turns off here. */
  async stop(): Promise<void> {
    const context = this.#context;
    const stream = this.#stream;
    const node = this.#node;
    this.#context = null;
    this.#stream = null;
    this.#node = null;
    this.#starting = null;
    this.#muted = false;
    this.#resetDetection();

    node?.port.close();
    node?.disconnect();
    for (const track of stream?.getTracks() ?? []) {
      track.stop();
    }
    if (context !== null && context.state !== "closed") {
      await context.close();
    }
  }

  async #startOnce(): Promise<void> {
    try {
      // Echo cancellation is the constraint that matters: without it the agent's own voice, played
      // through speakers, is heard as the user interrupting, and it talks itself down mid-sentence.
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      const context = new AudioContext();
      await context.audioWorklet.addModule(WORKLET_URL);
      const source = context.createMediaStreamSource(stream);
      const node = new AudioWorkletNode(context, PROCESSOR_NAME);
      node.port.onmessage = (event: MessageEvent<{ frame: Float32Array; rms: number }>) => {
        this.#onFrame(event.data.frame, event.data.rms);
      };
      source.connect(node);
      // Not connected to the destination: routing the microphone to the speakers would echo.

      this.#stream = stream;
      this.#context = context;
      this.#node = node;
    } catch (error) {
      this.#starting = null;
      this.#handlers.onError(describeMicrophoneError(error));
    }
  }

  #onFrame(frame: Float32Array, rms: number): void {
    if (this.#muted) {
      return;
    }

    if (!this.#speaking) {
      // Keep a rolling window of what came just before onset, so the first syllable survives.
      this.#preRoll.push(frame);
      if (this.#preRoll.length > PRE_ROLL_FRAMES) {
        this.#preRoll.shift();
      }
      this.#loudRun = rms >= SPEECH_RMS ? this.#loudRun + 1 : 0;
      if (this.#loudRun >= ONSET_FRAMES) {
        this.#speaking = true;
        this.#silentRun = 0;
        this.#utterance = [...this.#preRoll];
        this.#preRoll = [];
        this.#handlers.onSpeechStart();
      }
      return;
    }

    this.#utterance.push(frame);
    this.#silentRun = rms <= SILENCE_RMS ? this.#silentRun + 1 : 0;
    if (this.#silentRun >= REDEMPTION_FRAMES) {
      this.#finishUtterance();
    }
  }

  #finishUtterance(): void {
    const frames = this.#utterance;
    const spokenFrames = frames.length - REDEMPTION_FRAMES - this.#preRoll.length;
    this.#resetDetection();

    if (spokenFrames < MIN_SPEECH_FRAMES) {
      // A cough, a chair, a keystroke: too short to be a turn, so nothing is uploaded.
      this.#handlers.onMisfire();
      return;
    }

    const total = frames.reduce((sum, frame) => sum + frame.length, 0);
    const merged = new Float32Array(total);
    let offset = 0;
    for (const frame of frames) {
      merged.set(frame, offset);
      offset += frame.length;
    }
    const durationMs = Math.round((total / AUDIO_RATES.input) * 1000);
    this.#handlers.onUtterance(floatToPcm16(merged), durationMs);
  }

  #resetDetection(): void {
    this.#speaking = false;
    this.#loudRun = 0;
    this.#silentRun = 0;
    this.#preRoll = [];
    this.#utterance = [];
  }
}

/**
 * Turn a `getUserMedia` failure into something worth showing a person.
 *
 * @param error - Whatever was thrown.
 * @returns A sentence naming the cause and, where possible, the fix.
 */
export function describeMicrophoneError(error: unknown): string {
  const name = error instanceof DOMException ? error.name : "";
  if (name === "NotAllowedError" || name === "SecurityError") {
    return "microphone permission was refused; allow it in the address bar, or type your question instead";
  }
  if (name === "NotFoundError" || name === "OverconstrainedError") {
    return "no microphone was found; plug one in, or type your question instead";
  }
  if (name === "NotReadableError") {
    return "the microphone is in use by another application; close it and start the session again";
  }
  const detail = error instanceof Error ? error.message : String(error);
  return `the microphone could not be started: ${detail}`;
}
