// Runs in the AudioWorkletGlobalScope. `sampleRate` here is the sample rate
// of the AudioContext that registered this processor — the capture context
// in app.js is created with `{ sampleRate: 16000 }`, so the browser resamples
// the whole capture graph (including the mic input) to 16kHz before this
// process() callback ever sees a sample. No manual resampling needed.
class CaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const chunkMs = options?.processorOptions?.chunkMs ?? 20;
    this._chunkSamples = Math.round((chunkMs * sampleRate) / 1000);
    this._buffer = [];
    this._bufferedSamples = 0;
  }

  process(inputs) {
    const input = inputs[0];
    const channel = input && input[0];
    if (channel && channel.length > 0) {
      this._buffer.push(channel.slice());
      this._bufferedSamples += channel.length;
      if (this._bufferedSamples >= this._chunkSamples) {
        this._flush();
      }
    }
    return true;
  }

  _flush() {
    const merged = new Float32Array(this._bufferedSamples);
    let offset = 0;
    for (const chunk of this._buffer) {
      merged.set(chunk, offset);
      offset += chunk.length;
    }
    const pcm16 = new Int16Array(merged.length);
    for (let i = 0; i < merged.length; i++) {
      const clamped = Math.max(-1, Math.min(1, merged[i]));
      pcm16[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    }
    this.port.postMessage(pcm16.buffer, [pcm16.buffer]);
    this._buffer = [];
    this._bufferedSamples = 0;
  }
}

registerProcessor('capture-processor', CaptureProcessor);
