/**
 * The client tier of barge-in, wired the way the app wires it.
 *
 * The interesting behaviour is an ordering: the speakers must go quiet *before* anything is put on
 * the wire, because a round trip is audible and the whole feature is judged by ear (PRD F7,
 * TR-113). An ordering cannot be asserted from either side alone, so this file drives the real
 * `useSession` with the real `PlaybackQueue` and only the two things a test machine cannot have --
 * a microphone and an audio device -- replaced.
 */

import { act, fireEvent, render, screen } from "@testing-library/react";
import type { JSX } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { MicrophoneHandlers } from "../audio/microphone";
import { AUDIO_HEADER_BYTES } from "../protocol";
import type { ClientMessage, Deck, ServerMessage } from "../protocol";
import { useSessionStore } from "../store";

import { useSession } from "./useSession";

/** Handlers the hook passed to the microphone it built, so a test can play the part of a speaker. */
let handlers: MicrophoneHandlers | null = null;

/** Every call made on the microphone, in order, so lifecycle can be asserted. */
let micCalls: string[] = [];

vi.mock("../audio/microphone", () => ({
  Microphone: class {
    constructor(given: MicrophoneHandlers) {
      handlers = given;
    }
    start(): Promise<void> {
      micCalls.push("start");
      return Promise.resolve();
    }
    setPushToTalk(enabled: boolean): void {
      micCalls.push(`ptt:${String(enabled)}`);
    }
    beginPush(): void {
      micCalls.push("beginPush");
    }
    endPush(): void {
      micCalls.push("endPush");
    }
    mute(): void {
      micCalls.push("mute");
    }
    unmute(): void {
      micCalls.push("unmute");
    }
    stop(): Promise<void> {
      micCalls.push("stop");
      return Promise.resolve();
    }
  },
}));

const SOCKET_URL = "ws://test.invalid/ws/session";
const DECK_ID = "anatomy_of_a_voice_agent";
const SOCKET_OPEN = 1;
const SOCKET_CLOSED = 3;
const FRAME_SAMPLES = 2400;

/** Everything the audio graph did, in the order it happened, shared with the socket's record. */
let events: string[] = [];

/** A stand-in for the output graph that records stops, so a flush is observable. */
class FakeAudioContext {
  static instances: FakeAudioContext[] = [];

  currentTime = 0;
  state: AudioContextState = "running";
  readonly destination = {} as AudioDestinationNode;
  closed = false;
  readonly sources: { onended: (() => void) | null }[] = [];

  constructor() {
    FakeAudioContext.instances.push(this);
  }

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
    const node = {
      buffer: null as AudioBuffer | null,
      onended: null as (() => void) | null,
      connect: () => undefined,
      disconnect: () => undefined,
      start: () => undefined,
      stop: () => {
        events.push("audio stopped");
      },
    };
    this.sources.push(node);
    return node as unknown as AudioBufferSourceNode;
  }

  /**
   * Let the earliest scheduled frames finish, as the device would when they have been heard.
   *
   * @param count - How many frames to retire, oldest first.
   */
  finish(count: number): void {
    for (const source of this.sources.splice(0, count)) {
      source.onended?.();
    }
  }

  async close(): Promise<void> {
    this.closed = true;
    this.state = "closed";
  }
}

/** A WebSocket stand-in that records sends into the shared ordering. */
class FakeSocket extends EventTarget {
  static instances: FakeSocket[] = [];

  binaryType: BinaryType = "blob";
  readyState = 0;
  readonly sent: unknown[] = [];

  readonly url: string;

  constructor(url: string) {
    super();
    this.url = url;
    FakeSocket.instances.push(this);
  }

  send(data: unknown): void {
    this.sent.push(data);
    if (typeof data === "string") {
      events.push(`sent ${(JSON.parse(data) as ClientMessage).type}`);
    } else {
      events.push("sent audio");
    }
  }

  close(): void {
    this.readyState = SOCKET_CLOSED;
  }

  openFromServer(): void {
    this.readyState = SOCKET_OPEN;
    this.dispatchEvent(new Event("open"));
  }

  receive(message: ServerMessage): void {
    this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(message) }));
  }

  /**
   * Deliver one frame of spoken audio, framed as the backend frames it.
   *
   * @param sentenceId - Which sentence the frame belongs to.
   * @param seq - Position within that sentence.
   */
  receiveAudio(sentenceId: number, seq: number): void {
    const buffer = new ArrayBuffer(AUDIO_HEADER_BYTES + FRAME_SAMPLES * 2);
    const view = new DataView(buffer);
    // Little-endian, matching `AUDIO_HEADER = struct.Struct("<II")` on the server. Getting this
    // backwards is invisible for sentence 0 and wrong for every sentence after it.
    view.setUint32(0, sentenceId, true);
    view.setUint32(4, seq, true);
    this.dispatchEvent(new MessageEvent("message", { data: buffer }));
  }

  get sentMessages(): ClientMessage[] {
    return this.sent
      .filter((item): item is string => typeof item === "string")
      .map((item) => JSON.parse(item) as ClientMessage);
  }
}

function createSocket(url: string): WebSocket {
  return new FakeSocket(url) as unknown as WebSocket;
}

function makeDeck(): Deck {
  return {
    id: DECK_ID,
    title: "Anatomy of a Voice Agent",
    voice: "af_heart",
    slides: Array.from({ length: 6 }, (_, i) => ({
      index: i + 1,
      title: `Chapter ${String(i + 1)}`,
      bullets: ["a", "b"],
      notes: "Speaker notes.",
      aliases: [],
    })),
  };
}

function ready(deck: Deck): ServerMessage {
  return {
    type: "session.ready",
    session_id: "sess-1",
    protocol_version: 1,
    deck,
    providers: { stt: "groq", llm: "groq", tts: "kokoro" },
  };
}

/** Messages every session sends regardless of turn-taking, excluded so the exchange is legible. */
const BACKGROUND_MESSAGES = new Set(["session.start", "playback.progress"]);

/**
 * The turn-taking messages the session put on the wire.
 *
 * The handshake and the playback reports are filtered out: both happen on their own schedule and
 * would make every assertion here about ordering that is not under test.
 *
 * @param socket - The socket under test.
 * @returns The message types in order.
 */
function sentTypes(socket: FakeSocket): string[] {
  return socket.sentMessages
    .map((message) => message.type)
    .filter((type) => !BACKGROUND_MESSAGES.has(type));
}

/** Let every outstanding frame finish, so nothing is playing any more. */
function drainAudio(): void {
  const context = FakeAudioContext.instances.at(-1);
  act(() => {
    context?.finish(context.sources.length);
  });
}

function Harness(): JSX.Element {
  const session = useSession({ url: SOCKET_URL, createSocket });
  return (
    <div>
      <button type="button" onClick={session.start}>
        start
      </button>
      <button type="button" onClick={session.stop}>
        stop
      </button>
      <button
        type="button"
        onClick={() => {
          session.setMuted(!session.muted);
        }}
      >
        toggle-mute
      </button>
    </div>
  );
}

/**
 * Start a session and put the agent mid-sentence, speaking sentence 1.
 *
 * Sentence 0 is completed first, because what the server needs to know at a barge-in is how much
 * was actually heard, and that number is only non-trivial once a sentence has finished.
 *
 * @returns The open socket.
 */
function speakingSession(): FakeSocket {
  fireEvent.click(screen.getByRole("button", { name: "start" }));
  const socket = FakeSocket.instances.at(-1);
  if (socket === undefined) {
    throw new Error("start() did not open a socket");
  }
  act(() => {
    socket.openFromServer();
  });
  act(() => {
    socket.receive(ready(makeDeck()));
    socket.receive({ type: "state", value: "speaking", turn_id: 1, server_ts: 0 });
    socket.receiveAudio(0, 0);
    socket.receiveAudio(1, 0);
    socket.receiveAudio(1, 1);
  });
  // The first sentence has been heard; the second is mid-flight, which is where an interruption
  // is interesting.
  act(() => {
    FakeAudioContext.instances.at(-1)?.finish(1);
  });
  events = [];
  return socket;
}

describe("client tier of barge-in", () => {
  beforeEach(() => {
    FakeSocket.instances = [];
    FakeAudioContext.instances = [];
    handlers = null;
    micCalls = [];
    events = [];
    useSessionStore.getState().reset();
    vi.stubGlobal("AudioContext", FakeAudioContext);
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve({ ok: false, json: () => Promise.resolve({}) } as Response)),
    );
    render(<Harness />);
  });

  afterEach(() => {
    useSessionStore.getState().reset();
    vi.unstubAllGlobals();
  });

  it("TC-FE-021: silences the speakers before it tells the server anything", () => {
    const socket = speakingSession();

    act(() => {
      handlers?.onSpeechStart();
    });

    expect(events.indexOf("audio stopped")).toBeGreaterThanOrEqual(0);
    expect(events.indexOf("audio stopped")).toBeLessThan(events.indexOf("sent interrupt"));
    // An interruption, not a fresh turn: `speech.start` would tell the server the wrong thing.
    expect(sentTypes(socket)).toEqual(["interrupt"]);
  });

  it("TC-FE-021: reports the last sentence the listener actually heard", () => {
    const socket = speakingSession();

    act(() => {
      handlers?.onSpeechStart();
    });

    const interrupt = socket.sentMessages.find((message) => message.type === "interrupt");
    // Sentence 0 finished, because a frame of sentence 1 arrived after it; sentence 1 did not.
    expect(interrupt).toEqual({ type: "interrupt", last_completed_sentence_id: 0 });
  });

  it("TC-FE-021: records how long the audio took to stop, for the latency panel", () => {
    speakingSession();

    act(() => {
      handlers?.onSpeechStart();
    });

    const sample = useSessionStore.getState().metrics.last;
    expect(sample?.turnId).toBe(1);
    expect(sample?.interruptStopMs).toBeGreaterThanOrEqual(0);
    // The point of the client tier: silence is local, so it costs no round trip.
    expect(sample?.interruptStopMs).toBeLessThan(50);
  });

  it("speech while the agent is silent is a plain turn, not an interruption", () => {
    const socket = speakingSession();
    act(() => {
      socket.receive({ type: "state", value: "listening", turn_id: 1, server_ts: 0 });
      socket.receive({
        type: "metrics",
        turn_id: 1,
        stt_ms: 200,
        llm_ttft_ms: 300,
        llm_total_ms: 700,
        tts_ttfb_ms: 90,
        sentences: 2,
      });
    });

    // The turn is over and its audio has been heard, so nothing is playing any more.
    drainAudio();

    act(() => {
      handlers?.onSpeechStart();
    });

    expect(sentTypes(socket)).toContain("speech.start");
    expect(sentTypes(socket)).not.toContain("interrupt");
  });

  it("TC-FE-022: a cough that silenced the agent is taken back", () => {
    const socket = speakingSession();

    act(() => {
      handlers?.onSpeechStart();
    });
    act(() => {
      handlers?.onMisfire();
    });

    expect(sentTypes(socket)).toEqual(["interrupt", "interrupt.cancel"]);
  });

  it("TC-FE-022: a cough while the agent was already silent is not reported at all", () => {
    const socket = speakingSession();
    act(() => {
      socket.receive({ type: "state", value: "listening", turn_id: 1, server_ts: 0 });
    });
    drainAudio();
    events = [];

    act(() => {
      handlers?.onSpeechStart();
    });
    act(() => {
      handlers?.onMisfire();
    });

    expect(sentTypes(socket)).toEqual(["speech.start"]);
  });

  it("a real utterance after an interruption is uploaded, and the cancel is not sent", () => {
    const socket = speakingSession();

    act(() => {
      handlers?.onSpeechStart();
    });
    act(() => {
      handlers?.onUtterance(new Int16Array(FRAME_SAMPLES), 1200);
    });

    // Two frames of the interrupted sentence are silenced; the sentence already heard is gone.
    expect(events).toEqual([
      "audio stopped",
      "audio stopped",
      "sent interrupt",
      "sent speech.end",
      "sent audio",
    ]);
    const end = socket.sentMessages.find((message) => message.type === "speech.end");
    expect(end).toEqual({ type: "speech.end", duration_ms: 1200 });
  });

  it("mute and unmute reach the device, and ending the session releases it", () => {
    speakingSession();

    fireEvent.click(screen.getByRole("button", { name: "toggle-mute" }));
    fireEvent.click(screen.getByRole("button", { name: "toggle-mute" }));
    fireEvent.click(screen.getByRole("button", { name: "stop" }));

    expect(micCalls).toEqual(["ptt:false", "start", "mute", "unmute", "stop"]);
  });

  it("TC-FE-206: a false onset before the agent speaks does not stop a later interruption", () => {
    const socket = speakingSession();

    // 1. Something opened a capture before the agent was audible -- a click, a chair, a breath --
    //    so the client announced speech rather than an interruption.
    act(() => {
      socket.receive({ type: "state", value: "listening", turn_id: 1, server_ts: 0 });
    });
    drainAudio();
    act(() => {
      handlers?.onSpeechStart();
    });
    expect(sentTypes(socket)).toEqual(["speech.start"]);

    // 2. The agent starts talking. The microphone abandons that stale capture and says so, which
    //    is what lets a real onset be declared afterwards (TR-116).
    act(() => {
      socket.receive({ type: "state", value: "speaking", turn_id: 2, server_ts: 0 });
      socket.receiveAudio(0, 0);
      socket.receiveAudio(0, 1);
    });
    act(() => {
      handlers?.onMisfire();
    });

    // 3. Now the listener talks over it. This is the interruption that used to be impossible.
    act(() => {
      handlers?.onSpeechStart();
    });

    expect(sentTypes(socket)).toEqual(["speech.start", "interrupt"]);
    expect(events).toContain("audio stopped");
  });

  it("TC-FE-207: an abandoned capture that had already interrupted takes the interrupt back", () => {
    const socket = speakingSession();

    act(() => {
      handlers?.onSpeechStart();
    });
    act(() => {
      handlers?.onMisfire();
    });

    // The agent was silenced for something that turned out not to be speech, and the server is
    // told so rather than being left waiting for a question that is never coming.
    expect(sentTypes(socket)).toEqual(["interrupt", "interrupt.cancel"]);
  });
});
