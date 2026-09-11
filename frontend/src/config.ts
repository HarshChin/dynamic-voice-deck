/**
 * Central tunables for the frontend.
 *
 * Everything that a reviewer might want to change without reading code lives here: the endpoints,
 * the audio rates, and the VAD thresholds from TRD §5.3. Values are frozen `as const` so a typo at
 * a call site is a compile error rather than a silent `undefined`.
 */

/** Base path of the HTTP API. Relative, so the Vite dev-server proxy handles it. */
export const API_BASE_PATH = "/api";

/** Path of the session WebSocket endpoint (TRD §6). */
export const WS_SESSION_PATH = "/ws/session";

/** The parts of `Location` needed to build a same-origin WebSocket URL. */
export interface OriginLike {
  readonly protocol: string;
  readonly host: string;
}

/**
 * Build the session WebSocket URL for the page's own origin.
 *
 * Deriving it from `window.location` rather than an environment variable is what lets the dev
 * server proxy `/ws` to uvicorn (see vite.config.ts) while the same build, served from any host,
 * keeps working. The scheme follows the page: a page on `https:` must not open a `ws:` socket, as
 * browsers block mixed content.
 *
 * @param path - Endpoint path; defaults to the session endpoint.
 * @param origin - Source of the scheme and host; defaults to the current page location.
 * @returns An absolute `ws:` or `wss:` URL.
 */
export function websocketUrl(
  path: string = WS_SESSION_PATH,
  origin: OriginLike = window.location,
): string {
  const scheme = origin.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${origin.host}${path}`;
}

/**
 * Sample rates in hertz.
 *
 * Capture is resampled to 16 kHz because that is what Whisper expects; playback runs at 24 kHz
 * because that is what Kokoro emits (TR-101, TR-120).
 */
export const AUDIO_RATES = {
  input: 16_000,
  output: 24_000,
} as const;

/**
 * Speech detection parameters (PRD §F3, TR-111).
 *
 * The two levels are asymmetric on purpose: entering speech takes more energy than staying in it,
 * so a dip mid-word does not end the utterance. They are root-mean-square amplitudes rather than
 * the probabilities the design originally specified, because detection is an energy threshold and
 * not Silero; TR-110 records why. The timings are unchanged from the design: `redemptionMs` is the
 * silence tolerated before an utterance is considered over, `minSpeechMs` discards coughs and lip
 * smacks, and `preSpeechPadMs` is how much audio is kept from before the onset so the first
 * phoneme survives.
 */
export const VAD = {
  speechRms: 0.02,
  silenceRms: 0.012,
  redemptionMs: 600,
  minSpeechMs: 250,
  preSpeechPadMs: 300,
  /** Consecutive loud frames required to declare onset while the agent is audible (TR-112). */
  onsetFramesWhilePlaying: 3,
  /** Consecutive loud frames required while nothing is playing, where there is no echo to resist. */
  onsetFramesWhileIdle: 1,
  /**
   * Longest a single capture may run before the detector decides it is stuck (TR-116).
   *
   * Twenty seconds is far longer than anyone asks a slide deck a question for, and far shorter
   * than the server's own limit, so a capture that reaches it is a detector that has stopped
   * endpointing rather than a person who is still talking.
   */
  maxUtteranceMs: 20_000,
} as const;

/**
 * Reconnect policy for `SessionClient` (TR-175).
 *
 * Exactly one automatic attempt: a second silent retry hides a backend that is genuinely down,
 * and the UI's retry button is the honest way to ask for more.
 */
export const RECONNECT = {
  maxAutomaticAttempts: 1,
  delayMs: 500,
} as const;
