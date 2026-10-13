"use strict";

// Runs on the audio thread: hands the microphone's samples to the page in small batches,
// with their loudness so the page can tell speech from silence.
class Recorder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.batch = new Float32Array(2048);
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) {
      let offset = 0;
      while (offset < channel.length) {
        const n = Math.min(channel.length - offset, this.batch.length - this.filled);
        this.batch.set(channel.subarray(offset, offset + n), this.filled);
        this.filled += n;
        offset += n;
        if (this.filled === this.batch.length) {
          let sum = 0;
          for (const s of this.batch) sum += s * s;
          this.port.postMessage({ samples: this.batch, rms: Math.sqrt(sum / this.batch.length) });
          this.batch = new Float32Array(2048);
          this.filled = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("recorder", Recorder);
