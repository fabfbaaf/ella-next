import { invoke } from "@tauri-apps/api/core";
import { useEffect, useState } from "react";
import { runtimeEnsure, runtimeStatus, type RuntimeStatus } from "../../runtime-client";
import type { RuntimeOverview } from "./useRuntimeOverview";

const taskStates: Record<string, string> = {
  waiting_approval: "待确认", ready: "待执行", running: "执行中",
  needs_reconciliation: "待核对", complete: "已完成", failed: "执行失败",
};
const shortcuts = [
  ["chat", "01", "和艾拉聊聊", "测试回复，继续最近的对话", "开始聊天"],
  ["tasks", "02", "把事情交给艾拉", "写下目标，查看计划和执行结果", "创建任务"],
  ["voice", "03", "设置语音互动", "检查识别、播报和实时监听", "查看语音"],
  ["games", "04", "一起玩游戏", "查看插件连接，设定游玩目标", "查看游戏"],
] as const;

export function OverviewPanel({ snapshot }: { snapshot: RuntimeOverview }) {
  const [showSetup, setShowSetup] = useState(() => localStorage.getItem("ella-setup-dismissed") !== "true");
  const [service, setService] = useState<RuntimeStatus | null>(null);
  const [serviceBusy, setServiceBusy] = useState(false);
  const [serviceError, setServiceError] = useState("");
  const [shortcutErrors, setShortcutErrors] = useState<string[]>([]);
  useEffect(() => { if ("__TAURI_INTERNALS__" in window) void invoke<string[]>("shortcut_status").then(setShortcutErrors).catch(() => {}); }, []);
  useEffect(() => { let active = true; const refresh = () => { void runtimeStatus().then((value) => { if (active) setService(value); }).catch(() => {}); }; refresh(); const timer = window.setInterval(refresh, 5000); return () => { active = false; window.clearInterval(timer); }; }, []);
  const ensure = async (restart: boolean) => { setServiceBusy(true); setServiceError(""); try { setService(await runtimeEnsure(restart)); } catch (reason) { setServiceError(reason instanceof Error ? reason.message : String(reason)); } finally { setServiceBusy(false); } };
  const { health, models, voice, tasks, games, notifications, errors } = snapshot;
  const attention = tasks?.filter((task) => ["waiting_approval", "ready", "needs_reconciliation", "failed"].includes(task.state));
  const running = tasks?.filter((task) => task.state === "running");
  const connected = games?.filter((game) => game.bridge === "connected").length;
  const unread = notifications?.filter((item) => !item.read_at);
  const greeting = new Date().getHours() < 12 ? "早上好" : new Date().getHours() < 18 ? "下午好" : "晚上好";
  return <div className="overview-content">
    <section className="overview-welcome">
      <div><span className="welcome-label">YOUR EVERYDAY COMPANION</span><h2>{greeting}，今天一起做点什么？</h2><p>聊聊近况、完成工作，或去游戏里探索。艾拉的状态都在这里。</p><a className="welcome-action" href="#chat">开始一段对话 <span aria-hidden="true">↗</span></a></div>
      <div className="welcome-art" aria-hidden="true"><div className="art-orbit" /><div className="art-orbit second" /><div className="art-core">艾</div><span className="art-spark one">✦</span><span className="art-spark two">✦</span></div>
    </section>
    {showSetup && <section className="panel">
      <div className="section-title"><h2>首次使用检查</h2><button className="text-button" onClick={() => { localStorage.setItem("ella-setup-dismissed", "true"); setShowSetup(false); }}>收起提示</button></div>
      <div className="catalog-grid">
        <a className="catalog-card" href="#models"><h3>1 · 聊天模型</h3><p>{models?.chat?.configured ? "配置齐全，请检查连接并试聊" : "填写服务地址、模型和密钥"}</p></a>
        <a className="catalog-card" href="#voice"><h3>2 · 语音互动</h3><p>检查麦克风，分别设置识别与合成，再试听音色</p></a>
        <a className="catalog-card" href="#games"><h3>3 · 游戏接入</h3><p>查看游戏检测与模组状态；进入存档后确认实时连接</p></a>
      </div>
      <p className="memory-note">配置齐全表示字段已填写，实际效果以连接检查、试聊和语音试听为准。</p>
    </section>}
    <section className="overview-stats" aria-label="实时状态">
      <a href="#models" className="overview-stat"><span>聊天模型</span><strong>{!models ? "未读取" : models.chat?.configured ? "已配置" : "待配置"}</strong><small>{models?.chat?.model || "在模型页配置主聊天模型"}</small><i className={models?.chat?.configured ? "is-ready" : ""} /></a>
      <a href="#voice" className="overview-stat"><span>语音交互</span><strong>{!voice ? "未读取" : voice.can_transcribe && voice.can_speak ? "配置齐全" : "待完善"}</strong><small>{voice ? `识别${voice.can_transcribe ? "已配置" : "待配置"} · 播报${voice.can_speak ? "已配置" : "待配置"}` : "查看语音服务状态"}</small><i className={voice?.can_transcribe && voice.can_speak ? "is-ready" : ""} /></a>
      <a href="#tasks" className="overview-stat"><span>待处理任务</span><strong>{attention ? attention.length : "—"}<em> 项</em></strong><small>{running?.length ? `${running.length} 项正在执行` : "计划确认、结果核对与失败重试"}</small></a>
      <a href="#games" className="overview-stat"><span>游戏连接</span><strong>{connected ?? "—"}<em> / {games?.length ?? 3}</em></strong><small>进入存档后查看插件连接</small></a>
    </section>
    {errors.length > 0 && <div className="overview-warning" role="status"><strong>{health ? "部分状态暂时无法读取" : "尚未连接本地运行时"}</strong><p>{health ? `无法读取：${errors.join("、")}。稍后自动重试。` : "桌面端会自动启动本地运行时。可以在下方查看原因并重试，连接恢复后自动更新。"}</p></div>}
    {shortcutErrors.length > 0 && <div className="overview-warning" role="status">{shortcutErrors.map((error) => <p key={error}>{error}</p>)}</div>}
    <section className="panel runtime-service"><div className="section-title"><h2>本地运行时</h2><span>{service?.state === "running" ? "已连接" : service?.state === "starting" ? "启动中" : "等待连接"}</span></div><p>{service?.error || serviceError || (service?.owned ? "由桌面端管理，异常退出后自动恢复。" : "复用已启动的服务。")}</p>{service && <small>日志：{service.log_path}</small>}<div className="application-card-actions"><button disabled={serviceBusy} onClick={() => void ensure(false)}>{serviceBusy ? "处理中…" : "重试连接"}</button><button disabled={serviceBusy || !service?.owned} onClick={() => void ensure(true)}>重启运行时</button></div></section>
    <section className="overview-section"><div className="overview-section-heading"><h2>从这里开始</h2><span>常用功能，直接进入</span></div><div className="overview-shortcuts">{shortcuts.map(([page, number, title, description, action]) => <a key={page} href={`#${page}`} className="shortcut-card"><span className="shortcut-number">{number}</span><h3>{title}</h3><p>{description}</p><span className="shortcut-action">{action} <b aria-hidden="true">↗</b></span></a>)}</div></section>
    <div className="overview-bottom-grid">
      <section className="panel overview-list-panel"><div className="section-title"><h2>需要你处理</h2><a href="#tasks">全部任务 ↗</a></div>
        {attention === undefined ? <p className="overview-empty">任务状态暂不可用，连接恢复后更新。</p> : attention.length === 0 ? <div className="overview-empty"><strong>暂时没有待处理任务</strong><p>有新的计划或需要核对的结果时，会出现在这里。</p></div> : <ul className="overview-task-list">{attention.slice(0, 4).map((task) => <li key={task.id}><a href="#tasks"><span>{task.goal}</span><small>{task.error || "打开任务页查看计划与结果"}</small></a><em data-state={task.state}>{taskStates[task.state]}</em></li>)}</ul>}
      </section>
      <section className="panel overview-list-panel"><div className="section-title"><h2>通知与提醒</h2><a href="#reminders">查看记录 ↗</a></div>
        {unread === undefined ? <p className="overview-empty">通知记录暂不可用。</p> : unread.length === 0 ? <div className="overview-empty"><strong>没有未读通知</strong><p>提醒与工作进度保留在后台，桌面人物不会被文字遮挡。</p></div> : <ul className="overview-notice-list">{unread.slice(0, 3).map((item) => <li key={item.id}><p>{item.text}</p><small>{new Date(item.delivered_at).toLocaleString("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</small></li>)}</ul>}
      </section>
    </div>
  </div>;
}
