/**
 * Microphone capture worklet: downmix, resample to 16 kHz, and report loudness.
 *
 * Runs on the audio thread, so it is never delayed by rendering or by the model stream. It does the
 * mechanical work only -- the decision about when speech starts and stops is made on the main thread,
 * where it can be tested without an audio device.
 *
 * Served from `public/` and loaded with `addModule()`, which is how the Web Audio API loads worklets.
 * It is deliberately plain JavaScript with no imports: a worklet has no module graph, and this file
 * must not go through the bundler.
 */

/** Frames handed to the main thread, in samples at 16 kHz. 512 samples is 32 ms. */
const FRAME_SAMPLES = 512;

/** Target rate. Whisper wants 16 kHz, and sending anything higher is wasted bandwidth. */
const TARGET_RATE = 16000;

class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buffer = new Float32Array(FRAME_SAMPLES);
    this._filled = 0;
    // Fractional read position into the input block, advanced by the resampling ratio.
    this._position = 0;
  }

  /**
   * Consume one render quantum of microphone audio.
   *
   * @param {Float32Array[][]} inputs - Input channels.
   * @returns {boolean} True to stay alive.
   */
  process(inputs) {
    const channels = inputs[0];
    if (!channels || channels.length === 0 || !channels[0]) {
      return true;
    }

    // Downmix to mono. A laptop microphone is usually mono already, but a headset may not be.
    const first = channels[0];
    const blockLength = first.length;
    const mono = new Float32Array(blockLength);
    for (let c = 0; c < channels.length; c += 1) {
      const channel = channels[c];
      for (let i = 0; i < blockLength; i += 1) {
        mono[i] += (channel[i] || 0) / channels.length;
      }
    }

    // Linear resample to 16 kHz. Linear rather than a windowed filter because the consumer is a
    // speech model, not a listener: the aliasing this leaves is inaudible to Whisper and the
    // arithmetic is cheap enough to run on the audio thread without risking a dropout.
    const ratio = sampleRate / TARGET_RATE;
    while (this._position < blockLength) {
      const index = Math.floor(this._position);
      const next = Math.min(index + 1, blockLength - 1);
      const fraction = this._position - index;
      const sample = (mono[index] || 0) * (1 - fraction) + (mono[next] || 0) * fraction;

      this._buffer[this._filled] = sample;
      this._filled += 1;

      if (this._filled === FRAME_SAMPLES) {
        let sum = 0;
        for (let i = 0; i < FRAME_SAMPLES; i += 1) {
          sum += this._buffer[i] * this._buffer[i];
        }
        // Transfer the buffer rather than copying it: this runs 31 times a second for the life of
        // the session, and the main thread only reads it.
        const frame = this._buffer;
        this._buffer = new Float32Array(FRAME_SAMPLES);
        this._filled = 0;
        this.port.postMessage({ frame, rms: Math.sqrt(sum / FRAME_SAMPLES) }, [frame.buffer]);
      }
      this._position += ratio;
    }
    this._position -= blockLength;
    return true;
  }
}

registerProcessor("capture-processor", CaptureProcessor);
