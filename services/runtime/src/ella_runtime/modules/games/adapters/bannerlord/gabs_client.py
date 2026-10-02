from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class GabsError(RuntimeError):
    pass


def _resource_roots() -> list[Path]:
    resources = os.getenv("ELLA_RESOURCES_DIR")
    roots = [Path(resources)] if resources else []
    roots.extend(parent for parent in Path(__file__).resolve().parents if (parent / "artifacts/game-plugins").is_dir())
    return roots


def find_gabs_exe() -> str | None:
    explicit = os.getenv("ELLA_GABS_EXE")
    if explicit:
        return explicit
    for root in _resource_roots():
        bundled = root / "artifacts/game-plugins/gabs/gabs.exe"
        if bundled.is_file():
            return str(bundled)
    return shutil.which("gabs") or shutil.which("gabs.exe")


def find_gabs_config_dir(executable: Path | None = None) -> str | None:
    explicit = os.getenv("ELLA_GABS_CONFIG_DIR")
    if explicit:
        return explicit
    # Only an explicitly configured external directory can contain imported credentials.
    # Automatic setup always generates its own local configuration for the selected game.
    from ella_runtime.modules.games.launch import game_executable
    from ella_runtime.storage_paths import default_data_dir
    executable = executable or game_executable("bannerlord")
    if executable is None:
        return None
    config = default_data_dir() / "gabs/auto-config"
    config.mkdir(parents=True, exist_ok=True)
    path = config / "config.json"
    payload: dict[str, Any] = {}
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict) or payload.get("ellaManaged") is not True
                or not isinstance(payload.get("games"), dict)
                or not isinstance(payload.get("apiKey"), str) or len(payload["apiKey"]) < 32
            ):
                raise GabsError("自动 GABS 配置已被替换，请通过 ELLA_GABS_CONFIG_DIR 指定外部配置")
        except (OSError, ValueError) as exc:
            raise GabsError("自动 GABS 配置损坏，请检查本地配置") from exc
    modules = "_MODULES_*Bannerlord.Harmony*Bannerlord.ButterLib*Bannerlord.UIExtenderEx*Bannerlord.MBOptionScreen*Bannerlord.GABS*Native*SandBoxCore*CustomBattle*Sandbox*StoryMode*_MODULES_"
    game = {"id": "bannerlord", "name": "Mount & Blade II: Bannerlord", "launchMode": "DirectPath", "target": str(executable), "workingDir": str(executable.parent), "args": ["/singleplayer", modules], "stopProcessName": "Bannerlord.exe"}
    changed = payload.get("games", {}).get("bannerlord") != game
    if not payload:
        payload = {"version": "1.0", "ellaManaged": True, "apiKey": secrets.token_hex(32), "toolNormalization": {"enableOpenAINormalization": True, "maxToolNameLength": 64, "preserveOriginalName": True}, "games": {}}
    payload["games"]["bannerlord"] = game
    if changed:
        temporary = config / "config.tmp"
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(path)
    return str(config)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _unwrap_mcp_value(value: Any) -> Any:
    """Extract structured content from common MCP result shapes without assuming one SDK."""
    if isinstance(value, dict):
        if value.get("isError"):
            raise GabsError(_content_to_text(value.get("content")) or "GABS tool returned an MCP error")
        if "structuredContent" in value:
            return value["structuredContent"]
        if "content" in value:
            content = value["content"]
            if isinstance(content, list):
                texts = [item.get("text") for item in content if isinstance(item, dict) and item.get("type") == "text"]
                if len(texts) == 1 and isinstance(texts[0], str):
                    try:
                        return json.loads(texts[0])
                    except (json.JSONDecodeError, TypeError):
                        return texts[0]
                if texts:
                    return "\n".join(str(item) for item in texts)
        # Some GABS responses already expose the tool payload as a dict.
        return {key: _unwrap_mcp_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_unwrap_mcp_value(item) for item in value]
    return value


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(item.get("text", "")) for item in content if isinstance(item, dict)).strip()
    return str(content or "")


@dataclass(slots=True)
class GabsServerConfig:
    endpoint: str
    api_key: str | None = None
    managed: bool = False


class GabsMcpClient:
    """Minimal local MCP-over-HTTP client dedicated to GABS.

    Attach to an existing loopback GABS server or start a local GABS process.
    """

    def __init__(self, config: GabsServerConfig | None = None) -> None:
        self.config = config
        self._process: subprocess.Popen | None = None
        self._next_id = 1
        self.launch_executable_provider: Callable[[], Path | None] | None = None

    @property
    def endpoint(self) -> str | None:
        return self.config.endpoint if self.config else None

    async def start(self) -> None:
        if self.config is not None:
            await self.wait_ready()
            return
        explicit = os.getenv("ELLA_GABS_HTTP")
        api_key = os.getenv("ELLA_GABS_API_KEY")
        if explicit:
            endpoint = explicit.rstrip("/")
            if not endpoint.endswith("/mcp"):
                endpoint += "/mcp"
            parsed = urlparse(endpoint)
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise GabsError("GABS 地址只能是本机 HTTP 服务")
            self.config = GabsServerConfig(endpoint=endpoint, api_key=api_key, managed=False)
            await self.wait_ready()
            return
        exe = find_gabs_exe()
        if not exe:
            raise GabsError("GABS 未安装；请配置 ELLA_GABS_EXE 或 ELLA_GABS_HTTP")
        config_dir = find_gabs_config_dir(
            self.launch_executable_provider() if self.launch_executable_provider else None
        )
        if not api_key and config_dir:
            try:
                config_data = json.loads((Path(config_dir) / "config.json").read_text(encoding="utf-8"))
                api_key = config_data.get("apiKey") if isinstance(config_data, dict) else None
            except (OSError, ValueError):
                pass
        port = _free_port()
        self.config = GabsServerConfig(
            endpoint=f"http://127.0.0.1:{port}/mcp",
            api_key=api_key,
            managed=True,
        )
        args = [exe, "server", "--http", f"127.0.0.1:{port}"]
        if config_dir:
            args += ["--configDir", config_dir]
        try:
            self._process = await asyncio.to_thread(
                subprocess.Popen,
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            await self.wait_ready(timeout=10.0)
        except OSError as exc:
            await self.close()
            raise GabsError("无法启动 GABS，请检查程序路径和执行权限后重试") from exc
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 3)
            except subprocess.TimeoutExpired:
                process.kill()
        if self.config and self.config.managed:
            self.config = None

    async def wait_ready(self, timeout: float = 6.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        last: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                await self.call_tool("games_list", {})
                return
            except (GabsError, OSError, ValueError) as exc:  # startup/retry boundary
                last = exc
                await asyncio.sleep(0.15)
        raise GabsError(f"GABS MCP server did not become ready: {last}")

    async def call_tool(self, name: str, arguments: dict[str, Any], *, timeout: float = 30.0) -> Any:
        if self.config is None:
            raise GabsError("GABS client has not been started")
        request_id = self._next_id
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        response = await asyncio.to_thread(self._post_json, payload, timeout)
        if response.get("error"):
            error = response["error"]
            raise GabsError(f"GABS {name} failed: {error}")
        return _unwrap_mcp_value(response.get("result"))

    def _post_json(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        assert self.config is not None
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        request = urllib.request.Request(self.config.endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read(4 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            detail = exc.read(64 * 1024).decode("utf-8", errors="replace")
            raise GabsError(f"GABS HTTP {exc.code}: {detail}") from exc
        except OSError as exc:
            raise GabsError(f"GABS connection failed: {exc}") from exc
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GabsError("GABS returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise GabsError("GABS returned a non-object JSON-RPC response")
        return parsed
