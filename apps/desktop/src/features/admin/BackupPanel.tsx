import { useEffect, useState } from "react";
import { runtimeEnsure, runtimeFetch } from "../../runtime-client";

type BackupFile = { name: string; size: number; sha256: string; rows: Record<string, number> };
type RestorePreview = { id: string; created_at: string | null; files: BackupFile[]; effects: string[] };
type BackupStatus = { pending: RestorePreview | null; last_restore: { status: string; message: string; error?: string } | null };

const limit = 8 * 1024 * 1024;
const sizes = (bytes: number) => `${(bytes / 1024).toFixed(1)} KiB`;
async function responseJson(response: Response) {
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : `HTTP ${response.status}`);
  return value;
}
function encodeFile(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("备份文件读取失败"));
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.readAsDataURL(file);
  });
}

export function BackupPanel() {
  const [status, setStatus] = useState<BackupStatus | null>(null);
  const [preview, setPreview] = useState<RestorePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const refresh = async () => {
    const value = await responseJson(await runtimeFetch("/api/backup/status")) as BackupStatus;
    setStatus(value);
    if (value.pending) setPreview(value.pending);
  };
  useEffect(() => { void refresh().catch((caught) => setError(caught instanceof Error ? caught.message : "读取失败")); }, []);

  const exportBackup = async () => {
    setBusy(true); setError(""); setMessage("");
    try {
      const response = await runtimeFetch("/api/backup/export");
      if (!response.ok) { await responseJson(response); return; }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `ella-backup-${new Date().toISOString().slice(0, 10)}.zip`;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setMessage("备份已导出。ZIP 包含你的对话和记忆，请保存到你信任的位置。");
    } catch (caught) { setError(caught instanceof Error ? caught.message : "导出失败"); }
    finally { setBusy(false); }
  };

  const inspectBackup = async (file: File) => {
    if (!file.name.toLowerCase().endsWith(".zip") || file.size > limit || file.size === 0) {
      setError("请选择不超过 8 MiB 的艾拉备份 ZIP。"); return;
    }
    setBusy(true); setError(""); setMessage("");
    try {
      if (preview && !status?.pending) {
        const cancelled = await runtimeFetch(`/api/backup/staged/${preview.id}`, { method: "DELETE" });
        if (!cancelled.ok) await responseJson(cancelled);
      }
      const value = await responseJson(await runtimeFetch("/api/backup/preview", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ archive_base64: await encodeFile(file) }),
      })) as RestorePreview;
      setPreview(value);
      setMessage("校验完成。当前数据尚未改变，请核对下面的恢复内容。");
    } catch (caught) { setPreview(null); setError(caught instanceof Error ? caught.message : "校验失败"); }
    finally { setBusy(false); }
  };

  const cancel = async () => {
    if (!preview) return;
    setBusy(true); setError("");
    try {
      const response = await runtimeFetch(`/api/backup/staged/${preview.id}`, { method: "DELETE" });
      if (!response.ok) await responseJson(response);
      setPreview(null); setMessage("已取消恢复，当前数据保持原状。");
      await refresh();
    } catch (caught) { setError(caught instanceof Error ? caught.message : "取消失败"); }
    finally { setBusy(false); }
  };

  const restore = async () => {
    if (!preview) return;
    setBusy(true); setError("");
    try {
      await responseJson(await runtimeFetch("/api/backup/confirm", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: preview.id }),
      }));
      setStatus((previous) => ({ pending: preview, last_restore: previous?.last_restore || null }));
      setMessage("已确认恢复，正在重启艾拉运行时……");
      const restarted = await runtimeEnsure(true);
      if (restarted.state !== "running") throw new Error(restarted.error || "运行时重启失败，恢复仍待下次启动处理");
      setPreview(null);
      await refresh();
      setMessage("运行时已重启，请查看恢复结果，并重新配置保存的服务密钥。");
    } catch (caught) { setError(caught instanceof Error ? caught.message : "恢复失败"); }
    finally { setBusy(false); }
  };

  return <section id="backup" className="panel memory-panel">
    <div className="section-title"><h2>数据备份与恢复</h2><button type="button" disabled={busy} onClick={() => void exportBackup()}>{busy ? "处理中" : "导出备份 ZIP"}</button></div>
    <p className="memory-note">备份包含对话、长期记忆、任务记录、提醒、用量及模型和语音服务设置。浏览器登录、API 密钥、录音、游戏与存档另行保管。</p>
    <p className="memory-note">恢复会在运行时重启前覆盖九个数据库，并自动保留恢复前备份。未完成任务需要重新核对与批准，保存的服务密钥需要重新填写。</p>
    <label className="memory-form">选择备份文件（ZIP，最大 8 MiB）<input type="file" accept=".zip,application/zip" disabled={busy || Boolean(status?.pending)} onChange={(event) => { const file = event.target.files?.[0]; if (file) void inspectBackup(file); event.target.value = ""; }} /></label>
    {error && <p className="usage-error" role="alert">{error}</p>}
    {message && <p className="memory-note" role="status">{message}</p>}
    {status?.last_restore && <p className={status.last_restore.status === "restored" ? "memory-note" : "usage-error"}>最近恢复：{status.last_restore.message}{status.last_restore.error ? ` ${status.last_restore.error}` : ""}</p>}
    {preview && <div className="memory-item">
      <h3>{status?.pending ? "已确认，等待重启的恢复" : "恢复预览"}</h3>
      <p>备份时间：{preview.created_at ? new Date(preview.created_at).toLocaleString("zh-CN") : "未知"}</p>
      <div className="memory-list">{preview.files.map((file) => <article className="memory-item" key={file.name}>
        <div className="memory-meta"><b>{file.name}</b><span>{sizes(file.size)}</span></div>
        <p>{Object.entries(file.rows).filter(([name]) => name !== "memory_fts").map(([name, count]) => `${name}: ${count} 条`).join(" · ") || "空数据库，会覆盖对应现有数据"}</p>
      </article>)}</div>
      <ul>{preview.effects.map((effect) => <li key={effect}>{effect}</li>)}</ul>
      <div className="memory-actions"><button type="button" disabled={busy} onClick={() => void restore()}>确认恢复并重启</button><button type="button" className="text-button" disabled={busy} onClick={() => void cancel()}>取消恢复</button></div>
    </div>}
  </section>;
}
