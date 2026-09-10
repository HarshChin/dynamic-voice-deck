/**
 * Microphone capture and speech detection, entirely in the browser.
 *
 * Silero VAD scores short frames of microphone audio on-device and reports when speech starts and
 * stops. Running it here rather than on the server is the decision the whole interruption design
 * rests on (TR-110): the moment you start talking is known locally, so playback can be silenced
 * without waiting for a network round trip.
 *
 * The trade-off is stated honestly on slide 3 of the deck. Only a finished utterance is uploaded, so
 * transcription cannot begin until you stop speaking. In exchange one request covers a whole turn,
 * and barge-in costs nothing.
 *
 * Assets are served from `public/vad/` rather than a CDN, so the microphone keeps working offline
 * and on a hostile conference network.
 */

import { MicVAD } from "@ricky0123/vad-web";

import { AUDIO_RATES, VAD } from "../config";

/** Where `npm run postinstall` puts the detector's model and worklet. */
const ASSET_PATH = "/vad/";

/** What the microphone reports to the session. */
export interface MicrophoneHandlers {
  /** Speech began. Returns nothing; the session decides whether this is a barge-in. */
  readonly onSpeechStart: () => void;
  /** A finished utterance, as 16 kHz mono PCM16 ready for the wire. */
  readonly onUtterance: (pcm16: Int16Array, durationMs: number) => void;
  /** Speech began but was too short to be a turn, so nothing will be uploaded. */
  readonly onMisfire: () => void;
  /** The microphone could not be opened, with a message fit to show a person. */
  readonly onError: (message: string) => void;
}

/**
 * Convert the detector's float samples to the wire format.
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

/**
 * Owns the microphone and the speech detector for one session.
 */
export class Microphone {
  readonly #handlers: MicrophoneHandlers;
  #vad: MicVAD | null = null;
  #starting: Promise<void> | null = null;
  #muted = false;

  /**
   * @param handlers - Callbacks the session supplies.
   */
  constructor(handlers: MicrophoneHandlers) {
    this.#handlers = handlers;
  }

  /** Whether the detector is running and not muted. */
  get isListening(): boolean {
    return this.#vad !== null && !this.#muted;
  }

  /**
   * Ask for the microphone and start listening.
   *
   * Safe to call twice: the second call awaits the first rather than opening a second device, which
   * matters because React's StrictMode runs effects twice in development.
   *
   * @returns Resolves once the detector is running, or after `onError` has been called.
   */
  async start(): Promise<void> {
    if (this.#vad !== null) {
      return;
    }
    this.#starting ??= this.#startOnce();
    await this.#starting;
  }

  /** Stop listening without releasing the device, so it can resume quickly. */
  mute(): void {
    this.#muted = true;
    void this.#vad?.pause();
  }

  /** Resume listening after {@link mute}. */
  unmute(): void {
    this.#muted = false;
    void this.#vad?.start();
  }

  /** Release the microphone. The browser's recording indicator turns off here. */
  async stop(): Promise<void> {
    const vad = this.#vad;
    this.#vad = null;
    this.#starting = null;
    this.#muted = false;
    if (vad !== null) {
      await vad.pause();
      // Releases the media stream and the worklet; the browser's recording
      // indicator only turns off once this has run.
      await vad.destroy();
    }
  }

  async #startOnce(): Promise<void> {
    try {
      const vad = await MicVAD.new({
        model: "v5",
        baseAssetPath: ASSET_PATH,
        onnxWASMBasePath: ASSET_PATH,
        positiveSpeechThreshold: VAD.positiveSpeechThreshold,
        negativeSpeechThreshold: VAD.negativeSpeechThreshold,
        redemptionMs: VAD.redemptionMs,
        minSpeechMs: VAD.minSpeechMs,
        preSpeechPadMs: VAD.preSpeechPadMs,
        // Opening the microphone here rather than letting the library do it, so the constraints
        // are ours. Echo cancellation is the one that matters: without it the agent's own voice,
        // played through speakers, is heard as the user interrupting, and it talks itself down
        // within a sentence.
        getStream: () =>
          navigator.mediaDevices.getUserMedia({
            audio: {
              channelCount: 1,
              echoCancellation: true,
              noiseSuppression: true,
              autoGainControl: true,
            },
          }),
        onSpeechStart: () => {
          if (!this.#muted) {
            this.#handlers.onSpeechStart();
          }
        },
        onSpeechEnd: (audio: Float32Array) => {
          if (this.#muted) {
            return;
          }
          const durationMs = Math.round((audio.length / AUDIO_RATES.input) * 1000);
          this.#handlers.onUtterance(floatToPcm16(audio), durationMs);
        },
        onVADMisfire: () => {
          if (!this.#muted) {
            this.#handlers.onMisfire();
          }
        },
      });
      this.#vad = vad;
      await vad.start();
    } catch (error) {
      this.#starting = null;
      this.#handlers.onError(describeMicrophoneError(error));
    }
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
