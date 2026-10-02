import { useCallback, useEffect, useRef, useState } from "react";
import { runtimeFetch } from "../../runtime-client";
import "./task.css";
import { taskPresets } from "./task-presets";

type Step = {
  id: string;
  tool: string;
  arguments: Record<string, unknown>;
  reason: string;
  risk_level: string;
  risk_reason: string;
  destination: string | null;
  state: string;
  evidence: Record<string, unknown>;
};
type Task = { id: string; goal: string; state: string; steps: Step[]; error: string | null; plan_hash: string };
type Workspaces = { code: string; files: string };

const runtime = "http://127.0.0.1:8766";
const stateName: Record<string, string> = {
  waiting_approval: "等待确认计划",
  ready: "已确认，等待执行",
  running: "执行中",
  needs_reconciliation: "需要核对现场",
  complete: "已完成",
  failed: "失败",
};

export function TaskPanel() {
  const [goal, setGoal] = useState(() => {
    const draft = sessionStorage.getItem("ella-task-draft") || "";
    return draft;
  });
  const [tasks, setTasks] = useState<Task[]>([]);
  const [autoRunSafe, setAutoRunSafe] = useState(() => localStorage.getItem("ella-auto-run-safe") === "true");
  const [workspaces, setWorkspaces] = useState<Workspaces | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState<{ id: string; hash: string; text: string } | null>(null);
  const mounted = useRef(true);
  const refreshVersion = useRef(0);

  const refresh = useCallback(async () => {
    const version = ++refreshVersion.current;
    const response = await runtimeFetch(`${runtime}/api/tasks`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json() as Task[];
    if (!mounted.current || version !== refreshVersion.current) return;
    setTasks(data);
    setError((current) => current === "运行时未连接" ? "" : current);
  }, []);

  useEffect(() => {
    mounted.current = true;
    sessionStorage.removeItem("ella-task-draft");
    void refresh().catch(() => setError("运行时未连接"));
    void runtimeFetch(`${runtime}/api/workspaces`)
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("工作区状态读取失败")))
      .then((value: Workspaces) => setWorkspaces(value))
      .catch(() => {});
    const timer = window.setInterval(() => void refresh().catch(() => {}), 5000);
    return () => { mounted.current = false; refreshVersion.current += 1; window.clearInterval(timer); };
  }, [refresh]);

  const openWorkspace = async () => {
    try {
      const response = await runtimeFetch(`${runtime}/api/workspaces/code/open`, { method: "POST" });
      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || "打开编程工作区失败");
      }
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "打开编程工作区失败");
    }
  };

  const submit = async () => {
    if (!goal.trim()) return;
    setBusy(true);
    try {
      const response = await runtimeFetch(`${runtime}/api/tasks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ goal: goal.trim(), auto_run_safe: autoRunSafe }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "规划失败");
      setGoal("");
      setError("");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "规划失败");
    } finally { setBusy(false); }
  };

  const action = async (task: Task, command: "approve" | "run" | "reconcile") => {
    setBusy(true);
    try {
      const response = await runtimeFetch(`${runtime}/api/tasks/${task.id}/${command}`, {
        method: "POST",
        ...(command === "approve" ? {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ plan_hash: task.plan_hash }),
        } : {}),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "任务操作失败");
      setError("");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "任务操作失败");
    } finally { setBusy(false); }
  };

  const revise = async () => {
    if (!revision) return;
    setBusy(true); setError("");
    try {
      const steps: unknown = JSON.parse(revision.text);
      if (!Array.isArray(steps)) throw new Error("计划必须是步骤数组");
      const response = await runtimeFetch(`${runtime}/api/tasks/${revision.id}/plan`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan_hash: revision.hash, steps }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "计划修订失败");
      setRevision(null); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "计划修订失败"); }
    finally { setBusy(false); }
  };
  return (
    <section id="tasks" className="panel task-panel">
      <div className="section-title"><h2>任务 Agent</h2><span>先看风险与参数，再决定是否执行</span></div>
      {workspaces && <div className="task-workspaces">
        <span>编程工作区：{workspaces.code}</span>
        <button type="button" onClick={() => void openWorkspace()}>打开目录</button>
        <small>普通文件输出：{workspaces.files}</small>
      </div>}
      <div className="task-create">
        <input disabled={busy} value={goal} onChange={(event) => setGoal(event.target.value)} placeholder="例如：在工作区创建一份说明文档" maxLength={2000} />
        <button disabled={busy || !goal.trim()} onClick={submit}>{autoRunSafe ? "生成计划并执行安全步骤" : "只生成计划"}</button>
      </div>
      <div className="task-presets"><span className="task-presets-label">试试这些目标</span>{taskPresets.map((preset) => <button type="button" key={preset.label} disabled={busy} onClick={() => setGoal(preset.goal)}>{preset.label}</button>)}</div>
      <label className="task-auto-setting">
        <input type="checkbox" checked={autoRunSafe} onChange={(event) => {
          setAutoRunSafe(event.target.checked);
          localStorage.setItem("ella-auto-run-safe", String(event.target.checked));
        }} />
        自动执行确认属于低风险的计划（仅只读操作或新建普通文本）；默认关闭
      </label>
      <p className="task-help">网页填写可能自动保存；脚本文件、覆盖操作和未知参数需先确认。批准时会核对你看到的计划，执行时再次核对参数。每一步都保存核验结果。</p>
      {error && <p className="task-error">{error}</p>}
      {tasks.length === 0 && <p className="task-empty">暂无任务。配置聊天或操作模型后可生成计划。</p>}
      <div className="task-list">
        {tasks.map((task) => <article className="task-item" key={task.id}>
          <div className="task-heading"><strong>{task.goal}</strong><span>{stateName[task.state] || task.state}</span></div>
          {task.error && <p className="task-error">{task.error}</p>}
          <ol>{task.steps.map((step) => <li key={step.id}>
            <div><b>{step.tool}</b><span>{step.state}</span></div>
            {step.reason && <p>{step.reason}</p>}
            <p className={`task-risk task-risk-${step.risk_level}`}>
              {step.risk_level === "low" ? "低风险" : step.risk_level === "high" ? "高影响 · 需确认" : "需确认"}：{step.risk_reason}
            </p>
            {step.destination && <p className="task-destination">目标域名：<b>{step.destination}</b></p>}
            {step.tool === "browser.fill" && <p className="task-destination">输入框：{String(step.arguments.selector ?? "未指定")}；将填写：{String(step.arguments.value ?? "未指定")}</p>}
            <pre>{JSON.stringify(step.arguments, null, 2)}</pre>
            {Object.keys(step.evidence).length > 0 && <small>核验：{JSON.stringify(step.evidence)}</small>}
          </li>)}</ol>
          {revision?.id === task.id && <div className="task-revision">
            <p>只修改未执行步骤。已完成步骤及其参数必须保留原样；保存后需要重新确认计划。</p>
            <textarea aria-label="修订任务步骤" disabled={busy} value={revision.text} onChange={(event) => setRevision({ ...revision, text: event.target.value })} rows={12} maxLength={50000} style={{ width: "100%", fontFamily: "monospace" }} />
            <button disabled={busy} onClick={() => void revise()}>保存修订，重新确认</button>
            <button disabled={busy} onClick={() => setRevision(null)}>放弃修改</button>
          </div>}
          <div className="task-buttons">
            {["failed", "waiting_approval", "ready"].includes(task.state) && <button disabled={busy} onClick={() => setRevision({ id: task.id, hash: task.plan_hash, text: JSON.stringify(task.steps.map(({ tool, arguments: args, reason }) => ({ tool, arguments: args, reason })), null, 2) })}>修订未执行步骤</button>}
            {task.state === "waiting_approval" && <button disabled={busy} onClick={() => action(task, "approve")}>{task.steps.some((step) => step.risk_level === "high") ? "确认高影响计划" : "确认这份计划"}</button>}
            {task.state === "ready" && <button disabled={busy} onClick={() => action(task, "run")}>执行并核验</button>}
            {task.state === "needs_reconciliation" && <button disabled={busy} onClick={() => action(task, "reconcile")}>核对现场</button>}
          </div>
        </article>)}
      </div>
    </section>
  );
}
