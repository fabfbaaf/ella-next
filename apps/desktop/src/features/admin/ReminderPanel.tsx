import { useCallback, useEffect, useState } from "react";
import { runtimeFetch } from "../../runtime-client";
import "./reminder.css";

type Reminder = { id: string; title: string; due_at: string; delivered_at: string | null; read_at: string | null };
type Notification = { id: string; type: string; text: string; delivered_at: string; read_at: string | null };
type Status = { quiet_start: string; quiet_end: string; quiet_now: boolean };
const runtime = "http://127.0.0.1:8766";

export function ReminderPanel() {
  const [items, setItems] = useState<Reminder[]>([]);
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [quietNow, setQuietNow] = useState(false);
  const [now, setNow] = useState(Date.now());
  const [title, setTitle] = useState("");
  const [due, setDue] = useState("");
  const [quietStart, setQuietStart] = useState("22:00");
  const [quietEnd, setQuietEnd] = useState("08:00");
  const [error, setError] = useState("");

  const refresh = useCallback(async (syncQuiet = false) => {
    const [reminders, status, history] = await Promise.all([
      runtimeFetch(`${runtime}/api/reminders`),
      runtimeFetch(`${runtime}/api/companion/status`),
      runtimeFetch(`${runtime}/api/companion/notifications`),
    ]);
    if (!reminders.ok || !status.ok || !history.ok) throw new Error("运行时未连接");
    setItems(await reminders.json() as Reminder[]);
    setNotifications(await history.json() as Notification[]);
    const settings = await status.json() as Status;
    if (syncQuiet) {
      setQuietStart(settings.quiet_start);
      setQuietEnd(settings.quiet_end);
    }
    setQuietNow(settings.quiet_now);
    setNow(Date.now());
  }, []);

  useEffect(() => {
    void refresh(true).catch(() => setError("运行时未连接"));
    const timer = window.setInterval(() => void refresh().catch(() => setError("运行时未连接")), 10_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const add = async () => {
    if (!title.trim() || !due) return;
    try {
      const response = await runtimeFetch(`${runtime}/api/reminders`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: title.trim(), due_at: new Date(due).toISOString() }),
      });
      if (!response.ok) throw new Error((await response.json()).detail || "保存提醒失败");
      setTitle(""); setDue(""); setError("");
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "保存提醒失败"); }
  };

  const remove = async (identity: string) => {
    try {
      const response = await runtimeFetch(`${runtime}/api/reminders/${identity}`, { method: "DELETE" });
      if (!response.ok) throw new Error("删除提醒失败");
      setError(""); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "删除提醒失败"); }
  };

  const saveQuiet = async () => {
    try {
      const response = await runtimeFetch(`${runtime}/api/companion/quiet-hours`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ start: quietStart, end: quietEnd }),
      });
      if (!response.ok) throw new Error((await response.json()).detail || "保存安静时段失败");
      setError(""); await refresh(true);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "保存安静时段失败"); }
  };

  const markRead = async (identity: string) => {
    try {
      const response = await runtimeFetch(`${runtime}/api/companion/notifications/${identity}/read`, { method: "POST" });
      if (!response.ok) throw new Error("标记通知失败");
      setError(""); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "标记通知失败"); }
  };

  const reminderState = (item: Reminder) => {
    if (item.read_at) return "已查看";
    if (item.delivered_at) return "已投递，未查看";
    if (new Date(item.due_at).getTime() <= now) return quietNow ? "安静时段延后" : "已到期，等待桌宠投递";
    return "待提醒";
  };

  return (
    <section id="reminders" className="panel reminder-panel">
      <div className="section-title"><h2>提醒与陪伴</h2><span>安静时段内延后提醒</span></div>
      <div className="reminder-form">
        <input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="提醒内容" maxLength={300} />
        <input type="datetime-local" value={due} onChange={(event) => setDue(event.target.value)} />
        <button onClick={add} disabled={!title.trim() || !due}>添加提醒</button>
      </div>
      <div className="quiet-form">
        <span>安静时段</span>
        <input type="time" value={quietStart} onChange={(event) => setQuietStart(event.target.value)} />
        <span>至</span>
        <input type="time" value={quietEnd} onChange={(event) => setQuietEnd(event.target.value)} />
        <button onClick={saveQuiet}>保存</button>
      </div>
      {error && <p className="reminder-error">{error}</p>}
      {items.length === 0 ? <p className="reminder-empty">暂无提醒</p> : <ul className="reminder-list">{items.map((item) =>
        <li key={item.id}><span>{item.title}<small>{new Date(item.due_at).toLocaleString("zh-CN")} · {reminderState(item)}</small></span><button onClick={() => remove(item.id)}>删除</button></li>
      )}</ul>}
      <div className="reminder-history-title"><h3>通知记录</h3><small>由桌宠接收；此处只查看记录</small></div>
      {notifications.length === 0 ? <p className="reminder-empty">暂无通知</p> : <ul className="reminder-history">{notifications.map((item) =>
        <li key={item.id}>
          <span>{item.type === "reminder" ? "提醒 · " : item.type === "question" ? "主动问候 · " : "进展 · "}{item.text}<small>{new Date(item.delivered_at).toLocaleString("zh-CN")} · {item.read_at ? "已查看" : "未查看"}</small></span>
          {!item.read_at && <button type="button" onClick={() => void markRead(item.id)}>标记已查看</button>}
        </li>
      )}</ul>}
    </section>
  );
}
