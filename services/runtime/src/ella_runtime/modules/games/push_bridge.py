"""Authenticated, game-initiated bridge for Fabric and SMAPI plugins.

The game owns its main thread. Plugins publish observations and poll for one
command at a time; the runtime never sends input to an unrelated window.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ella_runtime.modules.games.bridge import GameBridgeError
from ella_runtime.modules.games.contracts import GameAction, GameMode, GameSnapshot
from ella_runtime.storage_paths import default_data_dir

PUSH_GAMES = {"minecraft", "stardew_valley"}
ACTION_LIMITS: dict[str, dict[str, dict[str, Any]]] = {
    "minecraft": {
        "move": {"direction": ["forward", "back", "left", "right"], "ticks": [1, 40]},
        "turn": {"degrees": [-90, 90]},
        "look": {"yaw_delta": [-90, 90], "pitch_delta": [-45, 45]},
        "select_hotbar": {"slot": [0, 8]},
        "jump": {"ticks": [1, 20]},
        "break_block": {"ticks": [1, 100]},
        "use_item": {},
        "attack_entity": {},
        "wait": {"ticks": [1, 40]},
    },
    "stardew_valley": {
        "move": {"direction": ["up", "down", "left", "right"], "ticks": [1, 30]},
        "face": {"direction": ["up", "down", "left", "right"]},
        "use_tool": {},
        "interact": {},
        "select_slot": {"slot": [0, 35]},
        "dialogue_continue": {},
        "dialogue_choose": {"index": [0, 19]},
        "chest_take": {"slot": [0, 35]},
        "chest_store": {"slot": [0, 35]},
        "shop_buy": {"index": [0, 9999]},
        "close_menu": {},
        "wait": {"ticks": [1, 60]},
    },
}


def available_plugin_actions(game_id: str, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose only actions supported by this plugin and its current UI state."""
    capabilities = state.get("capabilities")
    supported = set(capabilities) if isinstance(capabilities, list) else None
    catalog = []
    for name, schema in ACTION_LIMITS.get(game_id, {}).items():
        if supported is not None and name not in supported:
            continue
        if name == "break_block" and not state.get("target_block"):
            continue
        if name == "attack_entity" and not state.get("target_entity"):
            continue
        if game_id == "stardew_valley":
            if name == "close_menu" and state.get("menu_closable") is not True:
                continue
            if name == "dialogue_continue" and state.get("dialogue_can_continue") is not True:
                continue
            if name == "dialogue_choose" and state.get("dialogue_can_choose") is not True:
                continue
            if name in {"chest_take", "chest_store"} and state.get("chest_can_transfer") is not True:
                continue
            if name == "shop_buy" and not state.get("shop_items"):
                continue
            if state.get("player_free") is False and name not in {
                "wait", "close_menu", "dialogue_continue", "dialogue_choose",
                "chest_take", "chest_store", "shop_buy",
            }:
                continue
        catalog.append({"name": name, "parameters": schema})
    return catalog


def merge_observation(previous: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """A receipt may contain only changed fields; retain the last full observation."""
    merged = dict(previous)
    for key, value in update.items():
        old = merged.get(key)
        merged[key] = (
            merge_observation(old, value)
            if isinstance(old, dict) and isinstance(value, dict) else value
        )
    return merged


def validate_plugin_action(game_id: str, action: GameAction) -> None:
    spec = ACTION_LIMITS.get(game_id, {}).get(action.name)
    if spec is None:
        raise GameBridgeError("这款游戏不支持该动作")
    if set(action.parameters) != set(spec):
        raise GameBridgeError("游戏动作参数不符合插件协议")
    for key, allowed in spec.items():
        value = action.parameters[key]
        if len(allowed) == 2 and all(isinstance(item, int) for item in allowed):
            numeric = isinstance(value, int) if key in {"ticks", "slot", "index"} else isinstance(
                value, (int, float)
            )
            if isinstance(value, bool) or not numeric or not allowed[0] <= value <= allowed[1]:
                raise GameBridgeError("游戏动作数值超出范围")
        elif value not in allowed:
            raise GameBridgeError("游戏动作方向无效")


class PushGameBridge:
    mode = GameMode.API_CONTROL
    uses_monotonic_freshness = True
    LEASE_SECONDS = 5.0

    def __init__(self, game_id: str, *, data_dir: Path | None = None) -> None:
        if game_id not in PUSH_GAMES:
            raise ValueError("未知推送型游戏")
        self.game_id = game_id
        self.data_dir = data_dir or default_data_dir()
        self.token = self._load_token()
        self._snapshot: GameSnapshot | None = None
        self._last_full_seq = 0
        self._sequence = 0
        self._source_seq = -1
        self._owner: tuple[str, str] | None = None
        self._retired: set[tuple[str, str]] = set()
        self._seen_at = 0.0
        self._save_id: str | None = None
        self._pending: tuple[GameAction, asyncio.Future[bool], float] | None = None
        self._inflight: dict[str, asyncio.Future[bool]] = {}
        self._lock = asyncio.Lock()

    def _load_token(self) -> str:
        path = self.data_dir / "game-bridge" / f"{self.game_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        token = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            token = data.get("token") if isinstance(data, dict) else None
        except (OSError, ValueError, TypeError):
            pass
        if not isinstance(token, str) or not 20 <= len(token) <= 512 or not all(
            character.isascii() and (character.isalnum() or character in "_-")
            for character in token
        ):
            token = secrets.token_urlsafe(32)
        port = int(os.getenv("ELLA_RUNTIME_PORT", "8766"))
        if not 1 <= port <= 65535:
            raise ValueError("运行时端口超出范围")
        # Preserve the game's credential while publishing the current endpoint.
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "token": token, "base_url": f"http://127.0.0.1:{port}",
        }), encoding="utf-8")
        os.replace(temporary, path)
        return token

    def authenticate(self, authorization: str | None) -> None:
        if not authorization or not secrets.compare_digest(authorization, f"Bearer {self.token}"):
            raise GameBridgeError("游戏插件认证失败")

    @staticmethod
    def _identity(state: dict[str, Any]) -> tuple[str, str] | None:
        fields = state.get("client_id"), state.get("session_id")
        if not all(isinstance(value, str) and 1 <= len(value) <= 200 for value in fields):
            return None
        return fields  # type: ignore[return-value]

    @staticmethod
    def _source_sequence(state: dict[str, Any]) -> int:
        value = state.get("observation_seq")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise GameBridgeError("插件缺少有效状态序号，请更新游戏模组")
        return value

    def _invalidate_commands(self) -> None:
        self._pending = None
        for future in self._inflight.values():
            if not future.done():
                future.set_exception(GameBridgeError("游戏客户端或存档会话已切换，动作需核对"))
        self._inflight.clear()

    def _claim(self, identity: tuple[str, str], save_id: str | None) -> None:
        if identity in self._retired:
            raise GameBridgeError("旧游戏会话已失效，不能覆盖当前存档")
        if self._owner != identity:
            if self._owner is not None:
                same_client = self._owner[0] == identity[0]
                if not same_client and time.monotonic() - self._seen_at <= self.LEASE_SECONDS:
                    raise GameBridgeError("另一游戏客户端正在连接，禁止抢占或向错误窗口发动作")
                self._retired.add(self._owner)
                self._invalidate_commands()
            self._owner = identity
            self._source_seq = -1
            self._snapshot = None
            self._last_full_seq = 0
            self._save_id = save_id
        elif self._save_id is not None and save_id is not None and self._save_id != save_id:
            raise GameBridgeError("同一游戏会话的存档标识变化，请重新加载存档")
        if save_id is not None:
            self._save_id = save_id

    def _stamp(self, state: dict[str, Any], *, full: bool) -> GameSnapshot:
        self._sequence += 1
        state = dict(state)
        state["observation_seq"] = self._sequence
        state["source_observation_seq"] = self._source_seq
        state["bridge_ready"] = self._owner is not None and isinstance(state.get("player"), dict)
        state["save_id"] = self._save_id
        snapshot = GameSnapshot(
            game_id=self.game_id, captured_at=datetime.now(UTC),
            observation_seq=self._sequence, state=state,
        )
        self._snapshot = snapshot
        if full:
            self._last_full_seq = self._sequence
        return snapshot

    async def publish(self, state: dict[str, Any]) -> GameSnapshot:
        if len(json.dumps(state, ensure_ascii=False)) > 256_000:
            raise GameBridgeError("游戏状态过大")
        state = dict(state)
        async with self._lock:
            identity = self._identity(state)
            if identity is None:
                if self._owner is not None:
                    raise GameBridgeError("游戏状态缺少当前客户端/存档会话标识")
                # Legacy plugins may still be inspected, but cannot acquire actions.
                self._seen_at = time.monotonic()
                return self._stamp(state, full=isinstance(state.get("player"), dict))
            source_seq = self._source_sequence(state)
            save_id = state.get("save_id")
            if save_id is not None and (
                not isinstance(save_id, str) or not 1 <= len(save_id) <= 500
            ):
                raise GameBridgeError("游戏存档标识无效")
            self._claim(identity, save_id)
            if source_seq <= self._source_seq:
                raise GameBridgeError("忽略旧序号的游戏状态")
            self._source_seq = source_seq
            self._seen_at = time.monotonic()
            if self._snapshot is not None:
                for field in ("integration", "game_version", "mod_version", "capabilities"):
                    if field in self._snapshot.state:
                        state.setdefault(field, self._snapshot.state[field])
            full = state.get("full_snapshot") is True and isinstance(state.get("player"), dict)
            if not full and self._snapshot is not None:
                state = merge_observation(self._snapshot.state, state)
            return self._stamp(state, full=full)

    async def wait_for_telemetry(self, after: GameSnapshot, *, timeout: float = 3.0) -> None:
        """Order by server sequence, even when UTC timestamps coincide or move backwards."""
        deadline = asyncio.get_running_loop().time() + timeout
        expected = self._identity(after.state)
        while asyncio.get_running_loop().time() < deadline:
            async with self._lock:
                if self._owner != expected:
                    raise GameBridgeError("游戏会话在动作后已切换")
                if self._last_full_seq > after.observation_seq:
                    return
            await asyncio.sleep(0.05)
        raise GameBridgeError("游戏动作后没有收到新的完整状态，已停止连续游玩")

    async def observe(self) -> GameSnapshot:
        async with self._lock:
            snapshot = self._snapshot
            age = time.monotonic() - self._seen_at
        if snapshot is None:
            raise GameBridgeError("游戏插件尚未上报实时状态")
        if age > self.LEASE_SECONDS:
            raise GameBridgeError("游戏插件状态已过期")
        return snapshot

    def bind_action(self, action: GameAction, before: GameSnapshot) -> GameAction:
        identity = self._identity(before.state)
        if identity is None or before.state.get("bridge_ready") is not True:
            raise GameBridgeError("游戏插件尚未提供可操作的客户端/存档会话，请更新模组")
        for provided, expected in ((action.client_id, identity[0]),
                                   (action.session_id, identity[1]),
                                   (action.save_id, before.state.get("save_id"))):
            if provided is not None and provided != expected:
                raise GameBridgeError("动作不属于当前游戏客户端或存档")
        return action.model_copy(update={
            "client_id": identity[0], "session_id": identity[1],
            "save_id": before.state.get("save_id"),
        })

    async def next_command(
        self, client_id: str | None = None, session_id: str | None = None,
    ) -> GameAction | None:
        async with self._lock:
            if self._owner is None or (client_id, session_id) != self._owner:
                raise GameBridgeError("动作轮询不属于当前游戏客户端/存档会话")
            if time.monotonic() - self._seen_at > self.LEASE_SECONDS:
                raise GameBridgeError("游戏会话状态过期，禁止领取动作")
            pending = self._pending
            if pending is None:
                return None
            action, future, deadline = pending
            if future.done() or asyncio.get_running_loop().time() > deadline:
                self._pending = None
                return None
            self._pending = None  # At most once; a lost response is never replayed.
            return action

    async def receipt(self, action_id: str, verified: bool, state: dict[str, Any]) -> None:
        async with self._lock:
            if self._identity(state) != self._owner or self._owner is None:
                raise GameBridgeError("游戏动作回执来自其他客户端或存档会话")
            source_seq = self._source_sequence(state)
            future = self._inflight.pop(action_id, None)
            if future is None:
                raise GameBridgeError("未知或过期的游戏动作")
            # Late receipts can settle their own action, but cannot replace newer telemetry.
            current = self._snapshot.state if self._snapshot is not None else {}
            merged = dict(current)
            if source_seq > self._source_seq:
                merged = merge_observation(current, state)
                self._source_seq = source_seq
            merged["last_action"] = {
                "action_id": action_id, "status": "verified" if verified else "unverified",
            }
            self._seen_at = time.monotonic()
            self._stamp(merged, full=False)
            if not future.done():
                future.set_result(verified)

    async def apply_action(self, action: GameAction) -> None:
        validate_plugin_action(self.game_id, action)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        async with self._lock:
            if (action.client_id, action.session_id) != self._owner or self._owner is None:
                raise GameBridgeError("游戏会话在决策后已切换，禁止派发旧动作")
            if action.save_id != self._save_id:
                raise GameBridgeError("游戏存档在决策后已切换")
            if time.monotonic() - self._seen_at > self.LEASE_SECONDS:
                raise GameBridgeError("游戏状态已过期，禁止派发动作")
            if self._pending is not None or self._inflight:
                raise GameBridgeError("游戏仍在执行上一动作")
            self._inflight[action.action_id] = future
            self._pending = (action, future, loop.time() + 8)
        try:
            await asyncio.wait_for(future, timeout=8)
        except TimeoutError as exc:
            raise GameBridgeError("游戏动作执行超时，结果需核对") from exc
        finally:
            async with self._lock:
                if self._pending is not None and self._pending[0].action_id == action.action_id:
                    self._pending = None
                self._inflight.pop(action.action_id, None)

    def verify_action(
        self, action: GameAction, before: GameSnapshot, after: GameSnapshot,
    ) -> tuple[bool, dict[str, Any]]:
        receipt = after.state.get("last_action")
        same_session = self._identity(before.state) == self._identity(after.state) == (
            action.client_id, action.session_id,
        ) and before.state.get("save_id") == after.state.get("save_id") == action.save_id
        verified = (
            same_session and isinstance(receipt, dict)
            and receipt.get("action_id") == action.action_id
            and receipt.get("status") == "verified"
            and after.observation_seq > before.observation_seq
        )
        return verified, {"last_action": receipt, "same_session": same_session,
                          "observation_seq": after.observation_seq}
