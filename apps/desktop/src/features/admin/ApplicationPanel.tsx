import { useEffect, useRef, useState } from "react";
import { runtimeFetch } from "../../runtime-client";
import "./application.css";

type BrowserState = {channel: string; connected: boolean; dependency_ready: boolean; installed: Record<string, boolean>; auto_web: boolean; url: string | null; last_error?: string; last_query?: string; last_sources?: Link[]};
type Link = {title: string; url: string; snippet?: string};
type Page = {url: string; title: string; text: string; truncated: boolean; links: Link[]; results?: Link[]; captured_at: string};
const applications = [
  { id: "excel", name: "Excel", capability: "读取和修改已打开工作簿的单元格、公式，也可创建新工作簿", goal: "帮我编辑已打开的 Excel 工作簿；先确认文件名、工作表和要改的内容" },
  { id: "word", name: "Word", capability: "替换已打开文档的文本，也可创建新文档", goal: "帮我编辑已打开的 Word 文档；先确认文件名和要改的内容" },
  { id: "code", name: "编程项目", capability: "列出、读取和精确修改项目文件，运行检查并查看 Git 差异", goal: "帮我处理当前编程项目；先确认工作区路径和目标，保留改动与差异，不要提交" },
];
const runtime = "http://127.0.0.1:8766";
function safeLink(value: string) { try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) ? url.href : undefined; } catch { return undefined; } }

export function ApplicationPanel() {
  const [busy, setBusy] = useState<string | null>(null);
  const lock = useRef(false);
  const [error, setError] = useState("");
  const [browser, setBrowser] = useState<BrowserState | null>(null);
  const [installed, setInstalled] = useState<Record<string, {installed: boolean; detail: string}>>({});
  const [address, setAddress] = useState("https://www.bing.com");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState<Page | null>(null);
  const refresh = async () => {
    const response = await runtimeFetch(`${runtime}/api/browser/status`);
    if (!response.ok) throw new Error("无法读取浏览器状态");
    setBrowser(await response.json() as BrowserState);
  };
  useEffect(() => {
    let active = true;
    const read = async () => {
      try {
        const response = await runtimeFetch(`${runtime}/api/browser/status`);
        if (!response.ok) throw new Error("无法读取浏览器状态");
        const value = await response.json() as BrowserState;
        if (active) { setBrowser(value); }
        const catalog = await runtimeFetch(`${runtime}/api/applications/catalog`);
        if (catalog.ok && active) setInstalled(await catalog.json());
      } catch (reason) { if (active) setError(reason instanceof Error ? reason.message : "运行时未连接"); }
    };
    void read(); const timer = window.setInterval(() => void read(), 8000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);
  const operate = async (id: string, action: () => Promise<void>) => {
    if (lock.current) return;
    lock.current = true; setBusy(id); setError("");
    try { await action(); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "应用操作失败"); }
    finally { lock.current = false; setBusy(null); }
  };
  const request = async (path: string, body?: unknown, method = "POST") => {
    const response = await runtimeFetch(`${runtime}${path}`, {method, headers: {"Content-Type": "application/json"}, body: body === undefined ? undefined : JSON.stringify(body)});
    const data = response.status === 204 ? {} : await response.json();
    if (!response.ok) throw new Error(data.detail || "请求失败");
    return data;
  };
  const useTask = (goal: string) => { sessionStorage.setItem("ella-task-draft", goal); window.location.hash = "tasks"; };
  const settings = (channel: string, auto_web: boolean) => operate("settings", async () => { setBrowser(await request("/api/browser/settings", {channel, auto_web}, "PUT")); });
  return <section className="panel application-panel">
    <div className="section-title"><h2>应用与联网</h2><span>查看真实连接，直接开始工作</span></div>
    {error && <p className="usage-error" role="alert">{error}</p>}
    <div className="browser-workspace">
      <div className="browser-heading"><div><span className="browser-eyebrow">WEB ACCESS</span><h3>让艾拉去网上看看</h3></div><em className={browser?.connected ? "connected" : ""}>{browser?.connected ? "浏览器已连接" : "浏览器未启动"}</em></div>
      <p>使用艾拉独立的浏览会话，登录状态会保留。可以手动搜索，也可以在聊天或语音中说“搜索…”或“查一下…”。</p>
      <div className="browser-settings"><label>使用浏览器<select aria-label="使用浏览器" disabled={busy !== null} value={browser?.channel || "auto"} onChange={(event) => void settings(event.target.value, browser?.auto_web ?? true)}><option value="auto">自动选择 Edge / Chrome</option><option value="msedge">Microsoft Edge</option><option value="chrome">Google Chrome</option></select></label><label className="browser-auto"><input type="checkbox" disabled={!browser || busy !== null} checked={browser?.auto_web ?? true} onChange={(event) => void settings(browser?.channel || "auto", event.target.checked)} />聊天和语音按需联网</label></div>
      {browser && <small>{browser.dependency_ready ? "浏览器控制组件已安装" : "缺少浏览器控制组件，请安装 runtime[browser]"} · Edge {browser.installed.msedge ? "已安装" : "未找到"} · Chrome {browser.installed.chrome ? "已安装" : "未找到"}</small>}
      <form className="browser-search" onSubmit={(event) => { event.preventDefault(); void operate("search", async () => setPage(await request("/api/browser/search", {query: query.trim()}))); }}><input aria-label="搜索内容" disabled={busy !== null} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="想查什么？例如：星露谷温室解锁条件" maxLength={300} /><button disabled={busy !== null || !query.trim()}>{busy === "search" ? "正在搜索…" : "上网搜索"}</button></form>
      <form className="browser-address" onSubmit={(event) => { event.preventDefault(); void operate("open", async () => setPage(await request("/api/browser/open", {url: address.trim()}))); }}><input aria-label="网址" disabled={busy !== null} value={address} onChange={(event) => setAddress(event.target.value)} placeholder="https://…" /><button disabled={busy !== null || !address.trim()}>打开网址</button><button type="button" disabled={busy !== null || !browser?.connected} onClick={() => void operate("read", async () => setPage(await request("/api/browser/page", undefined, "GET")))}>读取当前网页</button></form>
      {browser?.last_error && <p className="usage-error">{browser.last_error}</p>}
      {page && <div className="browser-result"><div className="section-title"><h3>{page.title || "网页内容"}</h3><small>{new Date(page.captured_at).toLocaleString()}</small></div><a href={safeLink(page.url)} target="_blank" rel="noreferrer">{page.url}</a>{page.results && <ul>{page.results.map((result, index) => <li key={`${result.url}-${index}`}><a href={safeLink(result.url)} target="_blank" rel="noreferrer">{result.title}</a><p>{result.snippet}</p></li>)}</ul>}<details open={!page.results}><summary>读取到的网页正文{page.truncated ? "（部分）" : ""}</summary><pre>{page.text}</pre></details><button className="text-button" disabled={busy !== null} onClick={() => useTask(`阅读网页 ${page.url}，根据实际内容完成我的目标：`)}>带着网页创建任务</button></div>}
      {!!browser?.last_sources?.length && <details className="browser-recent"><summary>聊天／语音最近一次联网来源：{browser.last_query}</summary>{browser.last_sources.map((source, index) => <p key={index}><a href={safeLink(source.url)} target="_blank" rel="noreferrer">{source.title || source.url}</a></p>)}</details>}
      <div className="application-card-actions"><button disabled={busy !== null} onClick={() => void operate("browser", async () => { await request("/api/applications/browser/launch"); })}>打开受控浏览器</button><button className="secondary" disabled={busy !== null || !browser?.connected} onClick={() => void operate("close", async () => { await request("/api/browser/close"); setPage(null); })}>关闭浏览器</button></div>
    </div>
    <div className="catalog-grid">{applications.map((item) => <article className="catalog-card" key={item.id}><h3>{item.name}</h3><p>{item.capability}</p><span>{installed[item.id]?.detail || "正在读取安装状态"}</span><div className="application-card-actions"><button disabled={busy !== null || installed[item.id]?.installed === false} onClick={() => void operate(item.id, async () => { await request(item.id === "code" ? "/api/workspaces/code/open" : `/api/applications/${item.id}/launch`); useTask(item.goal); })}>{busy === item.id ? "启动中…" : "启动并工作"}</button><button className="secondary" onClick={() => useTask(item.goal)}>创建工作任务</button></div></article>)}</div>
  </section>;
}
