export const PCM_BYTES_PER_MS = 32;
export const PRE_ROLL_MS = 600;
export const MAX_PENDING_PCM_BYTES = PCM_BYTES_PER_MS * 20000;

export class PcmBuffer {
  private packets: ArrayBuffer[] = [];
  private bytes = 0;
  constructor(readonly maxBytes: number, readonly dropOldest = false) {}
  get byteLength() { return this.bytes; }
  append(packet: ArrayBuffer) {
    if (!packet.byteLength) return;
    if (packet.byteLength > this.maxBytes || (!this.dropOldest && this.bytes + packet.byteLength > this.maxBytes)) {
      throw new Error("语音缓冲已满，请等连接恢复后再说一次");
    }
    this.packets.push(packet);
    this.bytes += packet.byteLength;
    while (this.bytes > this.maxBytes) this.bytes -= this.packets.shift()!.byteLength;
  }
  drain() { const result = this.packets; this.packets = []; this.bytes = 0; return result; }
}

export function pcmRms(packet: ArrayBuffer) {
  const samples = new Int16Array(packet);
  let energy = 0;
  for (const sample of samples) energy += sample * sample;
  return samples.length ? Math.sqrt(energy / samples.length) / 32768 : 0;
}

type GateResult = { started: boolean; finished: boolean; packets: ArrayBuffer[] };
export class SpeechGate {
  private preRoll = new PcmBuffer(PRE_ROLL_MS * PCM_BYTES_PER_MS, true);
  private voicedMs = 0;
  private quietMs = 0;
  private elapsedMs = 0;
  private speech = false;
  private ended = false;
  reset(started = false, elapsedMs = 0) {
    this.preRoll.drain(); this.voicedMs = 0; this.quietMs = 0;
    this.elapsedMs = elapsedMs; this.speech = started; this.ended = false;
  }
  push(packet: ArrayBuffer, { enabled = true, threshold = 0.014, speechStartMs = 200 } = {}): GateResult {
    if (this.ended) return { started: false, finished: false, packets: [] };
    const durationMs = packet.byteLength / PCM_BYTES_PER_MS;
    if (!this.speech) this.preRoll.append(packet);
    if (!enabled) { this.voicedMs = 0; return { started: false, finished: false, packets: [] }; }
    const voiced = pcmRms(packet) > threshold;
    if (voiced) { this.voicedMs += durationMs; this.quietMs = 0; }
    else if (this.speech) this.quietMs += durationMs;
    else this.voicedMs = 0;
    const started = !this.speech && this.voicedMs >= speechStartMs;
    if (started) { this.speech = true; this.elapsedMs = this.voicedMs; }
    if (!this.speech) return { started: false, finished: false, packets: [] };
    if (!started) this.elapsedMs += durationMs;
    const packets = started ? this.preRoll.drain() : [packet];
    const finished = this.quietMs >= 900 || this.elapsedMs >= 18000;
    if (finished) this.ended = true;
    return { started, finished, packets };
  }
}

export function canDetectBargeIn(continuous: boolean, enabled: boolean, playing: boolean, echoCancellation: boolean | null) {
  return continuous && enabled && (!playing || echoCancellation === true);
}
