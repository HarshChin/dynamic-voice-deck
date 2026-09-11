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
 * Loudness above which a frame counts as speech, and the lower level at which it counts as silence.
 *
 * Root-mean-square amplitudes, so these are levels rather than probabilities. The gap between them
 * is the hysteresis that keeps a dip mid-word from ending an utterance. They live in `config.ts`
 * with the timings because TR-111 asks for every detection parameter in one place.
 */
const SPEECH_RMS = VAD.speechRms;

const SILENCE_RMS = VAD.silenceRms;

/**
 * Consecutive speech frames required to declare onset *while the agent is speaking*: about 96 ms
 * (TR-112).
 *
 * Echo is the reason. Cancellation is good but not perfect, and a leaked fragment of the agent's
 * own voice is short; requiring three frames in a row means such a fragment cannot cut it off
 * mid-sentence. With nothing playing there is no echo to resist, so waiting would be pure added
 * latency on the common case, and a false onset there is cheap: it emits `speech.start` and
 * nothing else, and the minimum-speech gate still refuses to upload the noise that caused it.
 */
const ONSET_FRAMES_WHILE_PLAYING = VAD.onsetFramesWhilePlaying;

const ONSET_FRAMES_WHILE_IDLE = VAD.onsetFramesWhileIdle;

/** What the microphone reports to the session. */
export interface MicrophoneHandlers {
  /**
   * Whether the agent is audible right now.
   *
   * Read per frame rather than latched, because it decides how much evidence onset needs
   * (TR-112) and it changes while the microphone is open. Optional: without it the cautious
   * three-frame rule applies always.
   */
  readonly isPlaying?: () => boolean;
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

/** Longest a single capture may run before it is abandoned as stuck (TR-116). */
const MAX_UTTERANCE_FRAMES = Math.ceil(VAD.maxUtteranceMs / FRAME_MS);

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
  #pushToTalk = false;
  #speaking = false;
  // Whether the agent was audible on the previous frame, so the moment it starts can be noticed.
  #wasPlaying = false;
  #loudRun = 0;
  #silentRun = 0;
  #preRoll: Float32Array[] = [];
  #utterance: Float32Array[] = [];
  // How many of `#utterance`'s leading frames came from the pre-roll, so the minimum-speech gate
  // can measure speech rather than padding. Not derivable afterwards: the pre-roll is short of
  // `PRE_ROLL_FRAMES` for an onset in the first third of a second after the microphone opens.
  #preRollFrames = 0;

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

  /** Whether a turn is being captured right now, whether a key or the detector started it. */
  get isCapturing(): boolean {
    return this.#speaking;
  }

  /**
   * Choose what decides when a turn starts: the detector, or a held key (TR-115).
   *
   * The reason to offer the choice at all is that energy detection has a failure mode a person
   * cannot work around -- a room loud enough that every frame reads as speech -- and in that room
   * a key still works. Switching abandons any turn in progress rather than finishing it under
   * rules it did not start under.
   *
   * @param enabled - `true` to take orders from {@link beginPush} and {@link endPush} instead.
   */
  setPushToTalk(enabled: boolean): void {
    if (this.#pushToTalk === enabled) {
      return;
    }
    this.#pushToTalk = enabled;
    this.#resetDetection();
  }

  /**
   * Start capturing because the key went down.
   *
   * The padding kept for the detector is used here too: a person starts speaking as they press,
   * not after, so the first syllable is usually already in the buffer.
   */
  beginPush(): void {
    if (!this.#pushToTalk || this.#muted || this.#speaking || this.#node === null) {
      return;
    }
    this.#beginUtterance();
  }

  /** Stop capturing because the key came up, uploading what was captured. */
  endPush(): void {
    if (!this.#pushToTalk || !this.#speaking) {
      return;
    }
    // Nothing to trim: the key ended the turn, so there is no closing silence to drop.
    this.#finishUtterance(0);
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

    if (this.#pushToTalk) {
      // The key decides; the level is not consulted at all. Frames still flow into the same two
      // buffers, so the padding and the upload are assembled by the same code either way.
      if (this.#speaking) {
        this.#utterance.push(frame);
      } else {
        this.#rememberForPadding(frame);
      }
      return;
    }

    // TR-116. A capture that is still open at the moment the agent starts talking did not begin
    // as a question -- it began as a chair, a cough, or a fragment of the agent's own previous
    // sentence -- and if it is left open the agent's voice keeps resetting the silence counter, so
    // the turn never ends. That is not merely a lost utterance: while a capture is open, no new
    // onset can be declared, so the person cannot interrupt at all. Abandoning it here is what
    // keeps barge-in available for the sentence about to be spoken.
    const playing = this.#handlers.isPlaying?.() ?? true;
    const startedPlaying = playing && !this.#wasPlaying;
    this.#wasPlaying = playing;
    if (startedPlaying && this.#speaking) {
      this.#resetDetection();
      this.#handlers.onMisfire();
      return;
    }

    if (!this.#speaking) {
      this.#rememberForPadding(frame);
      this.#loudRun = rms >= SPEECH_RMS ? this.#loudRun + 1 : 0;
      // While the agent is audible, onset needs more evidence: the cautious rule resists echo.
      const needed = playing ? ONSET_FRAMES_WHILE_PLAYING : ONSET_FRAMES_WHILE_IDLE;
      if (this.#loudRun >= needed) {
        this.#beginUtterance();
      }
      return;
    }

    this.#utterance.push(frame);
    this.#silentRun = rms <= SILENCE_RMS ? this.#silentRun + 1 : 0;

    if (this.#utterance.length >= MAX_UTTERANCE_FRAMES) {
      // TR-116. Twenty seconds of unbroken sound is not a question. If the agent is audible it is
      // almost certainly its own voice leaking past echo cancellation, and uploading that would
      // ask the model to answer itself; otherwise it is a noisy room, and whatever was said is
      // worth transcribing. Either way the capture ends here, so the next onset can happen.
      if (playing) {
        this.#resetDetection();
        this.#handlers.onMisfire();
      } else {
        this.#finishUtterance(0);
      }
      return;
    }

    if (this.#silentRun >= REDEMPTION_FRAMES) {
      // TR-114: what is uploaded is the padding plus the speech. The silence that ended the turn
      // is dropped -- every frame in that run is below the silence threshold by definition, so it
      // carries no words, and sending it would only make the transcriber wait longer to answer.
      this.#finishUtterance(REDEMPTION_FRAMES);
    }
  }

  /** Keep a rolling window of what came just before a turn, so the first syllable survives. */
  #rememberForPadding(frame: Float32Array): void {
    this.#preRoll.push(frame);
    if (this.#preRoll.length > PRE_ROLL_FRAMES) {
      this.#preRoll.shift();
    }
  }

  /** Open a turn, seeding it with the padding, and tell the session. */
  #beginUtterance(): void {
    this.#speaking = true;
    this.#silentRun = 0;
    this.#utterance = [...this.#preRoll];
    this.#preRollFrames = this.#preRoll.length;
    this.#preRoll = [];
    this.#handlers.onSpeechStart();
  }

  #finishUtterance(tailFrames: number): void {
    const frames = this.#utterance.slice(0, this.#utterance.length - tailFrames);
    const spokenFrames = frames.length - this.#preRollFrames;
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
    this.#preRollFrames = 0;
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
