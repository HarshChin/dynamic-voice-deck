import { describe, expect, it } from "vitest";

import {
  AUDIO_HEADER_BYTES,
  CLIENT_MESSAGE_TYPES,
  SERVER_MESSAGE_TYPES,
  decodeAudioFrame,
  parseServerMessage,
} from "./protocol";

/**
 * Build a binary frame byte by byte, the way the backend's `struct.pack("<II", ...)` would.
 *
 * Deliberately not using a helper from `protocol.ts`: a decoder tested against its own encoder
 * cannot catch an endianness mistake, because both sides would be wrong together.
 *
 * @param header - The eight header bytes, most-significant last.
 * @param payload - The PCM16 body bytes.
 * @returns The frame.
 */
function rawFrame(header: readonly number[], payload: readonly number[]): ArrayBuffer {
  return new Uint8Array([...header, ...payload]).buffer;
}

describe("decodeAudioFrame", () => {
  it("TC-FE-030: decodes a hand-built frame with header (7, 3)", () => {
    // 07 00 00 00 | 03 00 00 00 -> little-endian uint32 7 then 3, then two samples.
    const frame = rawFrame([7, 0, 0, 0, 3, 0, 0, 0], [0x01, 0x00, 0xff, 0xff]);

    const decoded = decodeAudioFrame(frame);

    expect(decoded.sentenceId).toBe(7);
    expect(decoded.seq).toBe(3);
    expect(Array.from(decoded.pcm)).toEqual([1, -1]);
  });

  it("TC-FE-100: round-trips multi-byte header fields and full-range samples", () => {
    // seq 258 is 0x0102: read big-endian it would be 33_026, so the byte order is load-bearing.
    const buffer = new ArrayBuffer(AUDIO_HEADER_BYTES + 6);
    const view = new DataView(buffer);
    view.setUint32(0, 0, true);
    view.setUint32(4, 258, true);
    view.setInt16(8, -32768, true);
    view.setInt16(10, 32767, true);
    view.setInt16(12, 0, true);

    const decoded = decodeAudioFrame(buffer);

    expect(decoded.sentenceId).toBe(0);
    expect(decoded.seq).toBe(258);
    expect(Array.from(decoded.pcm)).toEqual([-32768, 32767, 0]);

    // A header with no samples is legal: it is how a sentence with nothing left to send ends.
    expect(decodeAudioFrame(rawFrame([2, 0, 0, 0, 0, 0, 0, 0], [])).pcm).toHaveLength(0);
  });

  it("TC-FE-101: rejects a frame shorter than the header and a truncated sample", () => {
    expect(() => decodeAudioFrame(new ArrayBuffer(AUDIO_HEADER_BYTES - 1))).toThrow(RangeError);
    // A whole header plus a single stray byte: half a sample, so the frame was cut in transit.
    expect(() => decodeAudioFrame(rawFrame([0, 0, 0, 0, 0, 0, 0, 0], [0x01]))).toThrow(RangeError);
  });
});

describe("parseServerMessage", () => {
  it("TC-FE-102: narrows a well-formed message to its discriminated type", () => {
    const message = parseServerMessage(
      '{"type":"state","value":"thinking","turn_id":2,"server_ts":1.5}',
    );

    expect(message).not.toBeNull();
    expect(message?.type).toBe("state");
    // Narrowing, not just a field read: this only compiles because `type` is the discriminator.
    if (message?.type === "state") {
      expect(message.value).toBe("thinking");
      expect(message.turn_id).toBe(2);
    }
  });

  it("TC-FE-103: returns null for an unknown type and for malformed JSON", () => {
    expect(parseServerMessage('{"type":"transcript.alien","turn_id":0}')).toBeNull();
    expect(parseServerMessage("{not json")).toBeNull();
    expect(parseServerMessage("")).toBeNull();
  });

  it("TC-FE-104: returns null for JSON that is not an object with a string type", () => {
    expect(parseServerMessage("null")).toBeNull();
    expect(parseServerMessage("42")).toBeNull();
    expect(parseServerMessage('["state"]')).toBeNull();
    expect(parseServerMessage('{"turn_id":1}')).toBeNull();
    expect(parseServerMessage('{"type":7}')).toBeNull();
  });
});

describe("message type registries", () => {
  it("TC-FE-105: lists every type exactly once for the parity test (TR-143)", () => {
    // The cross-language comparison lives in the backend suite; here we only guarantee the arrays
    // it reads are well-formed, since a duplicate would make a set comparison pass by accident.
    expect(new Set(CLIENT_MESSAGE_TYPES).size).toBe(CLIENT_MESSAGE_TYPES.length);
    expect(new Set(SERVER_MESSAGE_TYPES).size).toBe(SERVER_MESSAGE_TYPES.length);
    expect(CLIENT_MESSAGE_TYPES).toContain("text.input");
    expect(SERVER_MESSAGE_TYPES).toContain("slide.goto");
  });
});
