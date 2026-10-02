import { invoke } from "@tauri-apps/api/core";

export const runtimeBase = "http://127.0.0.1:8766";
export type RuntimeStatus = { state: string; owned: boolean; error: string; log_path: string; base_url: string };
type Connection = { base_url: string; token: string };
async function connection(): Promise<Connection> {
  if (!("__TAURI_INTERNALS__" in window)) throw new Error("请在艾拉桌面窗口中使用本地运行时");
  const value = await invoke<Connection>("runtime_connection");
  const base = new URL(value.base_url);
  if (base.protocol !== "http:" || base.hostname !== "127.0.0.1" || base.username || base.password || base.pathname !== "/" || base.search || base.hash) throw new Error("运行时地址无效");
  return value;
}
export function runtimeStatus(): Promise<RuntimeStatus> { return invoke("runtime_status"); }
export function runtimeEnsure(restart = false): Promise<RuntimeStatus> { return invoke("runtime_ensure", { restart }); }
export async function runtimeFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const url = new URL(input, runtimeBase);
  if (url.origin !== runtimeBase) throw new Error("不能向其他服务发送桌面会话凭据");
  const service = await connection();
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${service.token}`);
  return fetch(`${service.base_url}${url.pathname}${url.search}`, { ...init, headers });
}
export async function runtimeVoiceSocket(): Promise<WebSocket> {
  const service = await connection();
  return new WebSocket(`${service.base_url.replace("http:", "ws:")}/api/voice/stream`, ["ella-auth", service.token]);
}
