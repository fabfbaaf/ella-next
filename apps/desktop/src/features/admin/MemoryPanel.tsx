import { useCallback, useEffect, useState } from "react";
import { runtimeFetch } from "../../runtime-client";

type MemoryRecord = {
  id: string;
  kind: "fact" | "preference" | "event";
  content: string;
  source_type: string;
  source_ref: string | null;
  confidence: number;
  tags: string[];
  updated_at: string;
  topic: string | null;
  active: boolean;
  superseded_by: string | null;
  inactive_reason: string | null;
};
type SourceMessage = { role: "user" | "assistant"; content: string };

const runtimeUrl = "http://127.0.0.1:8766";
const kindLabel = { fact: "事实", preference: "偏好", event: "事件" };

export function MemoryPanel() {
  const [items, setItems] = useState<MemoryRecord[]>([]);
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<MemoryRecord["kind"]>("fact");
  const [content, setContent] = useState("");
  const [tags, setTags] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [editedContent, setEditedContent] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [sourcePreview, setSourcePreview] = useState<{ id: string; messages: SourceMessage[] } | null>(null);

  const refresh = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (query.trim()) params.set("query", query.trim());
      const response = await runtimeFetch(`${runtimeUrl}/api/memory?${params}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setItems((await response.json()) as MemoryRecord[]);
      setError("");
    } catch (caught) {
      setItems([]);
      setError(caught instanceof Error ? caught.message : "读取失败");
    }
  }, [query]);

  useEffect(() => { void refresh(); }, [refresh]);

  const create = async () => {
    if (!content.trim()) return;
    try {
      const response = await runtimeFetch(`${runtimeUrl}/api/memory`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind,
          content: content.trim(),
          source_type: "manual",
          tags: tags.split(",").map((tag) => tag.trim()).filter(Boolean),
        }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setContent(""); setTags("");
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "保存失败");
    }
  };

  const correct = async (identity: string) => {
    if (!editedContent.trim() || !reason.trim()) return;
    try {
      const response = await runtimeFetch(`${runtimeUrl}/api/memory/${identity}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: editedContent.trim(), reason: reason.trim() }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setEditing(null); setReason("");
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "修正失败");
    }
  };

  const remove = async (identity: string) => {
    if (!window.confirm("确定彻底删除这条记忆及修正记录吗？")) return;
    try {
      const response = await runtimeFetch(`${runtimeUrl}/api/memory/${identity}`, { method: "DELETE" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "删除失败");
    }
  };

  const exportJson = async () => {
    try {
      const response = await runtimeFetch(`${runtimeUrl}/api/memory/export`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "ella-memory.json";
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "导出失败");
    }
  };

  const showSource = async (identity: string) => {
    if (sourcePreview?.id === identity) { setSourcePreview(null); return; }
    try {
      const response = await runtimeFetch(`${runtimeUrl}/api/memory/${identity}/source`);
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "原始对话不可读取");
      setSourcePreview({ id: identity, messages: result.messages as SourceMessage[] });
      setError("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "原始对话不可读取");
    }
  };

  return (
    <section id="memory" className="panel memory-panel">
      <div className="section-title"><h2>长期记忆</h2><button className="text-button" onClick={() => void exportJson()}>导出 JSON</button></div>
      <p className="memory-note">明确说“记住”会立即写入；会话会定期提取事件摘要与独立事实、偏好。明确否定或修改的偏好会保留来源并标记失效，艾拉检索时使用有效记录。可查看原始对话并修正或删除记忆；检索支持 FTS5 和可选语义向量。</p>
      <div className="memory-form">
        <select value={kind} onChange={(event) => setKind(event.target.value as MemoryRecord["kind"])}>
          <option value="fact">事实</option><option value="preference">偏好</option><option value="event">事件</option>
        </select>
        <input value={content} onChange={(event) => setContent(event.target.value)} placeholder="写下一条需要记住的事" />
        <input value={tags} onChange={(event) => setTags(event.target.value)} placeholder="标签，用逗号分隔" />
        <button onClick={() => void create()}>保存</button>
      </div>
      <input className="memory-search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索记忆" />
      {error && <p className="usage-error">运行时未连接或操作失败：{error}</p>}
      {items.length === 0 && !error && <p className="usage-empty">还没有符合条件的记忆。</p>}
      <div className="memory-list">{items.map((item) => <article className="memory-item" key={item.id}>
        <div className="memory-meta"><b>{kindLabel[item.kind]}</b><span>{item.active === false ? (item.superseded_by ? "已替代" : "已失效") : "有效"}{item.topic ? ` · ${item.topic}` : ""}</span><span>来源：{item.source_type}{item.source_ref ? ` / ${item.source_ref}` : ""}</span><span>{new Date(item.updated_at).toLocaleString("zh-CN")}</span></div>
        {item.active === false && <p className="memory-note">{item.inactive_reason || "这条记录已失效，检索不会使用。"}</p>}
        {editing === item.id ? <div className="memory-editor">
          <textarea value={editedContent} onChange={(event) => setEditedContent(event.target.value)} />
          <input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="修正原因（必填）" />
          <button onClick={() => void correct(item.id)}>保存修正</button><button className="text-button" onClick={() => setEditing(null)}>取消</button>
        </div> : <p>{item.content}</p>}
        <div className="memory-actions"><span>{item.tags.join(" · ")}</span>
          {item.source_type === "conversation" && /^(chat|voice):[^:]+(?::messages:\d+-\d+)?$/.test(item.source_ref || "") &&
            <button className="text-button" onClick={() => void showSource(item.id)}>查看原始对话</button>}
          <button className="text-button" onClick={() => { setEditing(item.id); setEditedContent(item.content); setReason(""); }}>修正</button><button className="text-button danger" onClick={() => void remove(item.id)}>删除</button></div>
        {sourcePreview?.id === item.id && <div className="memory-source">
          {sourcePreview.messages.map((message, index) => <p key={index}><b>{message.role === "user" ? "你" : "艾拉"}：</b>{message.content}</p>)}
        </div>}
      </article>)}</div>
    </section>
  );
}
