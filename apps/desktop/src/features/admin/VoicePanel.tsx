import { useEffect, useRef, useState } from "react";
import { emit, listen } from "@tauri-apps/api/event";
import { runtimeFetch } from "../../runtime-client";
import "./model-status.css";

type VoiceStatus = {
  provider: string; transcription_model: string; speech_model: string; voice: string;
  speech_provider?: string;
  can_transcribe: boolean; can_speak: boolean; state: string;
};
type VoiceSlot = "asr" | "tts";
type ServiceConfig = {
  provider: "openai" | "fish"; base_url: string; model: string; voice: string;
  configured: boolean; key_saved: boolean; key_present: boolean; source: "saved" | "environment";
};
type Configs = Record<VoiceSlot, ServiceConfig>;
type Form = { provider: "openai" | "fish"; base_url: string; model: string; voice: string; api_key: string };
type LiveVoiceState = { available: boolean; phase: "idle" | "recording" | "waiting" | "speaking"; message: string; echoCancellation?: boolean };
const runtime = "http://127.0.0.1:8766";
const phaseLabels = { idle: "未监听", recording: "正在聆听", waiting: "识别与思考中", speaking: "正在播报" };
const emptyForm: Form = { provider: "openai", base_url: "https://api.openai.com/v1", model: "", voice: "", api_key: "" };
const asForm = (value: ServiceConfig): Form => ({ provider: value.provider, base_url: value.base_url, model: value.model, voice: value.voice, api_key: "" });
const serviceOrigin = (value: string) => { try { return new URL(value).origin; } catch { return null; } };
function errorMessage(data: unknown, fallback: string): string {
  if (!data || typeof data !== "object") return fallback;
  const detail = (data as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item: { loc?: unknown[]; msg?: string }) => `${item.loc?.slice(1).join(".") || "配置"}：${item.msg || "字段无效"}`).join("；") || fallback;
  }
  return fallback;
}

export function VoicePanel() {
  const previewAudio = useRef<HTMLAudioElement | null>(null);
  const previewUrl = useRef<string | null>(null);
  const stopPreview = useRef<() => void>(() => {});
  const [live, setLive] = useState<LiveVoiceState | null>(null);
  const [status, setStatus] = useState<VoiceStatus | null>(null);
  const [configs, setConfigs] = useState<Configs | null>(null);
  const [forms, setForms] = useState<Record<VoiceSlot, Form>>({ asr: { ...emptyForm }, tts: { ...emptyForm } });
  const [slot, setSlot] = useState<VoiceSlot>("asr");
  const [clearKeys, setClearKeys] = useState<Record<VoiceSlot, boolean>>({ asr: false, tts: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [statusError, setStatusError] = useState("");
  const [notice, setNotice] = useState("");
  const [bargeIn, setBargeIn] = useState(() => localStorage.getItem("ella-voice-barge-in") === "true");
  const [continuous, setContinuous] = useState(() => localStorage.getItem("ella-continuous-listening") !== "false");
  const [spokenNotifications, setSpokenNotifications] = useState(() => localStorage.getItem("ella-spoken-notifications") !== "false");
  useEffect(() => {
    let active = true;
    let inFlight = false;
    let unlisten: (() => void) | null = null;
    const refresh = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const response = await runtimeFetch(`${runtime}/api/voice/status`);
        const value = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(errorMessage(value, "语音状态读取失败"));
        if (active) { setStatus(value as VoiceStatus); setStatusError(""); }
        if ("__TAURI_INTERNALS__" in window) await emit("ella-voice-status-request");
      } catch (reason) {
        if (active) { setStatus(null); setStatusError(reason instanceof Error ? reason.message : "状态读取失败"); }
      } finally { inFlight = false; }
    };
    const loadConfig = async () => {
      try {
        const response = await runtimeFetch(`${runtime}/api/voice/config`);
        const value = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(errorMessage(value, "语音配置读取失败"));
        const current = value as Configs;
        if (active) {
          setConfigs(current);
          setForms({ asr: asForm(current.asr), tts: asForm(current.tts) });
        }
      } catch (reason) {
        if (active) setError(reason instanceof Error ? reason.message : "配置读取失败");
      }
    };
    if ("__TAURI_INTERNALS__" in window) {
      void listen<LiveVoiceState>("ella-voice-status", (event) => {
        if (active) setLive(event.payload);
      }).then((stop) => {
        if (active) { unlisten = stop; void emit("ella-voice-status-request"); }
        else stop();
      });
    }
    void loadConfig();
    void refresh();
    const timer = window.setInterval(() => void refresh(), 10000);
    return () => { active = false; window.clearInterval(timer); unlisten?.(); };
  }, []);
  const form = forms[slot];
  const clearKey = clearKeys[slot];
  const current = configs?.[slot];
  const changedHost = Boolean(current && serviceOrigin(current.base_url) !== serviceOrigin(form.base_url));
  const update = (field: keyof Form, value: string) => {
    setForms((previous) => ({ ...previous, [slot]: { ...previous[slot], [field]: value } }));
    if (field === "api_key" && value.trim()) setClearKeys((previous) => ({ ...previous, [slot]: false }));
  };
  const selectProvider = (provider: Form["provider"]) => {
    setForms((previous) => ({ ...previous, [slot]: { ...previous[slot], provider, api_key: "",
      base_url: provider === "fish" ? "https://api.fish.audio" : "https://api.openai.com/v1",
      model: "", voice: "" } }));
    setClearKeys((previous) => ({ ...previous, [slot]: false }));
  };
  const save = async () => {
    setBusy(true); setError(""); setNotice("");
    try {
      const payload: Record<string, string> = { provider: form.provider, base_url: form.base_url.trim(), model: form.model.trim(), voice: form.voice.trim() };
      if (clearKey) payload.api_key = "";
      else if (form.api_key.trim()) payload.api_key = form.api_key.trim();
      const response = await runtimeFetch(`${runtime}/api/voice/config/${slot}`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      const value = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorMessage(value, "语音配置保存失败"));
      const next = value as Configs;
      setConfigs(next);
      setForms((previous) => ({ ...previous, [slot]: asForm(next[slot]) }));
      setClearKeys((previous) => ({ ...previous, [slot]: false }));
      setNotice(`${slot === "asr" ? "识别" : "合成"}配置已保存，下次语音请求生效。${changedHost && !payload.api_key ? "旧服务密钥未带入新地址。" : ""}`);
      if ("__TAURI_INTERNALS__" in window) await emit("ella-voice-status-request");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "语音配置保存失败"); }
    finally { setBusy(false); }
  };
  const reset = async () => {
    setBusy(true); setError(""); setNotice("");
    try {
      const response = await runtimeFetch(`${runtime}/api/voice/config/${slot}`, { method: "DELETE" });
      const value = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorMessage(value, "恢复语音配置失败"));
      const next = value as Configs;
      setConfigs(next);
      setForms((previous) => ({ ...previous, [slot]: asForm(next[slot]) }));
      setClearKeys((previous) => ({ ...previous, [slot]: false }));
      setNotice("已恢复环境变量配置，并移除这个服务保存的密钥。");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "恢复语音配置失败"); }
    finally { setBusy(false); }
  };
  const reloadConfig = async () => {
    setBusy(true); setError(""); setNotice("");
    try {
      const response = await runtimeFetch(`${runtime}/api/voice/config`);
      const value = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorMessage(value, "语音配置读取失败"));
      const next = value as Configs;
      setConfigs(next);
      setForms({ asr: asForm(next.asr), tts: asForm(next.tts) });
      setClearKeys({ asr: false, tts: false });
      setNotice("已重新读取语音配置。");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "语音配置读取失败"); }
    finally { setBusy(false); }
  };
  const clearContext = async () => {
    try {
      const response = await runtimeFetch(`${runtime}/api/voice/context`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ conversation_id: null }) });
      const data = await response.json();
      if (!response.ok) throw new Error(errorMessage(data, "取消接续失败"));
      setNotice("语音已取消后台聊天参考，语音已听历史仍保留。");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "取消接续失败"); }
  };
  const clearHistory = async () => {
    if (!window.confirm("清空语音对话的文字记录？已提取的长期记忆可在记忆模块单独删除。")) return;
    try {
      const response = await runtimeFetch(`${runtime}/api/voice/history`, { method: "DELETE" });
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        throw new Error(errorMessage(detail, "清空语音记录失败"));
      }
      setNotice("语音文字记录已清空"); setError("");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "清空语音记录失败"); }
  };
  useEffect(() => () => { stopPreview.current(); previewAudio.current?.pause(); if (previewUrl.current) URL.revokeObjectURL(previewUrl.current); }, []);
  const listening = (action: "pause" | "resume") => {
    if ("__TAURI_INTERNALS__" in window) void emit("ella-listening-control", { action });
  };
  const testMicrophone = async () => {
    setError(""); setNotice("");
    try {
      const media = await navigator.mediaDevices.getUserMedia({ audio: true });
      const count = media.getAudioTracks().length;
      media.getTracks().forEach((track) => track.stop());
      if (!count) throw new Error("没有可用麦克风");
      setNotice("麦克风权限正常。识别效果请恢复监听后试说一句话。");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "无法使用麦克风"); }
  };
  const preview = async () => {
    setBusy(true); setError(""); setNotice("");
    try {
      const response = await runtimeFetch(`${runtime}/api/voice/preview`, { method: "POST" });
      const data = await response.json();
      if (!response.ok) throw new Error(errorMessage(data, "音色试听失败"));
      if (!/^audio\/[\w.+-]+$/.test(data.media_type)) throw new Error("试听音频格式无效");
      const bytes = Uint8Array.from(atob(data.audio_base64), (char) => char.charCodeAt(0));
      const url = URL.createObjectURL(new Blob([bytes], { type: data.media_type }));
      previewUrl.current = url;
      const audio = new Audio(url); previewAudio.current = audio;
      await new Promise<void>((resolve, reject) => {
        const timer = window.setTimeout(() => reject(new Error("试听播放超时")), 45000);
        stopPreview.current = () => { window.clearTimeout(timer); reject(new DOMException("试听已停止", "AbortError")); };
        audio.onended = () => { window.clearTimeout(timer); resolve(); };
        audio.onerror = () => { window.clearTimeout(timer); reject(new Error("试听音频无法播放")); };
        void audio.play().catch((reason) => { window.clearTimeout(timer); reject(reason); });
      });
      setNotice("试听播放完成。此操作仅测试已保存的 TTS 音色，不会写入聊天记录。");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "音色试听失败"); }
    finally { stopPreview.current = () => {}; previewAudio.current?.pause(); previewAudio.current = null; if (previewUrl.current) URL.revokeObjectURL(previewUrl.current); previewUrl.current = null; setBusy(false); }
  };
  return <section className="panel">
    <div className="section-title"><h2>语音服务</h2><span>录音和播放在艾拉桌面窗口进行</span></div>
    {statusError && <p className="usage-error">{statusError}</p>}
    {error && <p className="usage-error" role="alert">{error}</p>}
    {notice && <p className="model-config-notice" role="status">{notice}</p>}
    <div className="catalog-grid">
      <article className="catalog-card"><h3>语音识别</h3><p>{configs?.asr.model || status?.transcription_model || "尚未配置模型"}</p><strong>{(configs?.asr.configured ?? status?.can_transcribe) ? "配置已齐全" : "待配置"}</strong></article>
      <article className="catalog-card"><h3>语音合成</h3><p>{configs?.tts.provider || status?.speech_provider || "openai"} · {configs?.tts.model || status?.speech_model || "尚未配置模型"}{(configs?.tts.voice || status?.voice) ? ` · ${configs?.tts.voice || status?.voice}` : ""}</p><strong>{(configs?.tts.configured ?? status?.can_speak) ? "配置已齐全" : "待配置"}</strong></article>
    </div>
    <div className="model-config-form">
      <label>配置位置<select disabled={busy} value={slot} onChange={(event) => { setSlot(event.target.value as VoiceSlot); setError(""); setNotice(""); }}><option value="asr">语音识别 ASR</option><option value="tts">语音合成 TTS</option></select></label>
      <label>接口格式<select disabled={busy} value={form.provider} onChange={(event) => selectProvider(event.target.value as Form["provider"])}><option value="openai">兼容 OpenAI</option>{slot === "tts" && <option value="fish">Fish Audio</option>}</select></label>
      <label className="wide">服务地址<input disabled={busy} value={form.base_url} onChange={(event) => update("base_url", event.target.value)} placeholder="http://127.0.0.1:8000/v1 或 https://..." /></label>
      <label>模型 ID<input disabled={busy} value={form.model} onChange={(event) => update("model", event.target.value)} placeholder={slot === "asr" ? "服务支持的识别模型" : "服务支持的合成模型"} /></label>
      {slot === "tts" && <label>{form.provider === "fish" ? "音色 reference_id" : "音色 ID"}<input disabled={busy} value={form.voice} onChange={(event) => update("voice", event.target.value)} placeholder="服务提供的音色 ID" /></label>}
      <label className="wide">API 密钥<input disabled={busy || clearKey} type="password" autoComplete="new-password" value={form.api_key} onChange={(event) => update("api_key", event.target.value)} placeholder={changedHost ? "已切换服务地址，请填写新服务密钥" : current?.key_present ? "已有当前服务密钥，留空保留" : "本地服务可留空"} /></label>
      {current?.key_present && <label className="wide" style={{ flexDirection: "row", alignItems: "center" }}><input style={{ width: "auto" }} disabled={busy} type="checkbox" checked={clearKey} onChange={(event) => setClearKeys((previous) => ({ ...previous, [slot]: event.target.checked }))} />保存时清除这个服务的密钥</label>}
      <p className="model-status-hint wide">识别与合成可以连接不同服务。密钥保存在 Windows 凭据库，切换主机或端口不会带入旧密钥。配置齐全表示字段可用，首次语音请求会验证服务是否能正常响应。</p>
      <div className="model-config-actions wide"><button type="button" disabled={busy || !configs} onClick={() => void save()}>{busy ? "处理中…" : "保存语音配置"}</button><button type="button" disabled={busy || current?.source !== "saved"} onClick={() => void reset()}>恢复环境变量</button><button type="button" disabled={busy} onClick={() => void reloadConfig()}>重新读取配置</button></div>
    </div>
    <div className="memory-note" role="status">
      <strong>桌宠语音：{live ? (live.available ? phaseLabels[live.phase] : "识别服务未就绪") : "等待桌宠状态"}</strong>
      <p>{live?.message || "连接、录音和播放状态会显示在这里。"}</p>
    </div>
    <div className="model-config-actions">
      <button disabled={busy} onClick={() => listening("pause")}>暂停桌宠监听</button>
      <button disabled={busy} onClick={() => listening("resume")}>恢复桌宠监听</button>
      <button disabled={busy} onClick={() => void clearContext()}>取消后台聊天接续</button>
      <button disabled={busy} onClick={() => void testMicrophone()}>检查麦克风权限</button>
      <button disabled={busy || !configs?.tts.configured} onClick={() => void preview()}>试听已保存音色</button>
    </div>
    <p className="memory-note">试听前先暂停桌宠监听。试听会调用已配置的 TTS 服务，远程服务可能计费。也可以直接说“暂停监听”，恢复请点击人物或上方按钮。</p>
    <label className="voice-listening-setting"><input type="checkbox" checked={continuous} onChange={(event) => { const enabled = event.target.checked; setContinuous(enabled); localStorage.setItem("ella-continuous-listening", String(enabled)); }} />实时监听：说话后停顿约一秒自动回复，播放结束后继续听</label>
    <label className="voice-listening-setting"><input type="checkbox" checked={bargeIn} onChange={(event) => { setBargeIn(event.target.checked); localStorage.setItem("ella-voice-barge-in", String(event.target.checked)); }} />开口打断：思考和播报期间持续采集人声（配合实时监听，建议佩戴耳机）</label>
    <label className="voice-listening-setting"><input type="checkbox" checked={spokenNotifications} onChange={(event) => { setSpokenNotifications(event.target.checked); localStorage.setItem("ella-spoken-notifications", String(event.target.checked)); }} />语音播报提醒与任务结果</label>
    <p className="memory-note">识别采用停顿后整句提交；模型增量输出与分段语音合成并行进行。回声消除：{live?.echoCancellation ? "浏览器已启用" : "尚未确认，播报期间不开启语音打断"}。当前语音服务未接入原生增量识别协议。</p>
    <p className="memory-note">配置语音识别和语音合成后可在桌宠中对话。桌宠保持无字幕显示，连接与权限错误可在这里查看。播报时点击人物或按 Ctrl+Alt+V 可打断。工作指令和提醒由当前对话处理。服务状态：{status?.state || "未知"}。</p>
    <button type="button" className="text-button" onClick={() => void clearHistory()}>清空语音文字记录</button>
  </section>;
}
