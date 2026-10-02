import { useEffect, useState } from "react";
import { BackupPanel } from "./BackupPanel";
import { UsagePanel } from "./UsagePanel";
import { MemoryPanel } from "./MemoryPanel";
import { TaskPanel } from "./TaskPanel";
import { ReminderPanel } from "./ReminderPanel";
import { GamePanel } from "./GamePanel";
import { ModelStatusPanel } from "./ModelStatusPanel";
import { ApplicationPanel } from "./ApplicationPanel";
import { VoicePanel } from "./VoicePanel";
import { OverviewPanel } from "./OverviewPanel";
import { useRuntimeOverview } from "./useRuntimeOverview";
import { ChatControl } from "../pet/ChatControl";
import "./admin-chat.css";
import "./admin.css";

const pages = [
  ["overview", "总览", "HOME", "今天的艾拉", "随时看看状态，开始一段对话或一项新任务。", "M3 10.5 12 3l9 7.5V21h-6v-7H9v7H3Z"],
  ["chat", "聊天测试", "CONVERSATION", "和艾拉聊聊", "在这里测试回复、继续对话，看看艾拉记住了什么。", "M21 11.5a8.5 8.5 0 0 1-8.5 8.5H3l2-5a8.5 8.5 0 1 1 16-3.5Z"],
  ["voice", "语音互动", "VOICE", "让对话自然一点", "检查语音服务，选择实时监听的方式。", "M12 3a3 3 0 0 1 3 3v6a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3Zm-7 8v1a7 7 0 0 0 14 0v-1M12 19v3m-4 0h8"],
  ["tasks", "任务工作台", "WORKSPACE", "把事情交给艾拉", "写下目标，核对计划，查看每一步的执行结果。", "M9 5H5v16h14V5h-4M9 3h6v4H9Zm-1 9h8m-8 4h6"],
  ["applications", "应用协作", "APPLICATIONS", "一起完成工作", "从浏览器、文档或编程项目开始。", "M3 4h18v13H3Zm5 17h8m-4-4v4"],
  ["games", "游戏伙伴", "GAMES", "一起去探索", "查看游戏插件连接，给艾拉一个游玩目标。", "M7 8h10a4 4 0 0 1 4 4v5a2 2 0 0 1-3.5 1.3L15 16H9l-2.5 2.3A2 2 0 0 1 3 17v-5a4 4 0 0 1 4-4Zm0 3v4m-2-2h4m6-1h.01m3 2h.01"],
  ["models", "模型与人格", "MODELS", "配置艾拉的大脑", "分别配置聊天、操作、视觉与人格模型。", "M12 3 3 7.5 12 12l9-4.5Zm-9 9L12 17l9-5m-18 5 9 5 9-5"],
  ["memory", "长期记忆", "MEMORY", "艾拉记得的事", "查看来源、修正内容，让记忆保持准确。", "M4 4h6a4 4 0 0 1 4 4v13a4 4 0 0 0-4-2H4Zm10 4a4 4 0 0 1 4-4h3v15h-3a4 4 0 0 0-4 2"],
  ["reminders", "提醒与陪伴", "COMPANION", "照顾日常的小事", "管理提醒、安静时段和通知记录。", "M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9Zm-8 12a2 2 0 0 0 4 0"],
  ["backup", "数据备份", "BACKUP", "把日常记忆带走", "导出对话、记忆和服务配置，核对内容后恢复。", "M4 4h16v16H4Zm4 0v6h8V4M8 20v-6h8v6"],
  ["usage", "用量记录", "USAGE", "每一次调用都清楚", "按模型、用途、日期与任务核对用量。", "M4 3v18h17M8 17v-5m5 5V7m5 10V4"],
] as const;
type Page = typeof pages[number][0];
const pageIds = new Set<string>(pages.map(([id]) => id));
function currentPage(): Page {
  const hash = window.location.hash.slice(1);
  return (pageIds.has(hash) ? hash : "overview") as Page;
}
export function AdminWindow() {
  const [page, setPage] = useState<Page>(currentPage);
  const { snapshot, loading, refresh } = useRuntimeOverview();
  useEffect(() => {
    const changed = () => setPage(currentPage());
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);
  const active = pages.find(([id]) => id === page)!;
  const online = snapshot.health?.status === "ok";
  return <main className="admin-window">
    <aside className="sidebar">
      <a className="brand" href="#overview" aria-label="艾拉首页"><div className="brand-mark">✦</div><div><div className="brand-name">艾拉 <span>Next</span></div><div className="brand-subtitle">你的日常伙伴</div></div></a>
      <nav aria-label="管理模块">{pages.map(([id, label, , , , icon], index) => <div key={id}>
        {(index === 0 || index === 3 || index === 6) && <span className="nav-group-label">{index === 0 ? "日常互动" : index === 3 ? "工作与游戏" : "设置与记录"}</span>}
        <a href={`#${id}`} className={page === id ? "active" : ""} aria-current={page === id ? "page" : undefined}><svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={icon} /></svg><span>{label}</span>{id === "tasks" && Boolean(snapshot.tasks?.some((task) => task.state === "waiting_approval" || task.state === "needs_reconciliation")) && <i className="nav-alert" />}</a>
      </div>)}</nav>
      <div className="sidebar-bottom"><div className={`runtime-indicator ${online ? "online" : ""}`}><i /><span>{snapshot.updatedAt === null ? "正在连接" : online ? "本地运行时已连接" : "运行时未连接"}</span></div><small>{snapshot.health?.version ? `v${snapshot.health.version}` : "Ella Next"} · 私人工作空间</small></div>
    </aside>
    <div className="admin-content">
      <header className="admin-page-header"><div><span className="eyebrow">{active[2]}</span><h1>{active[3]}</h1><p>{active[4]}</p></div><div className="admin-header-actions"><span className="admin-update-time">{snapshot.updatedAt ? `${new Date(snapshot.updatedAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })} 更新` : "获取状态中"}</span><button type="button" className="refresh-button" onClick={() => void refresh()} disabled={loading}><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5M5 7a8 8 0 0 1 14-1l1 6M4 12l1 6a8 8 0 0 0 14-1" /></svg>{loading ? "刷新中" : "刷新状态"}</button></div></header>
      {page === "overview" && <OverviewPanel snapshot={snapshot} />}
      {page === "models" && <ModelStatusPanel />}
      {page === "chat" && <ChatControl />}
      {page === "memory" && <MemoryPanel />}
      {page === "voice" && <VoicePanel />}
      {page === "tasks" && <TaskPanel />}
      {page === "applications" && <ApplicationPanel />}
      {page === "games" && <GamePanel />}
      {page === "reminders" && <ReminderPanel />}
      {page === "backup" && <BackupPanel />}
      {page === "usage" && <UsagePanel />}
    </div>
  </main>;
}
