import { useCallback, useEffect, useRef, useState } from "react";
import { runtimeBase, runtimeFetch } from "../../runtime-client";

export type OverviewTask = { id: string; goal: string; state: string; error: string | null };
type Provider = { configured: boolean; provider: string; model: string } | null;
export type RuntimeOverview = {
  health: { status: string; version: string } | null;
  models: { chat: Provider; action: Provider; action_uses_chat: boolean } | null;
  voice: { can_transcribe: boolean; can_speak: boolean; state: string } | null;
  games: { id: string; name: string; bridge: string; play: { status: string } }[] | null;
  tasks: OverviewTask[] | null;
  notifications: { id: string; text: string; read_at: string | null; delivered_at: string }[] | null;
  errors: string[];
  updatedAt: number | null;
};
const empty: RuntimeOverview = {
  health: null, models: null, voice: null, games: null, tasks: null,
  notifications: null, errors: [], updatedAt: null,
};
const endpoints = [
  ["health", "/health", "运行时"],
  ["models", "/api/models/status", "模型配置"],
  ["voice", "/api/voice/status", "语音状态"],
  ["games", "/api/games/catalog", "游戏连接"],
  ["tasks", "/api/tasks", "任务列表"],
  ["notifications", "/api/companion/notifications", "通知记录"],
] as const;

export function useRuntimeOverview() {
  const [snapshot, setSnapshot] = useState<RuntimeOverview>(empty);
  const [loading, setLoading] = useState(true);
  const controller = useRef<AbortController | null>(null);
  const refresh = useCallback(async () => {
    if (controller.current) return;
    const request = new AbortController();
    controller.current = request;
    setLoading(true);
    const timeout = window.setTimeout(() => request.abort(), 8000);
    try {
      const results = await Promise.allSettled(endpoints.map(async ([, path]) => {
        const response = await runtimeFetch(runtimeBase + path, { signal: request.signal });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      }));
      if (controller.current !== request) return;
      const next: RuntimeOverview = { ...empty, errors: [], updatedAt: Date.now() };
      results.forEach((result, index) => {
        const [key, , label] = endpoints[index];
        if (result.status === "fulfilled") Object.assign(next, { [key]: result.value });
        else next.errors.push(label);
      });
      setSnapshot(next);
    } finally {
      window.clearTimeout(timeout);
      if (controller.current === request) {
        controller.current = null;
        setLoading(false);
      }
    }
  }, []);
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15_000);
    return () => {
      window.clearInterval(timer);
      const request = controller.current;
      controller.current = null;
      request?.abort();
    };
  }, [refresh]);
  return { snapshot, loading, refresh };
}
