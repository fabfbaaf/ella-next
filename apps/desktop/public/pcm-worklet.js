class EllaPcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.position = 0;
    this.sum = 0;
    this.count = 0;
    this.buffer = new Int16Array(1600);
    this.offset = 0;
    this.port.onmessage = (event) => {
      if (event.data?.type === "flush") {
        this.emit();
        this.port.postMessage({ type: "flushed", id: event.data.id });
      }
    };
  }

  emit() {
    if (!this.offset) return;
    const packet = this.buffer.slice(0, this.offset);
    this.port.postMessage(packet.buffer, [packet.buffer]);
    this.offset = 0;
  }

  process(inputs, outputs) {
    // The microphone is captured, never monitored through the speakers.
    for (const channel of outputs[0] || []) channel.fill(0);
    const input = inputs[0]?.[0];
    if (!input) return true;
    for (const sample of input) {
      this.sum += sample;
      this.count += 1;
      this.position += 16000 / sampleRate;
      if (this.position < 1) continue;
      const bounded = Math.max(-1, Math.min(1, this.sum / this.count));
      this.sum = 0;
      this.count = 0;
      while (this.position >= 1) {
        this.position -= 1;
        this.buffer[this.offset++] = Math.round(bounded * 32767);
        if (this.offset === this.buffer.length) this.emit();
      }
    }
    return true;
  }
}

registerProcessor("ella-pcm", EllaPcmProcessor);
