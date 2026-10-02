// Observe actual speaker samples; this does not estimate which words were heard.
let context: AudioContext | null = null;
let sequence = 0;
export async function trackPetAudio(audio: HTMLAudioElement): Promise<() => void> {
  let source: MediaElementAudioSourceNode | null = null;
  let analyser: AnalyserNode | null = null;
  let frame = 0;
  let stopped = false;
  const token = ++sequence;
  const report = (value: number, active: boolean) => window.dispatchEvent(new CustomEvent("ella-pet-audio", { detail: { value, active } }));
  const stop = () => {
    if (stopped) return;
    stopped = true;
    cancelAnimationFrame(frame);
    try { if (source && analyser) source.disconnect(analyser); } catch { /* analysis is optional */ }
    try { analyser?.disconnect(); } catch { /* analysis is optional */ }
    if (audio.paused || audio.ended) { try { source?.disconnect(); } catch { /* audio owner has finished */ } }
    if (token === sequence) report(0, false);
  };
  try {
    context ??= new AudioContext();
    await Promise.race([context.resume(), new Promise<void>((resolve) => setTimeout(resolve, 400))]);
    if (context.state !== "running") return () => {};
    analyser = context.createAnalyser();
    analyser.fftSize = 512;
    source = context.createMediaElementSource(audio);
    // The speaker stays connected even if analysis or animation fails.
    source.connect(context.destination);
    source.connect(analyser);
    const samples = new Float32Array(analyser.fftSize);
    const tick = () => {
      if (stopped || token !== sequence) return;
      analyser!.getFloatTimeDomainData(samples);
      let energy = 0;
      for (const value of samples) energy += value * value;
      report(audio.paused ? 0 : Math.min(0.85, Math.sqrt(energy / samples.length) * 7), !audio.paused);
      frame = requestAnimationFrame(tick);
    };
    tick();
    return stop;
  } catch {
    // If a source was connected, leave it connected so presentation cannot mute speech.
    return stop;
  }
}
