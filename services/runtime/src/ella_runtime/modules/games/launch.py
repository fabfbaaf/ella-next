"""User-requested launch, then wait for a playable bridge before starting a goal."""
from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ella_runtime.modules.games.bridge import GameBridgeError
from ella_runtime.modules.games.controller import GameControlError, GameController
from ella_runtime.modules.games.play import GamePlayError


def steam_libraries() -> list[Path]:
    roots: list[Path] = []
    if os.name == "nt":
        import winreg
        for hive, key, name in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, key) as entry:
                    roots.append(Path(winreg.QueryValueEx(entry, name)[0]))
            except OSError:
                pass
    roots.extend([
        Path(r"C:\Program Files (x86)\Steam"),
        Path(r"C:\Program Files (x86)\Steam\stema"),
    ])
    libraries = list(roots)
    for root in roots:
        try:
            text = (root / "steamapps/libraryfolders.vdf").read_text(encoding="utf-8")
            libraries.extend(Path(value.replace("\\\\", "\\")) for value in
                             re.findall(r'"path"\s+"([^"\r\n]+)"', text))
        except (OSError, UnicodeError):
            pass
    return list(dict.fromkeys(libraries))


def game_executable(game_id: str, libraries: list[Path] | None = None) -> Path | None:
    explicit = os.getenv(f"ELLA_GAME_{game_id.upper()}_EXECUTABLE", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return path.resolve() if path.is_file() and path.suffix.lower() == ".exe" else None
    relative = {
        "stardew_valley": "Stardew Valley/StardewModdingAPI.exe",
        "bannerlord": "Mount & Blade II Bannerlord/bin/Win64_Shipping_Client/Bannerlord.BLSE.Standalone.exe",
    }.get(game_id)
    if not relative:
        return None
    for library in libraries if libraries is not None else steam_libraries():
        path = library / "steamapps/common" / relative
        if path.is_file():
            return path.resolve()
    return None


def installation_status(game_id: str) -> dict[str, Any]:
    path = game_executable(game_id)
    installed = path is not None
    detail = "已找到启动程序" if installed else "未找到启动程序，请配置游戏路径"
    if path and game_id == "stardew_valley":
        plugin = path.parent / "Mods/Ella.StardewBridge/Ella.StardewBridge.dll"
        if not plugin.is_file():
            detail = "已找到 SMAPI，尚未安装艾拉插件"
    if path and game_id == "bannerlord":
        modules = path.parents[2] / "Modules/Bannerlord.GABS"
        if not modules.is_dir():
            detail = "已找到骑砍启动程序，尚未安装 GABS 模组"
    return {"installed": installed, "executable": str(path) if path else None, "detail": detail}


class GameLaunchCoordinator:
    def __init__(self, *, timeout: float = 300, interval: float = 2) -> None:
        self.timeout = timeout
        self.interval = interval
        self.sessions: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}

    def status(self, game_id: str) -> dict[str, Any]:
        return dict(self.sessions.get(game_id, {"status": "idle"}))

    async def begin(
        self, game_id: str, goal: str, controller: GameController,
        launch: Callable[[], Awaitable[Any]], start: Callable[[], Awaitable[Any]],
    ) -> dict[str, Any]:
        task = self.tasks.get(game_id)
        if task and not task.done():
            raise GamePlayError("这款游戏正在等待进入存档，请先取消等待")
        session = {"status": "launching", "goal": goal, "error": "", "remaining_seconds": int(self.timeout)}
        self.sessions[game_id] = session
        self.tasks[game_id] = asyncio.create_task(self._run(session, controller, launch, start))
        return dict(session)

    async def _run(self, session, controller, launch, start) -> None:
        try:
            # An already connected game is reused. Never open a second process unnecessarily.
            try:
                snapshot = await controller.observe()
                ready = snapshot.state.get("bridge_ready") is True
                connected = True
            except (GameBridgeError, GameControlError):
                connected, ready = False, False
            if not connected:
                await launch()
            session["status"] = "waiting_for_save"
            deadline = time.monotonic() + self.timeout
            while not ready:
                remaining = deadline - time.monotonic()
                session["remaining_seconds"] = max(0, int(remaining))
                if remaining <= 0:
                    session["status"] = "timed_out"
                    session["error"] = "等待存档超时。请进入存档并检查插件，再点击一起玩。"
                    return
                await asyncio.sleep(self.interval)
                try:
                    snapshot = await controller.observe()
                    ready = snapshot.state.get("bridge_ready") is True
                except (GameBridgeError, GameControlError):
                    pass
            session["status"] = "starting_play"
            await start()
            session["status"] = "started"
            session["remaining_seconds"] = 0
        except asyncio.CancelledError:
            session["status"] = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - startup must stop on any failed boundary
            session["status"] = "failed"
            # Known user-facing errors are safe; never expose arbitrary process output/secrets.
            session["error"] = str(exc) if isinstance(exc, (GameBridgeError, GameControlError, GamePlayError)) else "游戏启动或游玩准备失败，请检查启动程序与插件"

    async def cancel(self, game_id: str) -> None:
        task = self.tasks.get(game_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def aclose(self) -> None:
        for game_id in list(self.tasks):
            await self.cancel(game_id)
