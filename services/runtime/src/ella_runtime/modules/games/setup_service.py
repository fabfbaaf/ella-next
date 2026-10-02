"""Background game discovery and deployment, independent of launching games."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ella_runtime.modules.games.bridge import GAME_NAMES
from ella_runtime.modules.games.minecraft_setup import install_fabric_profile
from ella_runtime.modules.games.setup import GameSetupError, GameSetupManager

logger = logging.getLogger(__name__)


class GameSetupService:
    def __init__(self, manager: GameSetupManager, *, interval: float = 60) -> None:
        self.manager = manager
        self.interval = interval
        self._lock = asyncio.Lock()
        self._scan_task: asyncio.Task[None] | None = None
        self._auto_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._error: str | None = None
        self._issues: dict[str, str] = {}

    def status(self, game_id: str) -> dict[str, Any]:
        status = self.manager.status(game_id)
        if issue := self._issues.get(game_id):
            status.update(state="error", ready=False, detail=issue)
        if game_id == "minecraft" and str(status.get("profile", "")).startswith("ella-fabric-"):
            status["launch_detail"] = "请在启动器选择“艾拉 · Fabric 26.2”，进入存档后会自动连接"
        return status

    def snapshot(self) -> dict[str, Any]:
        return {
            "games": [self.status(game_id) for game_id in GAME_NAMES],
            "scanning": self._lock.locked() or bool(
                self._scan_task and not self._scan_task.done()
            ),
            "error": self._error,
        }

    def start(self) -> None:
        if self._scan_task and not self._scan_task.done():
            return
        self._scan_task = asyncio.create_task(self._scan())

    def start_auto(self) -> None:
        self._stop.clear()
        self.start()
        if self._auto_task is None or self._auto_task.done():
            self._auto_task = asyncio.create_task(self._repeat())

    async def _repeat(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except TimeoutError:
                self.start()

    async def _scan(self) -> None:
        async with self._lock:
            self._error = None
            try:
                await asyncio.to_thread(self.manager.scan)
                for game_id in GAME_NAMES:
                    await self._prepare(game_id)
            except Exception:
                self._error = "自动检测未完成，请重新检测；日志中有详细原因"
                logger.exception("Game discovery/deployment failed")

    async def prepare(self, game_id: str) -> dict[str, Any]:
        # Deployment cannot race with a user-requested launch or a location change.
        async with self._lock:
            await self._prepare(game_id)
            return self.status(game_id)

    def _bootstrap_minecraft(self) -> None:
        status = self.manager.status("minecraft")
        if status.get("profile_version") != "26.2" or not self.manager.bundle_root:
            return
        if self.manager.is_running("minecraft", Path(status["game_root"])):
            return
        profile = install_fabric_profile(self.manager.bundle_root, self.manager.data_dir, status)
        if profile:
            self.manager.select_location("minecraft", status["game_root"], profile=profile)

    async def _prepare(self, game_id: str) -> None:
        self._issues.pop(game_id, None)
        try:
            if game_id == "minecraft":
                # The initial scan chooses one instance; launching never guesses another one.
                await asyncio.to_thread(self.manager.scan)
                await asyncio.to_thread(self._bootstrap_minecraft)
            await self.manager.ensure(game_id)
        except (ValueError, OSError) as exc:
            self._issues[game_id] = str(exc) if isinstance(exc, GameSetupError) else (
                "游戏接入准备失败，请检查目录权限、兼容版本和启动器是否已关闭"
            )
            logger.exception("Game setup failed for %s", game_id)

    async def select(self, game_id: str, path: str, *, profile: str | None = None) -> None:
        async with self._lock:
            await asyncio.to_thread(self.manager.select_location, game_id, path, profile=profile)
        self.start()

    async def aclose(self) -> None:
        self._stop.set()
        if self._auto_task:
            await self._auto_task
        # Never cancel a worker while it is replacing files. Let its transaction finish.
        if self._scan_task:
            await self._scan_task
