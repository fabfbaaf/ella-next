import { useEffect, useState } from "react";
import { runtimeFetch } from "../../runtime-client";

type UsageNumbers = {
  requests: number;
  reported_requests: number;
  unreported_requests: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
};

type UsageGroup = UsageNumbers & {
  provider: string;
  model: string;
  purpose: string;
  task_id: string | null;
};

type UsageSummary = { totals: UsageNumbers; groups: UsageGroup[] };

const runtimeUrl = "http://127.0.0.1:8766";
const number = (value: number) => new Intl.NumberFormat("zh-CN").format(value);

export function UsagePanel() {
  const [summary, setSummary] = useState<UsageSummary | null>(null);
  const [error, setError] = useState("");
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [taskId, setTaskId] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      const query = new URLSearchParams();
      if (fromDate) query.set("from_date", fromDate);
      if (toDate) query.set("to_date", toDate);
      if (taskId.trim()) query.set("task_id", taskId.trim());
      try {
        const response = await runtimeFetch(`${runtimeUrl}/api/usage/summary?${query}`, {
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        setSummary((await response.json()) as UsageSummary);
        setError("");
      } catch (caught) {
        if (controller.signal.aborted) return;
        setSummary(null);
        setError(caught instanceof Error ? caught.message : "读取失败");
      }
    };
    void load();
    const interval = window.setInterval(() => void load(), 15_000);
    return () => { controller.abort(); window.clearInterval(interval); };
  }, [fromDate, toDate, taskId]);

  const totals = summary?.totals;
  return (
    <section id="usage" className="panel usage-panel">
      <div className="section-title"><h2>Token 用量</h2><span>提供方返回的实际用量</span></div>
      <div className="usage-filters">
        <label>起始日期<input type="date" value={fromDate} onChange={(event) => setFromDate(event.target.value)} /></label>
        <label>截止日期<input type="date" value={toDate} onChange={(event) => setToDate(event.target.value)} /></label>
        <label>任务 ID<input type="text" value={taskId} onChange={(event) => setTaskId(event.target.value)} placeholder="全部任务" /></label>
      </div>
      {error && <p className="usage-error">运行时未连接或读取失败：{error}</p>}
      {totals && <>
        <div className="usage-totals">
          <div><span>已报告总量</span><strong>{number(totals.input_tokens + totals.output_tokens)}</strong></div>
          <div><span>输入 / 输出</span><strong>{number(totals.input_tokens)} / {number(totals.output_tokens)}</strong></div>
          <div><span>缓存读取</span><strong>{number(totals.cache_read_tokens)}</strong></div>
          <div><span>未知用量请求</span><strong>{number(totals.unreported_requests)}</strong></div>
        </div>
        {summary!.groups.length === 0 ? <p className="usage-empty">暂无模型调用记录。</p> :
          <div className="usage-table-wrap"><table className="usage-table"><thead><tr><th>模型</th><th>用途</th><th>任务</th><th>输入</th><th>输出</th><th>未知</th></tr></thead><tbody>
            {summary!.groups.map((group, index) => <tr key={`${group.provider}-${group.model}-${group.purpose}-${group.task_id}-${index}`}>
              <td>{group.provider} / {group.model}</td><td>{group.purpose}</td><td>{group.task_id || "—"}</td>
              <td>{number(group.input_tokens)}</td><td>{number(group.output_tokens)}</td><td>{number(group.unreported_requests)}</td>
            </tr>)}
          </tbody></table></div>}
        <p className="usage-footnote">缓存读取已包含在输入 Token 中；未知用量不会按 0 计入已报告总量。</p>
      </>}
    </section>
  );
}
