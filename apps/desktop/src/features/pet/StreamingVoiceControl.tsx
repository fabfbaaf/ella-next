import { useEffect, useRef, useState } from "react";
import { emit, listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";
import { runtimeFetch, runtimeVoiceSocket } from "../../runtime-client";
import { closeVoiceSocket, mayGrantNotificationLease } from "./voice-notification-lease";
import { trackPetAudio } from "./pet-audio";
import "./voice.css";

type Segment = { index: number; audio_base64: string; media_type: string; text?: string };
type VoiceEvent = { type: string; text?: string; message?: string; index?: number; audio_base64?: string; media_type?: string };
type Phase = "idle" | "recording" | "waiting" | "speaking";
type VoiceStatus = { available: boolean; phase: Phase; message: string; echoCancellation?: boolean; paused?: boolean; notificationActive?: boolean };

export function StreamingVoiceControl() {
  const [available, setAvailable] = useState(false);
  const [phase, setPhase] = useState<Phase>("idle");
  const [message, setMessage] = useState("语音服务状态检查中…");
  const publishedStatus = useRef<VoiceStatus>({ available, phase, message });
  const socket = useRef<WebSocket | null>(null);
  const closingSockets = useRef(new Set<WebSocket>());
  const microphone = useRef<MediaStream | null>(null);
  const audioContext = useRef<AudioContext | null>(null);
  const worklet = useRef<AudioWorkletNode | null>(null);
  const playback = useRef<HTMLAudioElement | null>(null);
  const playbackUrl = useRef<string | null>(null);
  const stopMouth = useRef<() => void>(() => {});
  const queue = useRef<Segment[]>([]);
  const done = useRef(false);
  const mounted = useRef(false);
  const availableRef = useRef(false);
  const phaseRef = useRef<Phase>("idle");
  const generation = useRef(0);
  const toggleRef = useRef<() => void>(() => {});
  const startRef = useRef<() => Promise<void>>(async () => {});
  const continuous = useRef(localStorage.getItem("ella-continuous-listening") !== "false");
  const paused = useRef(false);
  const notificationLease = useRef<string | null>(null);
  const starting = useRef(false);
  const bargeIn = useRef(localStorage.getItem("ella-voice-barge-in") === "true");
  const echoCancellation = useRef(false);
  const echoReference = useRef("");
  const playingText = useRef("");
  const interrupting = useRef(false);
  const retryAttempt = useRef(0);
  const resumeTimer = useRef<number | null>(null);
  const preRoll = useRef<ArrayBuffer[]>([]);
  const vad = useRef({ voicedMs: 0, quietMs: 0, speechStarted: false, streamingStarted: false, elapsedMs: 0, finishing: false });

  publishedStatus.current = { available, phase, message, echoCancellation: echoCancellation.current, paused: paused.current, notificationActive: notificationLease.current !== null };

  const updatePhase = (value: Phase) => {
    phaseRef.current = value;
    if (mounted.current) setPhase(value);
  };

  const publishVoiceStatus = () => {
    const status: VoiceStatus = {
      ...publishedStatus.current, available: availableRef.current, phase: phaseRef.current,
      paused: paused.current, notificationActive: notificationLease.current !== null,
    };
    publishedStatus.current = status;
    window.dispatchEvent(new CustomEvent("ella-voice-status", { detail: status }));
    if ("__TAURI_INTERNALS__" in window) void emit("ella-voice-status", status);
  };

  const clearResume = () => {
    if (resumeTimer.current !== null) window.clearTimeout(resumeTimer.current);
    resumeTimer.current = null;
  };

  const scheduleResume = (delay?: number) => {
    if (!mounted.current || !availableRef.current || !continuous.current || paused.current || starting.current || notificationLease.current !== null || resumeTimer.current !== null) return;
    const wait = delay ?? Math.min(1000 * 2 ** retryAttempt.current++, 15000);
    resumeTimer.current = window.setTimeout(() => {
      resumeTimer.current = null;
      if (mounted.current && availableRef.current && continuous.current && !paused.current && !starting.current && notificationLease.current === null) void startRef.current();
    }, wait);
  };

  const stopCapture = async () => {
    const node = worklet.current; worklet.current = null;
    const media = microphone.current; microphone.current = null;
    try { media?.getTracks().forEach((track) => { try { track.stop(); } catch { /* continue stopping other tracks */ } }); } catch { /* device may already be released */ }
    try { node?.disconnect(); } catch { /* capture is stopped even if the graph is closed */ }
    const context = audioContext.current;
    audioContext.current = null;
    if (context && context.state !== "closed") { try { await context.close(); } catch { /* tracks are already stopped */ } }
  };

  const stopAll = async (keepCapture = false) => {
    generation.current += 1;
    clearResume();
    if (playback.current) {
      playback.current.onended = null;
      playback.current.onerror = null;
      try { playback.current.pause(); } catch { /* keep releasing microphone and socket */ }
    }
    playback.current = null;
    try { stopMouth.current(); } catch { /* presentation cannot block capture cleanup */ }
    if (playbackUrl.current) URL.revokeObjectURL(playbackUrl.current);
    playbackUrl.current = null;
    queue.current = [];
    done.current = false;
    const ws = socket.current;
    socket.current = null;
    if (ws?.readyState === WebSocket.OPEN) { try { ws.send(JSON.stringify({ type: "interrupt" })); } catch { /* socket closed between readyState and send */ } }
    if (ws) closingSockets.current.add(ws);
    const closed = Promise.all([...closingSockets.current].map(async (pending) => {
      const finished = await closeVoiceSocket(pending);
      if (finished) closingSockets.current.delete(pending);
      return finished;
    })).then((results) => results.every(Boolean));
    updatePhase("idle");
    if (!keepCapture) await stopCapture();
    return closed;
  };

  useEffect(() => {
    mounted.current = true;
    let inFlight = false;
    const refresh = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const response = await runtimeFetch("http://127.0.0.1:8766/api/voice/status");
        if (!response.ok) throw new Error("运行时未连接");
        const status = await response.json();
        if (!mounted.current) return;
        const ready = Boolean(status.can_transcribe);
        availableRef.current = ready;
        setAvailable(ready);
        if (!ready) setMessage("请先配置语音识别模型");
      } catch {
        if (!mounted.current) return;
        availableRef.current = false;
        setAvailable(false);
        setMessage("本地运行时尚未就绪，正在等待连接");
      } finally { inFlight = false; }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 10000);
    return () => { mounted.current = false; window.clearInterval(timer); void stopAll(); };
  }, []);

  const playNext = async (ws: WebSocket, current: number) => {
    if (current !== generation.current || playback.current) return;
    const segment = queue.current.shift();
    if (!segment) {
      if (done.current) {
        updatePhase("idle");
        if (paused.current) {
          setMessage("监听已暂停，点击人物或在后台恢复");
          void stopAll().then(publishVoiceStatus);
        } else {
          setMessage("聆听中");
          scheduleResume(400);
        }
      }
      return;
    }
    const bytes = Uint8Array.from(atob(segment.audio_base64), (char) => char.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: segment.media_type }));
    const audio = new Audio(url);
    playback.current = audio;
    playingText.current = segment.text || "";
    playbackUrl.current = url;
    updatePhase("speaking");
    audio.onended = () => {
      if (current !== generation.current) return;
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ack", index: segment.index }));
      playback.current = null;
      stopMouth.current();
      playbackUrl.current = null;
      URL.revokeObjectURL(url);
      void playNext(ws, current);
    };
    audio.onerror = () => {
      if (current !== generation.current) return;
      setMessage("音频播放失败，正在恢复监听");
      void stopAll().then(() => scheduleResume());
    };
    try {
      const stop = await trackPetAudio(audio);
      if (current !== generation.current) { stop(); return; }
      stopMouth.current = stop;
      await audio.play();
    }
    catch {
      if (current !== generation.current) return;
      paused.current = true;
      setMessage("浏览器阻止了自动播放，请检查音频权限");
      void stopAll();
    }
  };

  const start = async (keepCapture = false, seed: ArrayBuffer[] = []) => {
    if (starting.current || notificationLease.current !== null) return;
    if (!mounted.current || !availableRef.current || !navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
      if (mounted.current) setMessage("麦克风或流式语音服务不可用");
      return;
    }
    starting.current = true;
    let current = generation.current;
    try {
      const reference = keepCapture ? playingText.current.slice(-500) : "";
      const closed = await stopAll(keepCapture);
      echoReference.current = reference;
      if (!mounted.current || paused.current || notificationLease.current !== null || !availableRef.current) return;
      if (!closed) throw new Error("上一轮语音连接尚未关闭，正在等待恢复");
      current = ++generation.current;
      setMessage("正在连接语音服务…");
      vad.current = { voicedMs: 0, quietMs: 0, speechStarted: false, streamingStarted: false, elapsedMs: 0, finishing: false };
      preRoll.current = [];
      const ws = await runtimeVoiceSocket();
      if (current !== generation.current) { ws.close(); return; }
      socket.current = ws;
      await new Promise<void>((resolve, reject) => {
        const timeout = window.setTimeout(() => { ws.close(); reject(new Error("语音连接超时")); }, 10000);
        const cleanup = () => {
          window.clearTimeout(timeout);
          ws.removeEventListener("open", opened);
          ws.removeEventListener("error", failed);
          ws.removeEventListener("close", failed);
        };
        const opened = () => { cleanup(); resolve(); };
        const failed = () => { cleanup(); reject(new Error("无法连接流式语音服务")); };
        ws.addEventListener("open", opened);
        ws.addEventListener("error", failed);
        ws.addEventListener("close", failed);
      });
      if (current !== generation.current) { ws.close(); return; }
      ws.onmessage = (event) => {
        if (current !== generation.current) return;
        let item: VoiceEvent;
        try { item = JSON.parse(event.data) as VoiceEvent; }
        catch { setMessage("语音响应格式无效"); void stopAll().then(() => scheduleResume()); return; }
        if (!item || typeof item.type !== "string") return;
        if (item.type === "transcript") setMessage("正在回复…");
        if (item.type === "listening_pause") {
          paused.current = true;
          clearResume();
          void stopCapture();
          setMessage("正在暂停监听…");
          publishVoiceStatus();
        }
        if (item.type === "audio_segment" && typeof item.index === "number" && item.audio_base64 && item.media_type) {
          if (queue.current.length >= 6) { setMessage("待播放音频过多，正在恢复监听"); void stopAll().then(() => scheduleResume()); return; }
          queue.current.push(item as Segment);
          void playNext(ws, current);
        }
        if (item.type === "reply_done") { done.current = true; if (!playback.current) void playNext(ws, current); }
        if (item.type === "audio_echo") { void stopAll().then(() => scheduleResume(400)); }
        if (item.type === "speech_error") setMessage("合成语音失败，请在管理后台检查语音设置");
        if (item.type === "error" || item.type === "interrupted") {
          setMessage(item.message || (item.type === "interrupted" ? "语音已被另一轮对话打断" : "语音请求失败"));
          void stopAll().then(() => scheduleResume());
        }
      };
      ws.onclose = () => {
        if (current !== generation.current) return;
        setMessage("语音连接已断开，正在重新连接");
        void stopAll().then(() => scheduleResume());
      };
      const media = microphone.current || await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      if (current !== generation.current) { media.getTracks().forEach((track) => track.stop()); return; }
      microphone.current = media;
      echoCancellation.current = media.getAudioTracks()[0]?.getSettings().echoCancellation === true;
      const context = audioContext.current || new AudioContext();
      audioContext.current = context;
      if (!worklet.current) await context.audioWorklet.addModule("/pcm-worklet.js");
      if (current !== generation.current) return;
      const existing = worklet.current;
      const node = existing || new AudioWorkletNode(context, "ella-pcm");
      worklet.current = node;
      if (!existing) { context.createMediaStreamSource(media).connect(node); node.connect(context.destination); }
      if (seed.length) {
        for (const chunk of seed) ws.send(chunk);
        vad.current.speechStarted = true;
        vad.current.streamingStarted = true;
        vad.current.voicedMs = 300;
      }
      interrupting.current = false;
      node.port.onmessage = (event) => {
        if (current !== generation.current || !(event.data instanceof ArrayBuffer)) return;
        if (phaseRef.current !== "recording") {
          if (!continuous.current || !bargeIn.current || interrupting.current || (phaseRef.current === "speaking" && !echoCancellation.current)) return;
          const samples = new Int16Array(event.data);
          let energy = 0;
          for (const sample of samples) energy += sample * sample;
          const duration = samples.length / 16;
          preRoll.current.push(event.data);
          while (preRoll.current.reduce((sum, chunk) => sum + chunk.byteLength / 32, 0) > 600 && preRoll.current.length > 1) preRoll.current.shift();
          const activity = vad.current;
          activity.voicedMs = Math.sqrt(energy / samples.length) / 32768 > 0.024 ? activity.voicedMs + duration : 0;
          if (activity.voicedMs >= 300) {
            interrupting.current = true;
            void start(true, preRoll.current.slice());
          }
          return;
        }
        if (!continuous.current || vad.current.finishing) {
          if (ws.readyState === WebSocket.OPEN) ws.send(event.data);
          return;
        }
        const samples = new Int16Array(event.data);
        let energy = 0;
        for (const sample of samples) energy += sample * sample;
        const rms = Math.sqrt(energy / samples.length) / 32768;
        const durationMs = samples.length / 16;
        const activity = vad.current;
        if (rms > 0.014) {
          activity.voicedMs += durationMs;
          activity.quietMs = 0;
          if (activity.voicedMs >= 200) activity.speechStarted = true;
        } else if (activity.speechStarted) {
          activity.quietMs += durationMs;
        } else {
          activity.voicedMs = 0;
        }
        if (!activity.streamingStarted) {
          preRoll.current.push(event.data);
          if (preRoll.current.length > 5) preRoll.current.shift();
          if (!activity.speechStarted) return;
          if (ws.readyState === WebSocket.OPEN) for (const chunk of preRoll.current) ws.send(chunk);
          preRoll.current = [];
          activity.streamingStarted = true;
        } else if (ws.readyState === WebSocket.OPEN) ws.send(event.data);
        if (!activity.speechStarted) return;
        activity.elapsedMs += durationMs;
        if (activity.quietMs >= 900 || activity.elapsedMs >= 18000) void finish();
      };

      retryAttempt.current = 0;
      updatePhase("recording");
      setMessage("正在聆听");
    } catch (error) {
      if (current !== generation.current || !mounted.current) return;
      if (error instanceof DOMException && ["NotAllowedError", "NotFoundError", "SecurityError"].includes(error.name)) paused.current = true;
      setMessage(error instanceof Error ? error.message : "无法开始录音");
      await stopAll();
    } finally {
      starting.current = false;
      if (mounted.current && phaseRef.current === "idle") scheduleResume();
    }
  };
  startRef.current = start;

  useEffect(() => {
    if (!available) { clearResume(); return; }
    const sync = () => {
      const enabled = localStorage.getItem("ella-continuous-listening") !== "false";
      const allowBarge = localStorage.getItem("ella-voice-barge-in") === "true";
      if (bargeIn.current && !allowBarge && phaseRef.current !== "recording") void stopCapture();
      bargeIn.current = allowBarge;
      if (continuous.current !== enabled) paused.current = false;
      continuous.current = enabled;
      if (enabled && !paused.current && phaseRef.current === "idle") void startRef.current();
      if (!enabled) {
        clearResume();
        if (phaseRef.current === "recording") void stopAll();
      }
    };
    sync();
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, [available]);

  const finish = async () => {
    if (phaseRef.current !== "recording") return;
    const current = generation.current;
    vad.current.finishing = true;
    updatePhase("waiting");
    setMessage("正在识别和回复…");
    const node = worklet.current;
    if (node) {
      await new Promise<void>((resolve) => {
        const timeout = window.setTimeout(resolve, 500);
        const prior = node.port.onmessage;
        node.port.onmessage = (event) => {
          prior?.call(node.port, event);
          if (event.data?.type === "flushed") { window.clearTimeout(timeout); resolve(); }
        };
        node.port.postMessage({ type: "flush" });
      });
    }
    if (current !== generation.current) return;
    if (!continuous.current || !bargeIn.current) await stopCapture();
    vad.current.voicedMs = 0;
    preRoll.current = [];
    if (current !== generation.current) return;
    if (socket.current?.readyState === WebSocket.OPEN) socket.current.send(JSON.stringify({ type: "finish", echo_reference: echoReference.current }));
  };

  toggleRef.current = () => {
    if (notificationLease.current !== null) {
      notificationLease.current = null;
      window.dispatchEvent(new Event("ella-notification-interrupt"));
      if (!availableRef.current) {
        paused.current = true;
        clearResume();
        setMessage("通知播报已暂停，点击人物可恢复");
        publishVoiceStatus();
        return;
      }
    }
    if (!availableRef.current) {
      // A deliberate second interaction resumes notification speech even
      // when recognition has not been configured yet.
      paused.current = false;
      publishVoiceStatus();
      if ("__TAURI_INTERNALS__" in window) void invoke("show_admin");
      else window.open("/?window=admin", "_blank");
    } else if (phaseRef.current === "recording") void finish();
    else if (phaseRef.current === "idle") { paused.current = false; retryAttempt.current = 0; void start(); }
    else { paused.current = true; void stopAll(); publishVoiceStatus(); }
  };
  useEffect(() => {
    const toggle = () => toggleRef.current();
    const control = (event: Event) => {
      const action = (event as CustomEvent<{ action?: string }>).detail?.action;
      if (action === "pause") { paused.current = true; setMessage("监听已暂停，点击人物可恢复"); void stopAll().then(publishVoiceStatus); }
      if (action === "resume") { paused.current = false; retryAttempt.current = 0; void startRef.current(); publishVoiceStatus(); }
    };
    window.addEventListener("ella-toggle-voice", toggle);
    window.addEventListener("ella-listening-control", control);
    return () => { window.removeEventListener("ella-toggle-voice", toggle); window.removeEventListener("ella-listening-control", control); };
  }, []);
  useEffect(() => {
    if (!("__TAURI_INTERNALS__" in window)) return;
    let active = true;
    let unlisten: (() => void) | null = null;
    let stopControl: (() => void) | null = null;
    void listen<{ action: string }>("ella-listening-control", (event) => window.dispatchEvent(new CustomEvent("ella-listening-control", { detail: event.payload }))).then((stop) => { if (active) stopControl = stop; else stop(); });
    void listen("ella-toggle-voice", () => toggleRef.current()).then((stop) => {
      if (active) unlisten = stop;
      else stop();
    });
    return () => { active = false; unlisten?.(); stopControl?.(); };
  }, []);

  useEffect(() => {
    window.dispatchEvent(new CustomEvent("ella-voice-status", { detail: publishedStatus.current }));
    if ("__TAURI_INTERNALS__" in window) void emit("ella-voice-status", publishedStatus.current);
  }, [available, phase, message]);
  useEffect(() => {
    const publish = publishVoiceStatus;
    const pause = (event: Event) => {
      const requestId = (event as CustomEvent<{ requestId?: string }>).detail?.requestId;
      if (!requestId) return;
      const granted = (value: boolean) => window.dispatchEvent(new CustomEvent("ella-notification-granted", { detail: { requestId, granted: value } }));
      if (!mayGrantNotificationLease({
        mounted: mounted.current, paused: paused.current, starting: starting.current,
        hasLease: notificationLease.current !== null, phase: phaseRef.current,
        continuous: continuous.current, speechStarted: vad.current.speechStarted,
        voicedMs: vad.current.voicedMs,
      })) {
        granted(false); return;
      }
      notificationLease.current = requestId;
      void stopAll().then((closed) => {
        if (!closed || !mounted.current || paused.current || starting.current || notificationLease.current !== requestId) {
          granted(false);
          if (notificationLease.current === requestId) notificationLease.current = null;
          scheduleResume(400);
          return;
        }
        publish();
        granted(true);
      }).catch(() => {
        if (notificationLease.current === requestId) notificationLease.current = null;
        granted(false);
        publish();
        scheduleResume(400);
      });
    };
    const resume = (event: Event) => {
      const requestId = (event as CustomEvent<{ requestId?: string }>).detail?.requestId;
      if (!requestId || notificationLease.current !== requestId) return;
      notificationLease.current = null;
      publish();
      scheduleResume(400);
    };
    window.addEventListener("ella-notification-pause", pause);
    window.addEventListener("ella-notification-resume", resume);
    window.addEventListener("ella-voice-status-request", publish);
    return () => {
      notificationLease.current = null;
      window.removeEventListener("ella-notification-pause", pause);
      window.removeEventListener("ella-notification-resume", resume);
      window.removeEventListener("ella-voice-status-request", publish);
    };
  }, []);
  useEffect(() => {
    if (!("__TAURI_INTERNALS__" in window)) return;
    let active = true;
    let unlisten: (() => void) | null = null;
    void listen("ella-voice-status-request", publishVoiceStatus).then((stop) => {
      if (active) unlisten = stop;
      else stop();
    });
    return () => { active = false; unlisten?.(); };
  }, []);

  const label = !available ? "语音尚未配置，打开管理后台" :
    phase === "recording" ? "结束录音并让艾拉回复" :
    phase === "idle" ? "开始语音对话" : "打断艾拉";
  return <section className="voice-control" data-phase={phase} aria-label="流式语音对话">
    <p className="sr-only" aria-live="polite">{message}</p>
    <button type="button" aria-label={label} onClick={() => toggleRef.current()}>
      {phase === "recording" ? "●" : phase === "speaking" ? "♫" : phase === "waiting" ? "…" : "🎙"}
    </button>
  </section>;
}
