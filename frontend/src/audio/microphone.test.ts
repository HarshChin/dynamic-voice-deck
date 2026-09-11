import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { VAD } from "../config";

import { Microphone, describeMicrophoneError, floatToPcm16 } from "./microphone";

/** Samples per frame from the worklet, matching `public/worklets/capture.js`. */
const FRAME_SAMPLES = 512;

/** Milliseconds of audio in one frame: 32 ms at 16 kHz. */
const FRAME_MS = 32;

/** Frames of padding kept from before onset. */
const PRE_ROLL_FRAMES = Math.ceil(VAD.preSpeechPadMs / FRAME_MS);

/** Silent frames that end a turn. */
const REDEMPTION_FRAMES = Math.ceil(VAD.redemptionMs / FRAME_MS);

/** A level comfortably above the speech threshold. */
const LOUD = 0.2;

/** A level comfortably below the silence threshold. */
const QUIET = 0.001;

/** A stand-in for the port an `AudioWorkletNode` exposes, so a test can push frames through it. */
class FakePort {
  onmessage: ((event: MessageEvent<{ frame: Float32Array; rms: number }>) => void) | null = null;
  closed = false;

  close(): void {
    this.closed = true;
  }
}

/** A stand-in for the worklet node. Only the port and the graph calls are used. */
class FakeWorkletNode {
  readonly port = new FakePort();
  disconnected = false;

  disconnect(): void {
    this.disconnected = true;
  }
}

/** A stand-in for a microphone track, which is what has to be stopped to release the device. */
class FakeTrack {
  stopped = false;

  stop(): void {
    this.stopped = true;
  }
}

/** A stand-in for the stream `getUserMedia` resolves with. */
class FakeStream {
  readonly tracks = [new FakeTrack()];

  getTracks(): FakeTrack[] {
    return this.tracks;
  }
}

/**
 * A stand-in for the capture graph.
 *
 * Nothing here synthesises audio: frames are pushed through {@link FakePort} by the test, which is
 * the same seam the real worklet uses, so the detector under test runs unmodified.
 */
class FakeAudioContext {
  static instances: FakeAudioContext[] = [];

  state: AudioContextState = "running";
  closed = false;
  node: FakeWorkletNode | null = null;
  readonly addedModules: string[] = [];
  readonly audioWorklet = {
    addModule: (url: string): Promise<void> => {
      this.addedModules.push(url);
      return Promise.resolve();
    },
  };

  constructor() {
    FakeAudioContext.instances.push(this);
  }

  createMediaStreamSource(_stream: MediaStream): { connect: (node: unknown) => void } {
    return { connect: () => undefined };
  }

  async close(): Promise<void> {
    this.closed = true;
    this.state = "closed";
  }
}

interface Recorded {
  readonly starts: number;
  readonly misfires: number;
  readonly errors: string[];
  readonly utterances: { pcm: Int16Array; durationMs: number }[];
}

/** What the last-constructed microphone reported, and the graph it built. */
let seen: Recorded;
let streams: FakeStream[];
let playing: boolean;
let mediaError: DOMException | null = null;

function counters(): Recorded {
  return { starts: 0, misfires: 0, errors: [], utterances: [] };
}

/**
 * Open a microphone against the fake graph.
 *
 * @returns The started microphone and the port frames are pushed through.
 */
async function open(): Promise<{ mic: Microphone; port: FakePort }> {
  const record = seen as { starts: number; misfires: number };
  const mic = new Microphone({
    isPlaying: () => playing,
    onSpeechStart: () => {
      record.starts += 1;
    },
    onUtterance: (pcm, durationMs) => seen.utterances.push({ pcm, durationMs }),
    onMisfire: () => {
      record.misfires += 1;
    },
    onError: (message) => seen.errors.push(message),
  });
  await mic.start();
  const context = FakeAudioContext.instances.at(-1);
  const port = context?.node?.port;
  if (port === undefined) {
    throw new Error("the microphone did not build a capture graph");
  }
  return { mic, port };
}

/**
 * Push frames of a given loudness through the worklet port.
 *
 * @param port - The port the microphone is listening on.
 * @param count - How many frames to send.
 * @param rms - The level to report for each.
 */
function push(port: FakePort, count: number, rms: number): void {
  for (let i = 0; i < count; i += 1) {
    port.onmessage?.({
      data: { frame: new Float32Array(FRAME_SAMPLES).fill(rms), rms },
    } as MessageEvent<{ frame: Float32Array; rms: number }>);
  }
}

beforeEach(() => {
  seen = counters();
  streams = [];
  playing = false;
  mediaError = null;
  FakeAudioContext.instances = [];

  vi.stubGlobal(
    "AudioContext",
    class extends FakeAudioContext {
      constructor() {
        super();
      }
    },
  );
  vi.stubGlobal("AudioWorkletNode", function AudioWorkletNodeStub(this: unknown, context: unknown) {
    const node = new FakeWorkletNode();
    (context as FakeAudioContext).node = node;
    return node;
  });
  vi.stubGlobal("navigator", {
    mediaDevices: {
      getUserMedia: (constraints: MediaStreamConstraints) => {
        if (mediaError !== null) {
          return Promise.reject(mediaError);
        }
        expect(constraints.audio).toMatchObject({ echoCancellation: true, channelCount: 1 });
        const stream = new FakeStream();
        streams.push(stream);
        return Promise.resolve(stream as unknown as MediaStream);
      },
    },
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("onset", () => {
  it("TC-FE-020: while the agent is speaking, two loud frames are not onset and the third is", async () => {
    playing = true;
    const { port } = await open();

    push(port, 2, LOUD);
    expect(seen.starts).toBe(0);

    push(port, 1, LOUD);
    expect(seen.starts).toBe(1);
  });

  it("TC-FE-020: a lone loud frame among quiet ones never reaches onset", async () => {
    playing = true;
    const { port } = await open();

    for (let i = 0; i < 5; i += 1) {
      push(port, 2, LOUD);
      push(port, 1, QUIET);
    }

    expect(seen.starts).toBe(0);
  });

  it("TR-112: with nothing playing, the first loud frame is onset", async () => {
    playing = false;
    const { port } = await open();

    push(port, 1, LOUD);

    expect(seen.starts).toBe(1);
  });
});

describe("endpointing", () => {
  it("TC-FE-023: what is uploaded is the padding plus the speech, and not the closing silence", async () => {
    const speechFrames = 20;
    const { port } = await open();

    // Long enough before onset that the pre-roll window is full.
    push(port, PRE_ROLL_FRAMES + 5, QUIET);
    push(port, speechFrames, LOUD);
    push(port, REDEMPTION_FRAMES, QUIET);

    expect(seen.utterances).toHaveLength(1);
    const utterance = seen.utterances[0];
    const frames = (utterance?.pcm.length ?? 0) / FRAME_SAMPLES;
    // One frame short of the sum, and necessarily so: the frame that triggered onset is already
    // sitting in the pre-roll window, so it is padding and speech at once rather than counted
    // twice. That is the one frame of slack TR-114 allows.
    expect(frames).toBe(PRE_ROLL_FRAMES + speechFrames - 1);
    expect(utterance?.durationMs).toBe(Math.round(frames * FRAME_MS));
    // The trailing silence really is gone: 600 ms of it would be a fifth of this upload.
    expect(frames * FRAME_MS).toBeLessThan((PRE_ROLL_FRAMES + speechFrames) * FRAME_MS);
  });

  it("TC-FE-023: an onset in the first moments keeps whatever padding exists", async () => {
    const { port } = await open();

    push(port, 2, QUIET);
    push(port, 20, LOUD);
    push(port, REDEMPTION_FRAMES, QUIET);

    const frames = (seen.utterances[0]?.pcm.length ?? 0) / FRAME_SAMPLES;
    expect(frames).toBe(22);
  });

  it("TC-FE-022: a 160 ms utterance is a misfire, so nothing is uploaded", async () => {
    const { port } = await open();

    push(port, PRE_ROLL_FRAMES + 5, QUIET);
    // Five frames is 160 ms: past onset, short of the 250 ms minimum.
    push(port, 5, LOUD);
    push(port, REDEMPTION_FRAMES, QUIET);

    expect(seen.starts).toBe(1);
    expect(seen.misfires).toBe(1);
    expect(seen.utterances).toEqual([]);
  });

  it("a dip in the middle of a word does not end the utterance", async () => {
    const { port } = await open();

    push(port, 10, LOUD);
    push(port, REDEMPTION_FRAMES - 1, QUIET);
    push(port, 10, LOUD);
    expect(seen.utterances).toEqual([]);

    push(port, REDEMPTION_FRAMES, QUIET);
    expect(seen.utterances).toHaveLength(1);
  });

  it("a second turn is detected after the first has been uploaded", async () => {
    const { port } = await open();

    for (let turn = 0; turn < 2; turn += 1) {
      push(port, 20, LOUD);
      push(port, REDEMPTION_FRAMES, QUIET);
    }

    expect(seen.starts).toBe(2);
    expect(seen.utterances).toHaveLength(2);
  });
});

describe("mute", () => {
  it("drops frames while muted and detects again after unmute", async () => {
    const { mic, port } = await open();

    mic.mute();
    expect(mic.isListening).toBe(false);
    push(port, 20, LOUD);
    push(port, REDEMPTION_FRAMES, QUIET);
    expect(seen.starts).toBe(0);

    mic.unmute();
    expect(mic.isListening).toBe(true);
    push(port, 20, LOUD);
    push(port, REDEMPTION_FRAMES, QUIET);
    expect(seen.utterances).toHaveLength(1);
  });

  it("muting mid-utterance abandons it rather than uploading half a sentence", async () => {
    const { mic, port } = await open();

    push(port, 20, LOUD);
    mic.mute();
    mic.unmute();
    push(port, REDEMPTION_FRAMES, QUIET);

    expect(seen.utterances).toEqual([]);
  });
});

describe("device lifetime", () => {
  it("TC-FE-035: five start/stop cycles leave no live track and no open context", async () => {
    for (let cycle = 0; cycle < 5; cycle += 1) {
      const { mic } = await open();
      await mic.stop();
    }

    expect(streams).toHaveLength(5);
    expect(streams.every((stream) => stream.tracks.every((track) => track.stopped))).toBe(true);
    expect(FakeAudioContext.instances).toHaveLength(5);
    expect(FakeAudioContext.instances.every((context) => context.closed)).toBe(true);
  });

  it("TR-103: starting twice opens one device, as React's double-invoked effects require", async () => {
    const mic = new Microphone({
      onSpeechStart: () => undefined,
      onUtterance: () => undefined,
      onMisfire: () => undefined,
      onError: (message) => seen.errors.push(message),
    });

    await Promise.all([mic.start(), mic.start()]);
    await mic.start();

    expect(streams).toHaveLength(1);
    await mic.stop();
  });

  it("loads the capture worklet by URL rather than importing it", async () => {
    await open();

    expect(FakeAudioContext.instances.at(-1)?.addedModules).toEqual(["/worklets/capture.js"]);
  });

  it("a refused microphone is reported once and leaves nothing open", async () => {
    mediaError = new DOMException("denied", "NotAllowedError");

    const mic = new Microphone({
      onSpeechStart: () => undefined,
      onUtterance: () => undefined,
      onMisfire: () => undefined,
      onError: (message) => seen.errors.push(message),
    });
    await mic.start();

    expect(seen.errors).toHaveLength(1);
    expect(seen.errors[0]).toContain("permission was refused");
    expect(mic.isListening).toBe(false);
    expect(FakeAudioContext.instances).toEqual([]);
  });
});

describe("conversion", () => {
  it("scales to full range and clamps beyond it", () => {
    const pcm = floatToPcm16(new Float32Array([0, 1, -1, 2, -2, 0.5]));

    expect(Array.from(pcm)).toEqual([0, 32767, -32767, 32767, -32767, 16384]);
  });

  it("produces two bytes per sample", () => {
    expect(floatToPcm16(new Float32Array(FRAME_SAMPLES)).byteLength).toBe(FRAME_SAMPLES * 2);
  });
});

describe("error messages", () => {
  it.each([
    ["NotAllowedError", "permission was refused"],
    ["SecurityError", "permission was refused"],
    ["NotFoundError", "no microphone was found"],
    ["OverconstrainedError", "no microphone was found"],
    ["NotReadableError", "in use by another application"],
  ])("%s names the cause and the fix", (name, expected) => {
    expect(describeMicrophoneError(new DOMException("x", name))).toContain(expected);
  });

  it("an unrecognised failure still quotes what went wrong", () => {
    expect(describeMicrophoneError(new Error("boom"))).toContain("boom");
  });

  it("something that is not an error at all is still described", () => {
    expect(describeMicrophoneError("odd")).toContain("odd");
  });
});

describe("a capture that would otherwise never end (TR-116)", () => {
  it("TC-FE-200: the agent starting to speak abandons a capture that was already open", async () => {
    const { port } = await open();

    // Something opened a capture just before the agent started: a chair, a cough, or the tail of
    // its own previous sentence.
    push(port, 3, LOUD);
    expect(seen.starts).toBe(1);

    playing = true;
    push(port, 1, QUIET);

    // The stale capture is gone, and the session is told so it can take back any interrupt.
    expect(seen.misfires).toBe(1);
    expect(seen.utterances).toEqual([]);
  });

  it("TC-FE-201: and because it is gone, the listener can still interrupt", async () => {
    const { port } = await open();
    push(port, 3, LOUD);
    playing = true;
    push(port, 1, QUIET);
    const onsetsSoFar = seen.starts;

    // Now the person actually talks over the agent. This is the interruption that used to be
    // impossible: while a capture was open, no new onset could ever be declared.
    push(port, 3, LOUD);

    expect(seen.starts).toBe(onsetsSoFar + 1);
  });

  it("TC-FE-202: the agent's own voice never becomes a question the model has to answer", async () => {
    playing = true;
    const { port } = await open();

    // Onset while the agent is audible is a genuine barge-in, so the capture is kept. But if it
    // then runs for twenty seconds of unbroken sound, it is the agent leaking, not a person.
    push(port, 3, LOUD);
    expect(seen.starts).toBe(1);
    push(port, Math.ceil(VAD.maxUtteranceMs / FRAME_MS), LOUD);

    expect(seen.utterances).toEqual([]);
    expect(seen.misfires).toBe(1);
  });

  it("TC-FE-203: a long question in a noisy room is uploaded rather than thrown away", async () => {
    playing = false;
    const { port } = await open();

    push(port, 3, LOUD);
    push(port, Math.ceil(VAD.maxUtteranceMs / FRAME_MS), LOUD);

    // Nothing is playing, so the sound is the room and whatever was said in it. It goes up.
    expect(seen.utterances).toHaveLength(1);
    expect(seen.misfires).toBe(0);
  });

  it("TC-FE-204: an ordinary barge-in is not disturbed by any of this", async () => {
    playing = true;
    const { port } = await open();

    push(port, 3, LOUD);
    push(port, 20, LOUD);
    push(port, REDEMPTION_FRAMES, QUIET);

    expect(seen.starts).toBe(1);
    expect(seen.utterances).toHaveLength(1);
    expect(seen.misfires).toBe(0);
  });

  it("TC-FE-205: the agent stopping and starting again does not abandon a real question", async () => {
    playing = true;
    const { port } = await open();
    // The agent finishes; the listener starts a question in the quiet that follows.
    playing = false;
    push(port, 3, LOUD);
    push(port, 10, LOUD);
    // The agent does not start again mid-question, so nothing interferes.
    push(port, REDEMPTION_FRAMES, QUIET);

    expect(seen.utterances).toHaveLength(1);
    expect(seen.misfires).toBe(0);
  });
});
