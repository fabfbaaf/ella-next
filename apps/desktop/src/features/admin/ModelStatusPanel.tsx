import { useCallback, useEffect, useRef, useState } from "react";
import { runtimeFetch } from "../../runtime-client";
import "./model-status.css";

type Slot = "chat" | "action" | "vision" | "persona";
type Provider = {
  provider: string; model: string; base_url: string; configured: boolean;
  source: "saved" | "environment"; key_saved: boolean;
} | null;
type ModelStatus = Record<Slot, Provider> & { action_uses_chat: boolean; vision_uses_chat: boolean };
type ModelTestResult = { preview: string; provider: string; model: string; latency_ms: number; mode: string };
type Form = { provider: string; model: string; base_url: string; api_key: string };
const runtime = "http://127.0.0.1:8766";
const slots: [Slot, string][] = [
  ["chat", "主聊天"], ["action", "操作"], ["vision", "视觉"], ["persona", "人格润色"],
];
const defaults: Record<Slot, Form> = {
  chat: { provider: "ollama", model: "", base_url: "http://127.0.0.1:11434/v1", api_key: "" },
  action: { provider: "deepseek", model: "deepseek-flash", base_url: "https://api.deepseek.com", api_key: "" },
  vision: { provider: "gemini", model: "", base_url: "https://generativelanguage.googleapis.com/v1beta/openai", api_key: "" },
  persona: { provider: "gemini", model: "", base_url: "https://generativelanguage.googleapis.com/v1beta/openai", api_key: "" },
};
const presets: Record<string, { label: string; base_url: string }> = {
  ollama: { label: "Ollama · 本地", base_url: "http://127.0.0.1:11434/v1" },
  gemini: { label: "Gemini · 官方", base_url: "https://generativelanguage.googleapis.com/v1beta/openai" },
  deepseek: { label: "DeepSeek · 官方", base_url: "https://api.deepseek.com" },
  openai: { label: "OpenAI · 官方", base_url: "https://api.openai.com/v1" },
};
const routeHints: Record<Slot, string> = {
  chat: "日常对话和实时语音使用此模型。语音需要通过流式生成检查。",
  action: "工具选择、应用操作和游戏决策使用此模型；未单独配置时沿用主聊天。",
  vision: "截图理解使用此模型；未单独配置时沿用主聊天，需要模型支持图片输入。此处生成检查仅检查文字，图片能力可在截图功能中验证。",
  persona: "可选的第二次润色调用，会增加耗时与用量。实时语音不经过此路由；艾拉的基础性格在主聊天中始终生效。",
};
const serviceOrigin = (value: string) => {
  try { return new URL(value).origin; }
  catch { return null; }
};

export function ModelStatusPanel() {
  const [status, setStatus] = useState<ModelStatus | null>(null);
  const [slot, setSlot] = useState<Slot>("chat");
  const [forms, setForms] = useState<Record<Slot, Form>>(() => ({ chat: { ...defaults.chat }, action: { ...defaults.action }, vision: { ...defaults.vision }, persona: { ...defaults.persona } }));
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const mounted = useRef(true);
  const version = useRef(0);
  const dirty = useRef(new Set<Slot>());
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [modelLists, setModelLists] = useState<Record<Slot, string[]>>({ chat: [], action: [], vision: [], persona: [] });
  const [testResult, setTestResult] = useState<ModelTestResult | null>(null);
  const [clearKeys, setClearKeys] = useState<Record<Slot, boolean>>({ chat: false, action: false, vision: false, persona: false });
  const form = forms[slot], clearKey = clearKeys[slot];
  const asForm = (value: Provider, id: Slot): Form => value ? { provider: value.provider, model: value.model, base_url: value.base_url, api_key: "" } : { ...defaults[id] };
  const refresh = useCallback(async () => {
    const current = ++version.current;
    try {
      const response = await runtimeFetch(`${runtime}/api/models/status`);
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "运行时未连接");
      if (!mounted.current || current !== version.current) return;
      setStatus(data as ModelStatus);
      setForms((previous) => {
        const next = { ...previous };
        for (const [id] of slots) if (!dirty.current.has(id)) next[id] = asForm(data[id], id);
        return next;
      });
    } catch (reason) {
      if (!mounted.current || current !== version.current) return;
      throw reason;
    }
  }, []);
  useEffect(() => {
    mounted.current = true;
    void refresh().catch((reason) => { if (mounted.current) setError(reason instanceof Error ? reason.message : "运行时未连接"); });
    return () => { mounted.current = false; version.current += 1; };
  }, [refresh]);
  const reload = async () => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError(""); setNotice("");
    try { await refresh(); }
    catch (reason) { if (mounted.current) setError(reason instanceof Error ? reason.message : "运行时未连接"); }
    finally { busyRef.current = false; if (mounted.current) setBusy(false); }
  };
  const select = (next: Slot) => { if (busyRef.current) return; setSlot(next); setError(""); setNotice(""); setTestResult(null); };
  const update = (field: keyof Form, value: string) => {
    if (busyRef.current) return;
    dirty.current.add(slot); setTestResult(null);
    if (field === "base_url" || field === "provider") setModelLists((previous) => ({ ...previous, [slot]: [] }));
    setForms((previous) => ({ ...previous, [slot]: { ...previous[slot], [field]: value } }));
    if (field === "api_key" && value.trim()) setClearKeys((previous) => ({ ...previous, [slot]: false }));
  };
  const chooseModel = (model: string) => {
    if (busyRef.current || !model) return;
    const inherited = !status?.[slot] && (slot === "action" || slot === "vision") && status?.chat;
    if (!inherited) { update("model", model); return; }
    dirty.current.add(slot); setTestResult(null); setError("");
    setForms((previous) => ({ ...previous, [slot]: { ...asForm(inherited, slot), model } }));
    setClearKeys((previous) => ({ ...previous, [slot]: false }));
    setNotice("已按主聊天服务填写独立配置；保存时，同服务密钥会保留。");
  };
  const chooseProvider = (provider: string) => {
    if (busyRef.current) return;
    dirty.current.add(slot); setError(""); setNotice(""); setTestResult(null);
    setForms((previous) => ({ ...previous, [slot]: { provider: provider === "custom" ? "compatible" : provider, model: "", base_url: presets[provider]?.base_url || "", api_key: "" } }));
    setClearKeys((previous) => ({ ...previous, [slot]: false }));
    setModelLists((previous) => ({ ...previous, [slot]: [] }));
  };
  const savedProvider = status?.[slot];
  const changedHost = Boolean(savedProvider && serviceOrigin(savedProvider.base_url) !== serviceOrigin(form.base_url));
  const operate = async (method: "PUT" | "DELETE") => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError(""); setNotice("");
    version.current += 1; setTestResult(null);
    const target = slot;
    try {
      const payload: Record<string, string> = { provider: form.provider.trim(), model: form.model.trim(), base_url: form.base_url.trim() };
      if (clearKey) payload.api_key = ""; else if (form.api_key.trim()) payload.api_key = form.api_key.trim();
      const response = await runtimeFetch(`${runtime}/api/models/config/${target}`, { method, ...(method === "PUT" ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) } : {}) });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : Array.isArray(data.detail) ? data.detail.map((item: { msg?: string }) => item.msg || "字段无效").join("；") : "配置操作失败，请检查字段");
      if (!mounted.current) return;
      setStatus(data as ModelStatus); dirty.current.delete(target);
      setModelLists((previous) => ({ ...previous, [target]: [] }));
      setForms((previous) => ({ ...previous, [target]: asForm(data[target], target) }));
      setClearKeys((previous) => ({ ...previous, [target]: false }));
      setNotice(method === "DELETE" ? "已恢复环境变量配置，并移除保存的密钥" : changedHost && !payload.api_key ? "已保存新服务地址；旧服务密钥未带入。" : "已保存，下一次模型请求立即使用");
    } catch (reason) { if (mounted.current) setError(reason instanceof Error ? reason.message : "配置操作失败"); }
    finally { busyRef.current = false; if (mounted.current) setBusy(false); }
  };
  const save = () => operate("PUT"), reset = () => operate("DELETE");
  const probe = async () => {
    if (busyRef.current || dirty.current.has(slot)) return;
    busyRef.current = true; setBusy(true); setError(""); setNotice(""); setTestResult(null);
    try {
      const response = await runtimeFetch(`${runtime}/api/models/${slot}/probe`, { method: "POST" });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "连接检查失败");
      if (mounted.current) {
        setNotice(data.detail || "连接检查完成");
        setModelLists((previous) => ({ ...previous, [slot]: Array.isArray(data.model_ids) ? data.model_ids.filter((id: unknown): id is string => typeof id === "string" && Boolean(id)) : [] }));
      }
    } catch (reason) { if (mounted.current) setError(reason instanceof Error ? reason.message : "连接检查失败"); }
    finally { busyRef.current = false; if (mounted.current) setBusy(false); }
  };
  const testGeneration = async (mode: "text" | "stream") => {
    if (busyRef.current || dirty.current.has(slot)) return;
    busyRef.current = true; setBusy(true); setError(""); setNotice(""); setTestResult(null);
    try {
      const response = await runtimeFetch(`${runtime}/api/models/${slot}/test?mode=${mode}`, { method: "POST" });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "生成检查失败");
      if (mounted.current) {
        setTestResult(data as ModelTestResult);
        setNotice(mode === "stream" ? "流式回复已完整结束。" : "文字生成请求通过。");
      }
    } catch (reason) { if (mounted.current) setError(reason instanceof Error ? reason.message : "生成检查失败"); }
    finally { busyRef.current = false; if (mounted.current) setBusy(false); }
  };
  const effectiveProvider = status?.[slot] || (slot === "action" || slot === "vision" ? status?.chat : null);
  const rows = status ? slots.map(([id, label]) => {
    const inherited = (id === "action" && status.action_uses_chat) || (id === "vision" && status.vision_uses_chat);
    return { id, label, inherited, provider: inherited ? status.chat : status[id] };
  }) : [];
  return (
    <section id="models" className="panel model-status-panel">
      <div className="section-title"><h2>模型路由</h2><button type="button" disabled={busy} onClick={() => void reload()}>{busy ? "处理中" : "重新读取配置"}</button></div>
      <p className="model-status-hint">支持 Chat Completions 兼容接口。密钥不会回显，重新读取会保留未保存的修改。</p>
      {error && <p className="model-config-error" role="alert">{error}</p>}
      <div className="model-status-list">{rows.map(({ id, label, inherited, provider }) => <div key={id}>
        <b>{label}</b><span>{provider ? `${inherited ? "沿用主聊天 · " : ""}${provider.provider} · ${provider.model || "未填模型"}` : "未单独设置"}</span>
        <em className={provider?.configured ? "configured" : ""}>{provider ? provider.configured ? "字段齐全" : "未配齐" : "可选"}</em>
      </div>)}</div>
      <div className="model-config-form">
        <label>配置位置<select disabled={busy} value={slot} onChange={(event) => select(event.target.value as Slot)}>{slots.map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label>
        <label>提供方<select disabled={busy} value={presets[form.provider] ? form.provider : "custom"} onChange={(event) => chooseProvider(event.target.value)}>{Object.entries(presets).map(([id, value]) => <option key={id} value={id}>{value.label}</option>)}<option value="custom">其他兼容服务</option></select></label>
        {!presets[form.provider] && <label>服务名称<input disabled={busy} value={form.provider} onChange={(event) => update("provider", event.target.value)} placeholder="自定义服务名称" /></label>}
        <label>模型 ID<input disabled={busy} value={form.model} onChange={(event) => update("model", event.target.value)} placeholder="例如 gemini-3.8-flash" /></label>
        <p className="model-route-hint wide">{routeHints[slot]}</p>
        {modelLists[slot].length > 0 && <label className="wide">服务返回的模型<select disabled={busy} value="" onChange={(event) => chooseModel(event.target.value)}><option value="">选择一个模型后保存，也可手动填写 ID</option>{modelLists[slot].map((id) => <option key={id} value={id}>{id}</option>)}</select></label>}
        <label className="wide">服务地址<input disabled={busy} value={form.base_url} onChange={(event) => update("base_url", event.target.value)} placeholder="https://..." /></label>
        <label className="wide">API 密钥<input type="password" autoComplete="new-password" value={form.api_key} onChange={(event) => update("api_key", event.target.value)} disabled={busy || clearKey} placeholder={changedHost ? "服务地址已变化，输入新服务密钥" : status?.[slot]?.key_saved ? "当前服务密钥已保存，留空保留" : "本地模型可留空"} /></label>
        <p className="model-status-hint wide">密钥只用于当前服务主机和端口。同服务换模型可留空保留；切换服务地址时，旧密钥不会发送给新服务。</p>
        {status?.[slot]?.key_saved && <label className="wide"><input disabled={busy} type="checkbox" checked={clearKey} onChange={(event) => { dirty.current.add(slot); setClearKeys((previous) => ({ ...previous, [slot]: event.target.checked })); }} />保存时清除密钥</label>}
        <div className="model-config-actions wide"><button type="button" disabled={busy || !status} onClick={() => void save()}>保存配置</button><button type="button" disabled={busy || !status || dirty.current.has(slot)} onClick={() => void probe()}>检查已保存连接</button><button type="button" disabled={busy || status?.[slot]?.source !== "saved"} onClick={() => void reset()}>恢复环境变量</button></div>
      </div>
      <div className="model-test-actions">
        <button type="button" disabled={busy || dirty.current.has(slot) || !effectiveProvider?.configured} onClick={() => void testGeneration("text")}>试一句</button>
        <button type="button" disabled={busy || dirty.current.has(slot) || !effectiveProvider?.configured} onClick={() => void testGeneration("stream")}>检查流式回复</button>
        <span>这两项会调用已保存的模型，并计入用量。</span>
      </div>
      <p className="model-status-hint">修改后先保存。连接检查只读取模型列表；字段齐全表示配置完整，生成检查通过才说明当前接口能回答。</p>
      {testResult && <div className="model-test-result"><span>{testResult.provider} · {testResult.model} · {testResult.latency_ms} ms</span><p>{testResult.preview}</p></div>}
      {notice && <p className="model-config-notice" role="status">{notice}</p>}
    </section>
  );
}
