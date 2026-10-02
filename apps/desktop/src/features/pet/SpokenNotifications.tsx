import { useEffect, useRef } from "react";
import { emit, listen } from "@tauri-apps/api/event";
import { runtimeFetch } from "../../runtime-client";
import { NotificationSpeechDeferredError, NotificationSpeechQueue, type PreparedSpeech, type SpokenNotification } from "./notification-speech";

type VoiceState = { phase: "idle" | "recording" | "waiting" | "speaking"; paused?: boolean };
const completedKey = "ella-spoken-notification-completions";
const runtime = "http://127.0.0.1:8766";

function preferenceEnabled(): boolean {
  try { return localStorage.getItem("ella-spoken-notifications") !== "false"; }
  catch { return true; }
}

function loadCompleted(): string[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(completedKey) || "[]");
    return Array.isArray(value) ? value.filter((id): id is string => typeof id === "string").slice(-200) : [];
  } catch { return []; }
}

function release(requestId: string): void {
  window.dispatchEvent(new CustomEvent("ella-notification-resume", { detail: { requestId } }));
}

function acquire(requestId: string, signal: AbortSignal): Promise<boolean> {
  return new Promise((resolve) => {
    if (signal.aborted) { resolve(false); return; }
    let settled = false;
    const finish = (granted: boolean) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      signal.removeEventListener("abort", canceled);
      window.removeEventListener("ella-notification-granted", received);
      if (!granted) release(requestId);
      resolve(granted);
    };
    const received = (event: Event) => {
      const payload = (event as CustomEvent<{ requestId?: string; granted?: boolean }>).detail;
      if (payload?.requestId === requestId) finish(payload.granted === true);
    };
    const canceled = () => finish(false);
    const timer = window.setTimeout(() => finish(false), 8000);
    signal.addEventListener("abort", canceled, { once: true });
    window.addEventListener("ella-notification-granted", received);
    window.dispatchEvent(new CustomEvent("ella-notification-pause", { detail: { requestId } }));
  });
}

async function responseError(response: Response, fallback: string): Promise<Error> {
  const data = await response.json().catch(() => ({})) as { detail?: unknown };
  const message = typeof data.detail === "string" ? data.detail : `${fallback}（HTTP ${response.status}）`;
  return response.status === 409 ? new NotificationSpeechDeferredError(message) : new Error(message);
}

function play(speech: PreparedSpeech, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new DOMException("已停止播报", "AbortError")); return; }
    if (!speech.audio_base64 || speech.audio_base64.length > 16_000_000 || !/^audio\/[\w.+-]+$/.test(speech.media_type)) {
      reject(new Error("通知音频格式无效")); return;
    }
    let bytes: Uint8Array;
    try { bytes = Uint8Array.from(atob(speech.audio_base64), (char) => char.charCodeAt(0)); }
    catch { reject(new Error("通知音频无法解码")); return; }
    const buffer = new ArrayBuffer(bytes.byteLength);
    new Uint8Array(buffer).set(bytes);
    const url = URL.createObjectURL(new Blob([buffer], { type: speech.media_type }));
    const audio = new Audio(url);
    let settled = false;
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      audio.onended = null;
      audio.onerror = null;
      audio.pause();
      signal.removeEventListener("abort", canceled);
      URL.revokeObjectURL(url);
      if (error) reject(error); else resolve();
    };
    const canceled = () => finish(new DOMException("已停止播报", "AbortError"));
    const timer = window.setTimeout(() => finish(new Error("通知语音播放超时")), 120_000);
    audio.onended = () => finish();
    audio.onerror = () => finish(new Error("通知音频播放失败，请在后台检查语音配置"));
    signal.addEventListener("abort", canceled, { once: true });
    void audio.play().catch((reason: unknown) => finish(new Error(reason instanceof Error ? reason.message : "通知音频播放被阻止")));
  });
}

export function useSpokenNotifications(items: SpokenNotification[], quietNow: boolean): void {
  const queue = useRef<NotificationSpeechQueue | null>(null);
  const records = useRef(items);
  const quiet = useRef(quietNow);
  const voice = useRef<VoiceState | null>(null);
  records.current = items;
  quiet.current = quietNow;

  useEffect(() => {
    let active = true;
    let unlisten: (() => void) | null = null;
    const allowed = () => active && preferenceEnabled() && !quiet.current && voice.current !== null
      && !voice.current.paused && !["waiting", "speaking"].includes(voice.current.phase);
    const speech = new NotificationSpeechQueue({
      allowed, acquire, release, play,
      synthesize: async (item, signal) => {
        const response = await runtimeFetch(`${runtime}/api/companion/notifications/${encodeURIComponent(item.id)}/speech`, { method: "POST", signal });
        if (!response.ok) throw await responseError(response, "通知语音合成失败");
        return response.json() as Promise<PreparedSpeech>;
      },
      report: async (id, status, error) => {
        const response = await runtimeFetch(`${runtime}/api/companion/notifications/${encodeURIComponent(id)}/spoken`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status, ...(error ? { error } : {}) }), signal: AbortSignal.timeout(8000),
        });
        if (!response.ok) throw await responseError(response, "通知播报状态保存失败");
      },
      rememberCompleted: (ids) => { try { localStorage.setItem(completedKey, JSON.stringify(ids)); } catch { /* Backend acknowledgements still persist. */ } },
    }, loadCompleted());
    queue.current = speech;
    speech.update(records.current);
    const pump = () => {
      if (!allowed()) speech.interrupt();
      else void speech.pump();
    };
    const updateVoice = (value: VoiceState) => {
      if (!active || !value || !["idle", "recording", "waiting", "speaking"].includes(value.phase)) return;
      voice.current = value;
      pump();
    };
    const localVoice = (event: Event) => updateVoice((event as CustomEvent<VoiceState>).detail);
    const interrupt = () => speech.interrupt();
    window.addEventListener("ella-voice-status", localVoice);
    window.addEventListener("ella-notification-interrupt", interrupt);
    window.addEventListener("storage", pump);
    window.dispatchEvent(new Event("ella-voice-status-request"));
    if ("__TAURI_INTERNALS__" in window) {
      void listen<VoiceState>("ella-voice-status", (event) => updateVoice(event.payload)).then((stop) => {
        if (active) { unlisten = stop; void emit("ella-voice-status-request"); }
        else stop();
      }).catch(() => { /* Same-window status events remain available. */ });
    }
    const timer = window.setInterval(pump, 3000);
    return () => {
      active = false;
      speech.dispose();
      if (queue.current === speech) queue.current = null;
      window.clearInterval(timer);
      window.removeEventListener("ella-voice-status", localVoice);
      window.removeEventListener("ella-notification-interrupt", interrupt);
      window.removeEventListener("storage", pump);
      unlisten?.();
    };
  }, []);

  useEffect(() => {
    queue.current?.update(items);
    if (quietNow || !preferenceEnabled()) queue.current?.interrupt();
    else void queue.current?.pump();
  }, [items, quietNow]);
}
