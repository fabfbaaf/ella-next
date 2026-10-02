import { useEffect, useRef, useState } from "react";
import { runtimeFetch } from "../../runtime-client";
import "./game.css";

const runtime = "http://127.0.0.1:8766";
type PlayStatus = { status: string; goal?: string; step?: number; max_steps?: number; max_decisions?: number; decision_calls?: number; stagnant_steps?: number; last_action?: string; last_result?: string; error?: string; recovery_pending?: boolean; completion_evidence?: Record<string, unknown> };
type GameStatus = { id: string; name: string; bridge: "connected" | "offline" | "not_configured"; ready?: boolean; mode: string; paused: boolean; play: PlayStatus; installation?: {installed: boolean; detail: string; executable: string | null}; launch?: {status: string; remaining_seconds?: number; error?: string} };
type GameSetup = {
  game_id: string;
  state: "not_found" | "needs_selection" | "unsupported" | "running" | "installing" | "ready" | "error" | "blocked";
  detail: string;
  launch_detail?: string;
  game_root?: string | null;
  version?: string | null;
  loader_version?: string | null;
  bundled_loader_version?: string | null;
  pending_files?: Array<{ path: string; reason: "missing" | "changed" }>;
  candidates?: Array<{ path: string; label: string; profile?: string | null; version?: string | null; loader_version?: string | null }>;
  files_changed?: string[];
  backup_dir?: string | null;
  blockers?: string[];
  ready?: boolean;
  detected?: boolean;
  reason?: string;
};
type GameSetupStatus = { games: GameSetup[]; scanning: boolean };
type SetupLocationDraft = { path: string; profile?: string | null; selection?: string };
const candidateValue = (item: { path: string; profile?: string | null }) => JSON.stringify([item.path, item.profile ?? null]);
const setupLabels: Record<GameSetup["state"], string> = {
  not_found: "未发现游戏", needs_selection: "需要选择游戏目录", unsupported: "版本不支持",
  running: "等待游戏关闭", installing: "正在部署插件", ready: "插件已部署",
  error: "接入失败", blocked: "接入受阻",
};
const blockerLabels: Record<string, string> = {
  game_version: "游戏版本与插件不兼容", fabric_0_19_5_required: "需要 Fabric Loader 0.19.5 或更新版本",
  "fabric_0.19.5_required": "需要 Fabric Loader 0.19.5 或更新版本", java_25_required: "需要可用的 Java 25",
  smapi_version_unknown: "无法确认已安装的 SMAPI 版本", smapi_4_5_required: "需要 SMAPI 4.5 或更新版本",
  "smapi_4.5_required": "需要 SMAPI 4.5 或更新版本", blse_required: "缺少骑砍 BLSE 加载器",
  fabric_api_26_2_required: "缺少匹配的 Fabric API", "fabric_api_26.2_required": "缺少匹配的 Fabric API",
  minecraft_26_2_not_installed: "请先在官方启动器安装并运行 Minecraft 26.2",
  "minecraft_26.2_not_installed": "请先在官方启动器安装并运行 Minecraft 26.2",
  minecraft_mod_conflict: "存在另一份艾拉或 Fabric API 模组，请移走重复模组后重新检测",
};
type HighTool = { name: string; group: string; parameters: Record<string, unknown> | null; can_preview: boolean };
type HighCatalog = { save_id: string | null; actions: HighTool[] };
type HighPreview = { preview_id: string; save_id: string; group: string; effect: string; tool: string; arguments: Record<string, unknown>; expires_at: string };

export function GamePanel() {
  const [gameId, setGameId] = useState("minecraft");
  const [windowTitle, setWindowTitle] = useState("");
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [countdown, setCountdown] = useState(0);
  const [catalog, setCatalog] = useState<GameStatus[]>([]);
  const [catalogError, setCatalogError] = useState("");
  const [setupStatus, setSetupStatus] = useState<GameSetupStatus | null>(null);
  const [setupLoadError, setSetupLoadError] = useState("");
  const [setupError, setSetupError] = useState("");
  const [setupBusy, setSetupBusy] = useState<string | null>(null);
  const [setupLocations, setSetupLocations] = useState<Record<string, SetupLocationDraft>>({});
  const setupLock = useRef(false);
  const refreshLock = useRef(false);
  const mounted = useRef(false);
  const [operationBusy, setOperationBusy] = useState<string | null>(null);
  const operationLock = useRef(false);
  const [playGoal, setPlayGoal] = useState("安全地探索当前区域，完成至少 3 个可核验动作；遇到风险就停");
  const [snapshot, setSnapshot] = useState<{ gameId: string; text: string } | null>(null);
  const [highCatalog, setHighCatalog] = useState<HighCatalog | null>(null);
  const [highTool, setHighTool] = useState("");
  const [highArguments, setHighArguments] = useState("{}");
  const [highPreview, setHighPreview] = useState<HighPreview | null>(null);
  const [highResult, setHighResult] = useState("");
  const [highError, setHighError] = useState("");
  const [highBusy, setHighBusy] = useState(false);
  const selectedHighTool = highCatalog?.actions.find((item) => item.name === highTool);

  const setupFailure = async (response: Response) => {
    const data = await response.json().catch(() => ({})) as { detail?: unknown };
    return new Error(typeof data.detail === "string" ? data.detail : `接入状态读取失败（HTTP ${response.status}）`);
  };

  const refresh = async () => {
    if (refreshLock.current) return;
    refreshLock.current = true;
    try {
      const [games, setup] = await Promise.allSettled([
        (async () => {
          const response = await runtimeFetch(`${runtime}/api/games/catalog`);
          if (!response.ok) throw new Error("运行时未连接");
          return response.json() as Promise<GameStatus[]>;
        })(),
        (async () => {
          const response = await runtimeFetch(`${runtime}/api/games/setup`);
          if (!response.ok) throw await setupFailure(response);
          return response.json() as Promise<GameSetupStatus>;
        })(),
      ]);
      if (!mounted.current) return;
      if (games.status === "fulfilled") { setCatalog(games.value); setCatalogError(""); }
      else setCatalogError(games.reason instanceof Error ? games.reason.message : "游戏状态读取失败");
      if (setup.status === "fulfilled") { setSetupStatus(setup.value); setSetupLoadError(""); }
      else setSetupLoadError(setup.reason instanceof Error ? setup.reason.message : "接入状态读取失败");
    } finally { refreshLock.current = false; }
  };
  useEffect(() => {
    mounted.current = true;
    void refresh();
    const interval = window.setInterval(() => { void refresh(); }, 3000);
    return () => { mounted.current = false; window.clearInterval(interval); };
  }, []);

  const configureSetup = async (id?: string) => {
    if (setupLock.current || setupStatus?.scanning) return;
    const draft = id ? setupLocations[id] : undefined;
    const location = draft?.path.trim();
    if (id && !location) return;
    setupLock.current = true;
    setSetupBusy(id || "scan");
    setSetupError("");
    try {
      const response = await runtimeFetch(id ? `${runtime}/api/games/${id}/setup/location` : `${runtime}/api/games/setup/scan`, {
        method: id ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: id ? JSON.stringify({ path: location, ...(draft?.profile ? { profile: draft.profile } : {}) }) : undefined,
      });
      if (!response.ok) throw await setupFailure(response);
      const result = await response.json() as GameSetupStatus;
      if (!mounted.current) return;
      setSetupStatus(result);
      setSetupLoadError("");
      await refresh();
    } catch (reason) {
      if (mounted.current) setSetupError(reason instanceof Error ? reason.message : "游戏接入失败");
    } finally {
      setupLock.current = false;
      if (mounted.current) setSetupBusy(null);
    }
  };

  const operate = async (id: string, action: () => Promise<void>) => {
    if (operationLock.current) return;
    operationLock.current = true;
    setOperationBusy(id);
    try { await action(); }
    finally { operationLock.current = false; setOperationBusy(null); }
  };

  const gameCommand = async (id: string, command: string, body?: unknown) => {
    setCatalogError("");
    try {
      const response = await runtimeFetch(`${runtime}/api/games/${id}/${command}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "游戏操作失败");
      await refresh();
    } catch (reason) { setCatalogError(reason instanceof Error ? reason.message : "游戏操作失败"); }
  };

  const play = async (game: GameStatus) => {
    setCatalogError("");
    try {
      const stopping = game.play.status === "running" || game.play.status === "paused";
      const response = await runtimeFetch(`${runtime}/api/games/${game.id}/play${stopping ? "/stop" : ""}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: stopping ? undefined : JSON.stringify({ goal: playGoal.trim(), max_steps: 60 }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "游玩任务启动失败");
      await refresh();
    } catch (reason) { setCatalogError(reason instanceof Error ? reason.message : "游玩任务操作失败"); }
  };

  const observe = async (id: string) => {
    setCatalogError("");
    try {
      const response = await runtimeFetch(`${runtime}/api/games/${id}/observe`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "实时状态读取失败");
      const text = JSON.stringify(data.state, null, 2);
      setSnapshot({ gameId: id, text: text.length > 20000 ? `${text.slice(0, 20000)}\n…状态过长，显示前 20,000 字` : text });
    } catch (reason) { setCatalogError(reason instanceof Error ? reason.message : "实时状态读取失败"); }
  };

  const launch = async (id: string) => {
    setCatalogError("");
    try {
      const response = await runtimeFetch(`${runtime}/api/games/${id}/launch`, { method: "POST" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "启动失败");
      setGameId(id);
      await refresh();
    } catch (reason) { setCatalogError(reason instanceof Error ? reason.message : "启动失败"); }
  };

  const togglePause = async (game: GameStatus) => {
    try {
      const response = await runtimeFetch(`${runtime}/api/games/${game.id}/${game.paused || game.play.status === "paused" ? "resume" : "pause"}`, { method: "POST" });
      if (!response.ok) { const data = await response.json(); throw new Error(data.detail || "切换暂停状态失败"); }
      await refresh();
    } catch (reason) { setCatalogError(reason instanceof Error ? reason.message : "操作失败"); }
  };

  const loadHighActions = async () => {
    setHighBusy(true); setHighError(""); setHighPreview(null);
    try {
      const response = await runtimeFetch(`${runtime}/api/games/bannerlord/high-impact`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "无法读取骑砍高影响动作");
      setHighCatalog(data as HighCatalog);
      setHighTool(""); setHighArguments("{}");
    } catch (reason) { setHighError(reason instanceof Error ? reason.message : "无法读取骑砍高影响动作"); }
    finally { setHighBusy(false); }
  };

  const previewHighAction = async () => {
    if (!selectedHighTool?.can_preview) return;
    setHighBusy(true); setHighError(""); setHighPreview(null); setHighResult("");
    try {
      const argumentsValue: unknown = JSON.parse(highArguments);
      if (!argumentsValue || typeof argumentsValue !== "object" || Array.isArray(argumentsValue)) throw new Error("参数必须是 JSON 对象");
      const response = await runtimeFetch(`${runtime}/api/games/bannerlord/high-impact/preview`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action_id: crypto.randomUUID(), name: highTool, parameters: argumentsValue }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "预览失败");
      setHighPreview(data as HighPreview);
    } catch (reason) { setHighError(reason instanceof Error ? reason.message : "预览失败"); }
    finally { setHighBusy(false); }
  };

  const confirmHighAction = async () => {
    if (!highPreview) return;
    setHighBusy(true); setHighError("");
    try {
      const response = await runtimeFetch(`${runtime}/api/games/bannerlord/high-impact/${highPreview.preview_id}/execute`, { method: "POST" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "高影响操作失败");
      setHighResult(`执行结果：${data.status}。请核对当前存档与游戏画面。`);
      setHighPreview(null);
      await refresh();
    } catch (reason) { setHighError(reason instanceof Error ? reason.message : "高影响操作失败"); }
    finally { setHighBusy(false); }
  };

  const ask = async () => {
    if (!windowTitle.trim() || !question.trim()) return;
    setBusy(true); setError("");
    try {
      for (let seconds = 3; seconds > 0; seconds--) {
        setCountdown(seconds);
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
      }
      setCountdown(0);
      const response = await runtimeFetch(`${runtime}/api/games/screen-chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ game_id: gameId, window_title: windowTitle.trim(), message: question.trim() }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "截图问答失败");
      setAnswer(data.reply);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "截图问答失败"); }
    finally { setBusy(false); setCountdown(0); }
  };

  return (
    <section id="games" className="panel game-panel">
      <div className="section-title"><h2>游戏与艾拉</h2><span>安装接入与实时连接分别显示</span></div>
      <div className="game-setup-overview">
        <div><strong>检测与接入</strong><p>启动艾拉后自动检测和部署兼容插件，游戏运行时会等关闭后再接入。</p><small>发现游戏文件后仍需核对版本与插件。插件部署完成后，启动游戏并进入存档，才能建立实时连接。</small></div>
        <button type="button" disabled={setupBusy !== null || setupStatus?.scanning} onClick={() => void configureSetup()}>{setupBusy === "scan" || setupStatus?.scanning ? "正在检测与接入…" : "重新检测与接入"}</button>
      </div>
      {setupLoadError && <p className="game-error" role="status">{setupLoadError}</p>}
      {setupError && <p className="game-error" role="alert">{setupError}</p>}
      {catalogError && <p className="game-error">{catalogError}</p>}
      <label className="game-goal">艾拉的游玩目标<input value={playGoal} onChange={(event) => setPlayGoal(event.target.value)} placeholder="例如：在农场浇完今天的作物" /></label>
      <div className="catalog-grid">{catalog.map((game) => {
        const setup = setupStatus?.games.find((item) => item.game_id === game.id);
        const connected = game.bridge === "connected";
        const playable = connected && game.ready !== false;
        const setupReady = setup?.state === "ready";
        const setupBlocked = !connected && !setupReady;
        const setupWorking = setupStatus?.scanning || setupBusy !== null || setup?.state === "installing";
        const detected = setup?.detected ?? (setupReady || game.installation?.installed);
        return <article className="catalog-card" key={game.id}>
        <div className="game-card-heading"><h3>{game.name}</h3><em className={game.bridge === "connected" ? "connected" : ""}>{game.bridge === "connected" ? "已连接" : game.bridge === "offline" ? "未连接" : "待配置"}</em></div>
        <p>{game.bridge === "connected" ? (playable ? "接口桥接已连接，可读取实时状态和提交可核验动作" : "接口已连接，等待进入存档后开始游玩") : game.bridge === "offline" ? "游戏插件当前未连接" : "未配置游戏接口桥接"}</p>
        <div className="game-setup-status" data-state={setup?.state || "unknown"}>
          <div className="game-setup-status-heading"><strong>{setup ? setupLabels[setup.state] : "插件状态尚未核对"}</strong><small>{detected ? "游戏文件已发现" : setup?.state === "needs_selection" ? "发现多个游戏目录" : "游戏文件尚未确认"}</small></div>
          <p>{setup?.detail || (setupLoadError ? "接入状态暂时不可用，请检查运行时连接。" : "正在核对游戏目录、版本和插件…")}</p>
          {setup?.launch_detail && <small>{setup.launch_detail}</small>}
          {setupReady && <small>{connected ? "插件已部署，实时接口已连接。" : "插件部署完成，等待启动游戏并进入存档。"}</small>}
          {setup?.version && <small>检测版本：{setup.version}</small>}
          {setup?.loader_version && <small>当前加载器：{setup.loader_version}{setup.bundled_loader_version && ` · 项目资源版本：${setup.bundled_loader_version}`}</small>}
          {!!setup?.pending_files?.length && <details className="game-setup-backup"><summary>待准备文件（{setup.pending_files.length}）</summary><ul className="game-setup-blockers">{setup.pending_files.slice(0, 12).map((item) => <li key={item.path}>{item.reason === "missing" ? "缺少" : "与资源不同"} · {item.path}</li>)}</ul>{setup.pending_files.length > 12 && <small>另有 {setup.pending_files.length - 12} 个文件；接入后重新校验全部文件。</small>}</details>}
          {setup?.game_root && <small className="game-setup-path" title={setup.game_root}>游戏目录：{setup.game_root}</small>}
          {setup?.blockers && setup.blockers.length > 0 && <ul className="game-setup-blockers">{setup.blockers.map((item, index) => <li key={`${index}-${item}`}>{blockerLabels[item] || item}</li>)}</ul>}
          {setup?.backup_dir && <details className="game-setup-backup"><summary>原插件备份</summary><small className="game-setup-path">{setup.backup_dir}</small></details>}
        </div>
        <details className="game-setup-location" open={setup?.state === "needs_selection" || setup?.state === "not_found" ? true : undefined}>
          <summary>{setup?.state === "needs_selection" ? "选择要接入的游戏实例" : "设置游戏目录"}</summary>
          <fieldset disabled={setupBusy !== null || setupStatus?.scanning || setup?.state === "installing"}>
            {setup?.candidates && setup.candidates.length > 1 && <label>检测到的实例<select aria-label={`${game.name}游戏实例`} value={setupLocations[game.id]?.selection || ""} onChange={(event) => {
              const selection = event.target.value;
              const candidate = setup.candidates?.find((item) => candidateValue(item) === selection);
              setSetupLocations((value) => ({ ...value, [game.id]: candidate ? { path: candidate.path, profile: candidate.profile, selection } : { path: "" } }));
            }}>
              <option value="">选择一个目录</option>{setup.candidates.map((item) => <option value={candidateValue(item)} key={candidateValue(item)}>{item.label}{item.profile && !item.label.includes(item.profile) ? ` · ${item.profile}` : ""} · {item.version || "版本未知"}{item.loader_version ? ` · 加载器 ${item.loader_version}` : " · 未发现加载器"} · {item.path}</option>)}
            </select></label>}
            <label>游戏安装目录<input aria-label={`${game.name}游戏目录`} value={setupLocations[game.id]?.path || ""} onChange={(event) => setSetupLocations((value) => ({ ...value, [game.id]: { path: event.target.value } }))} placeholder={game.id === "minecraft" ? "输入 Minecraft 实例目录" : "输入游戏安装目录"} maxLength={2000} spellCheck={false} /></label>
            <button type="button" disabled={!setupLocations[game.id]?.path.trim()} onClick={() => void configureSetup(game.id)}>{setupBusy === game.id ? "正在核对目录…" : "检测并接入此目录"}</button>
          </fieldset>
        </details>
        {game.launch && ["launching", "waiting_for_save"].includes(game.launch.status) && <div className="game-launch-progress" role="status"><b>{game.launch.status === "launching" ? "正在打开游戏…" : "等待你进入存档…"}</b><p>进入存档并保持游戏运行，插件连接后艾拉会开始目标。剩余 {game.launch.remaining_seconds ?? 300} 秒。</p><button disabled={operationBusy !== null} onClick={() => void operate(game.id, () => gameCommand(game.id, "launch-play/cancel"))}>取消等待</button></div>}
        {game.launch?.error && <p className="game-error">{game.launch.error}</p>}
        {game.play.recovery_pending && <p className="game-error">运行时已重启，旧目标处于暂停。核对当前存档与上次动作后恢复，或停止旧目标再重新开始。</p>}
        <span>{game.bridge === "connected" ? "接口控制" : "屏幕陪聊可用"}{game.paused ? " · 已暂停操作" : ""}</span>
        {game.play.status !== "idle" && <p className="game-play-status">游玩：{game.play.status} · {game.play.step ?? 0}/{game.play.max_steps ?? 0} 步{typeof game.play.decision_calls === "number" ? ` · 决策 ${game.play.decision_calls}/${game.play.max_decisions ?? "—"}` : ""}{game.play.last_action ? ` · ${game.play.last_action} ${game.play.last_result ?? ""}` : ""}{game.play.error ? ` · ${game.play.error}` : ""}</p>}
        <div className="game-card-actions"><button className="primary" disabled={operationBusy !== null || setupWorking || setupBlocked || !playGoal.trim() || ["running", "paused"].includes(game.play.status) || ["launching", "waiting_for_save"].includes(game.launch?.status || "")} onClick={() => void operate(game.id, () => gameCommand(game.id, "launch-play", {goal: playGoal.trim(), max_steps: 60}))}>启动并一起玩</button><button disabled={operationBusy !== null || setupWorking || ["launching", "waiting_for_save", "starting_play"].includes(game.launch?.status || "")} onClick={() => void operate(game.id, () => launch(game.id))}>{operationBusy === game.id ? "处理中…" : "启动游戏"}</button><button disabled={operationBusy !== null || ["launching", "waiting_for_save", "starting_play"].includes(game.launch?.status || "") || ((game.play.status !== "running" && game.play.status !== "paused") && (!playable || !playGoal.trim()))} onClick={() => void operate(game.id, () => play(game))}>{game.play.status === "running" || game.play.status === "paused" ? "停止游玩" : "让艾拉游玩"}</button><button disabled={operationBusy !== null || game.bridge !== "connected"} onClick={() => void operate(game.id, () => observe(game.id))}>实时状态</button><button disabled={operationBusy !== null} onClick={() => void operate(game.id, () => togglePause(game))}>{game.paused || game.play.status === "paused" ? "恢复操作" : "暂停操作"}</button></div>
        {game.play.status === "awaiting_confirmation" && <div className="game-launch-progress"><b>艾拉认为目标已完成，请核对游戏</b><p>模型判断尚未作为完成结果。确认画面与目标一致后，可记录完成。</p><button disabled={operationBusy !== null} onClick={() => void operate(game.id, () => gameCommand(game.id, "play/confirm", {confirmed: true}))}>我已核对，记录完成</button></div>}
        {snapshot?.gameId === game.id && <details className="game-state-details" open><summary>当前实时状态</summary><pre className="game-snapshot">{snapshot.text}</pre></details>}
        {game.id === "bannerlord" && <details className="game-high-impact">
          <summary>高级操作 · 手动预览与确认</summary><div className="game-high-content">
          <p>普通游玩无法使用存档切换、宣战、角色永久变更、作弊或通用命令。存档身份可验证时，可预览具体动作并在 5 分钟内确认一次；作弊和通用命令暂不开放。</p>
          <button disabled={game.bridge !== "connected" || highBusy} onClick={() => void loadHighActions()}>查看可预览动作</button>
          {highCatalog && <>
            <p>当前存档 ID：{highCatalog.save_id ?? "GABS 未提供，无法授权"}</p>
            <select value={highTool} onChange={(event) => { setHighTool(event.target.value); setHighPreview(null); setHighArguments("{}"); }}>
              <option value="">选择一个动作</option>
              {highCatalog.actions.map((item) => <option value={item.name} key={item.name}>{item.name} · {item.group}{item.can_preview ? "" : "（禁止授权）"}</option>)}
            </select>
            {selectedHighTool && <>
              <pre className="game-snapshot">参数结构：{JSON.stringify(selectedHighTool.parameters, null, 2)}</pre>
              <textarea value={highArguments} onChange={(event) => { setHighArguments(event.target.value); setHighPreview(null); }} placeholder="输入此动作的 JSON 参数" maxLength={10000} />
              <button disabled={!selectedHighTool.can_preview || !highCatalog.save_id || highBusy} onClick={() => void previewHighAction()}>预览具体效果</button>
            </>}
          </>}
          {highPreview && <div className="game-high-preview">
            <p><b>能力组：</b>{highPreview.group}　<b>存档：</b>{highPreview.save_id}</p>
            <p><b>预期效果：</b>{highPreview.effect}</p>
            <p><b>工具：</b>{highPreview.tool}</p>
            <pre className="game-snapshot">最终参数：{JSON.stringify(highPreview.arguments, null, 2)}</pre>
            <p>许可仅用于这一次动作，到期时间：{new Date(highPreview.expires_at).toLocaleString()}</p>
            <button disabled={highBusy || game.play.status === "running" || game.play.status === "paused"} onClick={() => void confirmHighAction()}>确认并执行这一次操作</button>
          </div>}
          {highError && <p className="game-error">{highError}</p>}
          {highResult && <p>{highResult}</p>}
          </div>
        </details>}
      </article>; })}</div>
      {catalog.length === 0 && !catalogError && <p className="usage-empty">正在读取游戏接入状态…</p>}
      <div className="game-screen-chat">
        <strong>屏幕陪聊</strong>
        <p>填写游戏窗口标题，点击后有 3 秒切回游戏。艾拉只截取前台匹配的窗口并回答，不会控制游戏。</p>
        <select value={gameId} onChange={(event) => setGameId(event.target.value)}>
          <option value="minecraft">我的世界</option><option value="stardew_valley">星露谷物语</option><option value="bannerlord">骑马与砍杀</option>
        </select>
        <input value={windowTitle} onChange={(event) => setWindowTitle(event.target.value)} placeholder="游戏窗口标题，如 Minecraft" />
        <textarea value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="对当前画面提问…" />
        <button disabled={busy || windowTitle.trim().length < 3 || !question.trim()} onClick={ask}>{countdown ? `${countdown} 秒后截图，请切回游戏` : busy ? "分析中…" : "3 秒后截图并提问"}</button>
        {error && <p className="game-error">{error}</p>}
        {answer && <p className="game-answer">{answer}</p>}
      </div>
    </section>
  );
}
