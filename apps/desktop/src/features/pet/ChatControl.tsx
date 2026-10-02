import { emit } from "@tauri-apps/api/event";
import { useEffect, useRef, useState } from "react";
import { runtimeFetch } from "../../runtime-client";
import "./chat.css";

type Message = { role: "user" | "assistant"; content: string };
type Conversation = {
  id: string;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
  preview: string;
  message_count: number;
};
type LoadState = "ready" | "loading" | "offline" | "uncertain";

class ChatRequestError extends Error {
  constructor(message: string, readonly uncertain: boolean) { super(message); }
}

const runtime = "http://127.0.0.1:8766";
const key = "ella-next-conversation-id";
const cacheKey = (identity: string) => "ella-next-chat-cache-" + identity;

function cachedMessages(identity: string | null): Message[] {
  if (!identity) return [];
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(cacheKey(identity)) || "[]");
    return Array.isArray(parsed)
      ? parsed.filter((item): item is Message =>
        item !== null && typeof item === "object"
        && (item.role === "user" || item.role === "assistant")
        && typeof item.content === "string")
      : [];
  } catch { return []; }
}

function saveMessages(identity: string, messages: Message[]) {
  try { localStorage.setItem(cacheKey(identity), JSON.stringify(messages.slice(-100))); }
  catch { /* Storage can be full; the SQLite copy remains authoritative. */ }
}

export function ChatControl() {
  const [conversationId, setConversationId] = useState<string | null>(() => localStorage.getItem(key));
  const [messages, setMessages] = useState<Message[]>(() => cachedMessages(localStorage.getItem(key)));
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [search, setSearch] = useState("");
  const [copied, setCopied] = useState<number | null>(null);
  const [loadState, setLoadState] = useState<LoadState>("ready");
  const [retryKey, setRetryKey] = useState(0);
  const [listReload, setListReload] = useState(0);
  const [listError, setListError] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [memoryNotice, setMemoryNotice] = useState("");
  const [handoffBusy, setHandoffBusy] = useState(false);
  const [taskSummary, setTaskSummary] = useState("");
  const [taskGoal, setTaskGoal] = useState("");
  const selected = useRef(conversationId);
  const selectionVersion = useRef(0);
  const uncertainSend = useRef(false);
  const sending = useRef(false);
  const freshConversation = useRef<string | null>(null);
  const mounted = useRef(true);
  const end = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; selectionVersion.current += 1; };
  }, []);

  const handoff = async () => {
    if (!conversationId || handoffBusy) return;
    const identity = conversationId;
    setHandoffBusy(true); setError("");
    try {
      if ("__TAURI_INTERNALS__" in window) await emit("ella-listening-control", { action: "pause" });
      for (let attempt = 0; ; attempt += 1) {
        const response = await runtimeFetch(`${runtime}/api/voice/context`, {
          method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ conversation_id: identity }),
        });
        const data = await response.json();
        if (response.ok) break;
        if (response.status !== 409 || attempt >= 12) throw new Error(typeof data.detail === "string" ? data.detail : "语音接续失败");
        await new Promise((resolve) => setTimeout(resolve, 200));
      }
      if (mounted.current && selected.current === identity) setMemoryNotice("已将这段聊天作为语音参考。点击人物恢复监听后可接着聊；工作计划仍需在语音中完整交付后确认。");
    } catch (reason) { if (mounted.current) setError(reason instanceof Error ? reason.message : "语音接续失败"); }
    finally { if (mounted.current) setHandoffBusy(false); }
  };
  const switchTo = (identity: string | null) => {
    selectionVersion.current += 1;
    selected.current = identity;
    freshConversation.current = null;
    if (identity) localStorage.setItem(key, identity);
    else localStorage.removeItem(key);
    setConversationId(identity);
    setMessages(cachedMessages(identity));
    setLoadState(identity ? "loading" : "ready");
    uncertainSend.current = false;
    setError("");
    setMemoryNotice("");
    setCopied(null);
  };

  useEffect(() => {
    let active = true;
    if (!conversationId) {
      setLoadState("ready");
      return () => { active = false; };
    }
    const identity = conversationId;
    if (freshConversation.current === identity) {
      freshConversation.current = null;
      return () => { active = false; };
    }
    if (sending.current) return () => { active = false; };
    setLoadState("loading");
    const load = async () => {
      try {
        const response = await runtimeFetch(runtime + "/api/chat/" + encodeURIComponent(identity));
        if (!active || selected.current !== identity) return;
        if (response.status === 404) {
          localStorage.removeItem(cacheKey(identity));
          switchTo(null);
          setError("这段对话已不存在。可以从列表选择其他对话。");
          setListReload((value) => value + 1);
          return;
        }
        if (!response.ok) throw new Error("历史对话暂时无法载入");
        const data = await response.json() as { messages: Message[] };
        if (!active || selected.current !== identity) return;
        setMessages(data.messages);
        saveMessages(identity, data.messages);
        setLoadState("ready");
        setError(uncertainSend.current
          ? "请核对历史末尾是否已有刚才的消息，再决定是否重发。"
          : "");
        uncertainSend.current = false;
      } catch {
        if (!active || selected.current !== identity) return;
        setLoadState("offline");
        setError("运行时暂不可用，已保留当前会话和本地消息。恢复连接后会自动重试。");
      }
    };
    void load();
    return () => { active = false; };
  }, [conversationId, retryKey]);

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const response = await runtimeFetch(runtime + "/api/conversations");
        if (!response.ok) throw new Error("会话列表暂不可用");
        const items = await response.json() as Conversation[];
        if (!active) return;
        setConversations(items);
        setListError(false);
      } catch {
        if (active) setListError(true);
      }
    };
    void load();
    return () => { active = false; };
  }, [listReload]);

  useEffect(() => {
    if (loadState !== "offline" && loadState !== "uncertain" && !listError) return;
    const timer = window.setInterval(() => {
      if (!busy && (loadState === "offline" || loadState === "uncertain")) {
        setRetryKey((value) => value + 1);
      }
      setListReload((value) => value + 1);
    }, 10_000);
    return () => window.clearInterval(timer);
  }, [loadState, listError, busy]);

  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [messages]);

  const retry = () => {
    setRetryKey((value) => value + 1);
    setListReload((value) => value + 1);
  };

  const create = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const response = await runtimeFetch(runtime + "/api/conversations", { method: "POST" });
      if (!response.ok) throw new Error("新建对话失败");
      const item = await response.json() as Conversation;
      if (!mounted.current) return;
      switchTo(item.id);
      setShowArchived(false);
      setListReload((value) => value + 1);
    } catch {
      if (mounted.current) setError("新建对话失败，请检查运行时连接。");
    } finally { if (mounted.current) setBusy(false); }
  };

  const send = async () => {
    const text = draft.trim();
    if (!text || busy || loadState !== "ready"
      || conversations.find((item) => item.id === selected.current)?.archived_at) return;
    let identity = selected.current;
    const version = selectionVersion.current;
    let submitted = false;
    const optimistic: Message = { role: "user", content: text };
    setDraft("");
    setBusy(true);
    setError("");
    setMemoryNotice("");
    setMessages((current) => [...current, optimistic]);
    sending.current = true;
    try {
      // Fix the identity before invoking a model so uncertain delivery can be reconciled.
      if (!identity) {
        const created = await runtimeFetch(runtime + "/api/conversations", { method: "POST" });
        const item = await created.json().catch(() => ({})) as { id?: string; detail?: string };
        if (!created.ok || typeof item.id !== "string") throw new ChatRequestError(item.detail || "新建会话失败", false);
        if (!mounted.current || version !== selectionVersion.current) return;
        identity = item.id;
        freshConversation.current = identity;
        selected.current = identity;
        localStorage.setItem(key, identity);
        setConversationId(identity);
        setListReload((value) => value + 1);
      }
      submitted = true;
      const response = await runtimeFetch(runtime + "/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, conversation_id: identity }),
      });
      if (response.status === 404 && identity && mounted.current && version === selectionVersion.current) {
        localStorage.removeItem(cacheKey(identity));
        switchTo(null);
        setDraft(text);
        setError("这段对话已不存在，请选择其他对话或新建。");
        setListReload((value) => value + 1);
        return;
      }
      const data = await response.json().catch(() => ({})) as {
        conversation_id?: string; reply?: string; memory_saved?: boolean; detail?: unknown;
      };
      if (!response.ok) {
        const detail = typeof data.detail === "string" ? data.detail : `聊天请求失败：HTTP ${response.status}`;
        throw new ChatRequestError(detail, response.status >= 500 && response.status !== 502);
      }
      if (typeof data.conversation_id !== "string" || typeof data.reply !== "string") {
        throw new ChatRequestError("运行时返回了不完整的聊天结果", true);
      }
      const replyId = data.conversation_id;
      const replyText = data.reply;
      if (!mounted.current || version !== selectionVersion.current) return;
      if (data.conversation_id !== identity) {
        selected.current = data.conversation_id;
        localStorage.setItem(key, data.conversation_id);
        setConversationId(data.conversation_id);
      }
      setMessages((current) => {
        const next = [...current, { role: "assistant", content: replyText } as Message];
        saveMessages(replyId, next);
        return next;
      });
      setListReload((value) => value + 1);
      uncertainSend.current = false;
      if (data.memory_saved) setMemoryNotice("已加入长期记忆，可在后台修正或删除。");
    } catch (reason) {
      if (!mounted.current || version !== selectionVersion.current) return;
      setDraft(text);
      setMessages((current) =>
        current[current.length - 1] === optimistic ? current.slice(0, -1) : current);
      const uncertain = submitted && (!(reason instanceof ChatRequestError) || reason.uncertain);
      uncertainSend.current = uncertain;
      setLoadState(uncertain ? "uncertain" : "ready");
      const detail = reason instanceof Error ? reason.message : "聊天请求失败";
      setError(uncertain
        ? `${detail}。发送结果未确认，请先载入历史核对再重发。`
        : `${detail}。消息未发送，草稿已保留。`);
    } finally {
      sending.current = false;
      if (mounted.current) setBusy(false);
    }
  };

  const setArchive = async (archived: boolean) => {
    const identity = selected.current;
    if (!identity || busy) return;
    setBusy(true);
    try {
      const response = await runtimeFetch(
        runtime + "/api/conversations/" + encodeURIComponent(identity) + "/archive",
        { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ archived }) },
      );
      if (!response.ok) throw new Error("更新归档状态失败");
      const updated = await response.json() as Conversation;
      if (!mounted.current || selected.current !== identity) return;
      setConversations((current) => current.map((item) => item.id === identity ? updated : item));
      setShowArchived(archived);
      setListReload((value) => value + 1);
      setError("");
    } catch {
      if (mounted.current) setError("更新归档状态失败，请稍后重试。");
    } finally { if (mounted.current) setBusy(false); }
  };

  const remove = async () => {
    const identity = selected.current;
    if (!identity || busy || !window.confirm("确认永久删除这段对话？")) return;
    setBusy(true);
    try {
      const response = await runtimeFetch(runtime + "/api/chat/" + encodeURIComponent(identity), { method: "DELETE" });
      if (!response.ok && response.status !== 404) throw new Error("删除对话失败");
      if (!mounted.current || selected.current !== identity) return;
      localStorage.removeItem(cacheKey(identity));
      switchTo(null);
      setListReload((value) => value + 1);
    } catch {
      if (mounted.current) setError("删除对话失败，请检查运行时连接。");
    } finally { if (mounted.current) setBusy(false); }
  };

  useEffect(() => {
    let active = true;
    let inFlight = false;
    setTaskSummary(""); setTaskGoal("");
    if (!conversationId) return;
    const refresh = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const response = await runtimeFetch(`${runtime}/api/conversations/${encodeURIComponent(conversationId)}/task`);
        if (!response.ok) return;
        const value = await response.json() as { task: { goal: string } | null; summary?: string };
        if (active) { setTaskSummary(value.summary || ""); setTaskGoal(value.task?.goal || ""); }
      } catch { /* Keep the last actual status during a brief connection loss. */ }
      finally { inFlight = false; }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => { active = false; window.clearInterval(timer); };
  }, [conversationId, messages.length]);
  const current = conversations.find((item) => item.id === conversationId);
  const visible = conversations.filter((item) => Boolean(item.archived_at) === showArchived
    && item.preview.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
  const copyMessage = async (content: string, index: number) => {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(index);
    } catch { setError("暂时无法复制，请选择消息文本复制。"); }
  };
  const canSend = loadState === "ready" && !current?.archived_at;

  return (
    <section className="chat-control" aria-label="文字对话">
      <div className="chat-heading">
        <strong>对话记录</strong>
        <div className="chat-heading-actions">
          <button type="button" onClick={() => void create()} disabled={busy}>新建</button>
          <button type="button" onClick={retry} disabled={busy}>刷新会话</button>
        </div>
      </div>
      <div className="chat-layout">
        <aside className="chat-conversations" aria-label="会话列表">
          <div className="chat-list-tabs">
            <button type="button" className={!showArchived ? "active" : ""} onClick={() => setShowArchived(false)}>进行中</button>
            <button type="button" className={showArchived ? "active" : ""} onClick={() => setShowArchived(true)}>已归档</button>
          </div>
          <input className="chat-conversation-search" aria-label="搜索会话" placeholder="搜索会话…" value={search} onChange={(event) => setSearch(event.target.value)} />
          {listError && <small className="chat-list-error">列表暂不可用，正在重试</small>}
          {visible.length === 0 && <small className="chat-list-empty">{search.trim() ? "没有匹配的会话" : "暂无会话"}</small>}
          <div className="chat-conversation-items">{visible.map((item) =>
            <button type="button" key={item.id} disabled={busy}
              className={item.id === conversationId ? "chat-conversation active" : "chat-conversation"}
              onClick={() => item.id === conversationId ? retry() : switchTo(item.id)}>
              <span>{item.preview}</span>
              <small>{new Date(item.updated_at).toLocaleString("zh-CN")} · {item.message_count} 条消息</small>
            </button>
          )}</div>
        </aside>
        <div className="chat-main">
          <div className="chat-session-actions">
            <span>{current?.preview || (conversationId ? "当前对话" : "新对话")}</span>
            {conversationId && <>
              <button type="button" disabled={busy || !current} onClick={() => void setArchive(!current?.archived_at)}>
                {current?.archived_at ? "恢复" : "归档"}
              </button>
              <button type="button" disabled={busy} onClick={() => void remove()}>删除</button>
            </>}
          </div>
          <div className="chat-messages">
            {messages.length === 0 && <div className="chat-empty"><span className="chat-empty-mark" aria-hidden="true">✦</span><strong>今天想聊点什么？</strong><span>我是艾拉。配置主聊天模型后，就可以在这里开始对话。</span></div>}
            {messages.map((message, index) => <article className={"chat-message " + message.role} key={index}>
              <b>{message.role === "user" ? "你" : "艾拉"}</b><div className="chat-message-content">{message.content}</div>
              {message.role === "assistant" && <button type="button" className="copy-message" onClick={() => void copyMessage(message.content, index)}>{copied === index ? "已复制" : "复制回复"}</button>}
            </article>)}
            {busy && <div className="chat-thinking" role="status">正在处理…</div>}
            <div ref={end} />
          </div>
          {taskSummary && <div className="chat-task-status" role="status"><strong>当前工作 · {taskGoal}</strong><p>{taskSummary}</p><a href="#tasks">查看完整计划与核验记录</a></div>}
          {loadState === "offline" && <p className="chat-offline">当前显示本地缓存；连接恢复后自动同步。</p>}
          {conversationId && !current?.archived_at && <button type="button" disabled={busy || handoffBusy || loadState !== "ready"} onClick={() => void handoff()}>{handoffBusy ? "正在接续语音…" : "让语音接着这段聊"}</button>}
      {error && <p className="chat-error">{error}</p>}
          {memoryNotice && <p className="chat-memory-notice">{memoryNotice}</p>}
          <div className="chat-send">
            <textarea value={draft} onChange={(event) => setDraft(event.target.value)} rows={2} aria-label="消息内容"
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  void send();
                }
              }}
              placeholder={current?.archived_at ? "先恢复归档对话" : "和艾拉说点什么…"} maxLength={10000} disabled={busy || Boolean(current?.archived_at)} />
            <button type="button" onClick={() => void send()} disabled={busy || !draft.trim() || !canSend}>
              {busy ? "…" : "发送"}
            </button>
          </div>
          <div className="chat-compose-hint">Enter 发送 · Shift + Enter 换行</div>
        </div>
      </div>
    </section>
  );
}
