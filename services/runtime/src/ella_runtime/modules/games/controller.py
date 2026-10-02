"""Observe, act once, verify, and reconcile uncertain game outcomes."""

import asyncio
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from ella_runtime.modules.games.contracts import (
    ActionResult,
    ActionStatus,
    GameAction,
    GameAdapter,
    GameMode,
    GameSnapshot,
)
from ella_runtime.modules.games.journal import GameJournal


class GameControlError(RuntimeError):
    """Game is not ready for a safe controlled action."""


class GameController:
    def __init__(
        self,
        adapter: GameAdapter,
        journal: GameJournal,
        *,
        script_window_is_foreground: Callable[[str], bool] | None = None,
        max_observation_age: timedelta = timedelta(seconds=5),
    ) -> None:
        self.adapter = adapter
        self.journal = journal
        self.script_window_is_foreground = script_window_is_foreground
        self.max_observation_age = max_observation_age
        self._action_lock = asyncio.Lock()

    async def observe(self) -> GameSnapshot:
        snapshot = await self.adapter.observe()
        if snapshot.game_id != self.adapter.game_id:
            raise GameControlError("游戏桥接返回了其他游戏的状态")
        if snapshot.captured_at.tzinfo is None:
            raise GameControlError("游戏状态缺少时区信息")
        captured = snapshot.captured_at.astimezone(UTC)
        age = datetime.now(UTC) - captured
        if not getattr(self.adapter, "uses_monotonic_freshness", False) and (
            age > self.max_observation_age or age < -timedelta(seconds=2)
        ):
            raise GameControlError("游戏状态已过期，请重新连接游戏")
        return snapshot

    async def perform(self, action: GameAction) -> ActionResult:
        async with self._action_lock:
            return await self._perform_locked(action)

    async def _perform_locked(self, action: GameAction) -> ActionResult:
        if self.adapter.mode == GameMode.SCREEN_CHAT:
            raise GameControlError("当前游戏仅支持屏幕聊天，不能执行动作")
        if self.adapter.mode == GameMode.SCRIPT_CONTROL and (
            self.script_window_is_foreground is None
            or not self.script_window_is_foreground(self.adapter.game_id)
        ):
            raise GameControlError("目标游戏窗口未处于前台，脚本操作已停止")
        before = await self.observe()
        for field in ("client_id", "session_id", "save_id"):
            expected = getattr(action, field)
            if expected is not None and before.state.get(field) != expected:
                raise GameControlError("游戏会话在决策后已切换，禁止向新存档执行旧动作")
        binder = getattr(self.adapter, "bind_action", None)
        if binder is not None:
            action = binder(action, before)
        preflight = getattr(self.adapter, "preflight_action", None)
        if preflight is not None:
            await preflight(action, before)
        try:
            self.journal.begin(self.adapter.game_id, action, before)
        except sqlite3.IntegrityError as exc:
            raise GameControlError("此游戏动作 ID 已使用，禁止重复执行") from exc
        try:
            await self.adapter.apply_action(action)
            after = await self.observe()
            verified, evidence = self.adapter.verify_action(action, before, after)
            result = ActionResult(
                action_id=action.action_id,
                status=ActionStatus.VERIFIED if verified else ActionStatus.UNVERIFIED,
                before=before,
                after=after,
                evidence=evidence,
            )
        except Exception as exc:  # noqa: BLE001 - action may have succeeded before any tool error
            result = ActionResult(
                action_id=action.action_id,
                status=ActionStatus.UNKNOWN,
                before=before,
                evidence={"error_type": type(exc).__name__, "replay_allowed": False},
            )
        self.journal.finish(self.adapter.game_id, result)
        return result

    async def reconcile(self, action_id: str) -> ActionResult:
        async with self._action_lock:
            return await self._reconcile_locked(action_id)

    async def _reconcile_locked(self, action_id: str) -> ActionResult:
        game_id, action, before, prior = self.journal.get(action_id)
        if game_id != self.adapter.game_id:
            raise GameControlError("动作属于其他游戏")
        if prior is not None and prior.status == ActionStatus.VERIFIED:
            return prior
        after = await self.observe()
        verified, evidence = self.adapter.verify_action(action, before, after)
        result = ActionResult(
            action_id=action_id,
            status=ActionStatus.VERIFIED if verified else ActionStatus.UNKNOWN,
            before=before,
            after=after,
            evidence={**evidence, "replayed": False},
        )
        self.journal.finish(game_id, result)
        return result
