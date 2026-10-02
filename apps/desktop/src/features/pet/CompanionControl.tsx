import { useCallback, useEffect, useState } from "react";
import { runtimeFetch } from "../../runtime-client";
import { useSpokenNotifications } from "./SpokenNotifications";
import type { SpokenNotification } from "./notification-speech";
import "./companion.css";

type Status = { hunger: number; hungry: boolean; quiet_now: boolean };
type Notification = SpokenNotification;
type Props = { menuOpen: boolean; onCloseMenu: () => void; onOpenAdmin: () => void };
const runtime = "http://127.0.0.1:8766";

export function CompanionControl({ menuOpen, onCloseMenu, onOpenAdmin }: Props) {
  const [status, setStatus] = useState<Status | null>(null);
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [offline, setOffline] = useState(false);
  useSpokenNotifications(notifications, status?.quiet_now ?? true);
  const loadNotifications = useCallback(async () => {
    const response = await runtimeFetch(`${runtime}/api/companion/notifications`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json() as Promise<Notification[]>;
  }, []);
  useEffect(() => {
    let active = true;
    let inFlight = false;
    const refresh = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const state = await runtimeFetch(`${runtime}/api/companion/status`);
        if (!state.ok) throw new Error(`HTTP ${state.status}`);
        const current = await state.json() as Status;
        // Consume progress and due reminders into durable notification history.
        // Speech waits for a microphone lease; durable records remain in the menu and admin.
        const polled = await runtimeFetch(`${runtime}/api/companion/poll`, { method: "POST" });
        if (!polled.ok) throw new Error(`HTTP ${polled.status}`);
        const items = await loadNotifications();
        if (!active) return;
        setNotifications(items);
        setStatus(current);
        setOffline(false);
      } catch { if (active) setOffline(true); }
      finally { inFlight = false; }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 10_000);
    return () => { active = false; window.clearInterval(timer); };
  }, [loadNotifications]);
  const feed = async () => {
    setBusy(true);
    try {
      const response = await runtimeFetch(`${runtime}/api/companion/feed`, { method: "POST" });
      if (!response.ok) throw new Error();
      setStatus(await response.json() as Status);
      setMessage("谢谢你喂我！");
    } catch { setMessage("现在没连上运行时，稍后再试。"); }
    finally { setBusy(false); }
  };
  const markRead = async (identity: string) => {
    try {
      const response = await runtimeFetch(`${runtime}/api/companion/notifications/${identity}/read`, { method: "POST" });
      if (!response.ok) throw new Error();
      setNotifications(await loadNotifications());
      setMessage("");
    } catch { setMessage("通知状态保存失败，稍后再试。"); }
  };
  const unread = notifications.filter((item) => !item.read_at).length;
  return menuOpen ? <section className="pet-context-menu" aria-label="艾拉菜单">
    <div className="pet-menu-title">艾拉 · {offline ? "运行时未连接" : status ? `饱腹 ${status.hunger}%` : "连接中"}</div>
    {status?.hungry && <small>有点饿啦</small>}
    {status?.quiet_now && <small>安静时段，提醒会延后</small>}
    <div className="pet-menu-actions"><button type="button" disabled={busy || !status} onClick={() => void feed()}>喂食</button><button type="button" onClick={() => { onCloseMenu(); onOpenAdmin(); }}>打开后台</button></div>
    {message && <small role="status">{message}</small>}
    <strong>通知记录{unread ? ` · ${unread} 条未查看` : ""}</strong>
    {notifications.length === 0 ? <small>暂无通知</small> : <ul className="pet-notification-list">{notifications.map((item) => <li key={item.id}><span>{item.type === "reminder" ? "提醒：" : ""}{item.text}</span><small>{new Date(item.delivered_at).toLocaleString("zh-CN")}</small>{!item.read_at && <button type="button" onClick={() => void markRead(item.id)}>标记已查看</button>}</li>)}</ul>}
  </section> : null;
}
