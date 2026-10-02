"""Loopback bridge contract for game plugins with observable actions."""

from __future__ import annotations

import os
from urllib.parse import urlparse

import httpx

from ella_runtime.modules.games.contracts import GameAction, GameMode, GameSnapshot


class GameBridgeError(RuntimeError):
    """The installed game bridge is unavailable or returned invalid state."""


GAME_NAMES = {
    "minecraft": "我的世界",
    "stardew_valley": "星露谷物语",
    "bannerlord": "骑马与砍杀 II",
}


def bridge_url(game_id: str) -> str | None:
    if game_id not in GAME_NAMES:
        return None
    variable = f"ELLA_GAME_{game_id.upper()}_BRIDGE_URL"
    value = os.getenv(variable, "").strip().rstrip("/")
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise GameBridgeError("游戏桥接地址只能是本机 HTTP 服务")
    return value


class LocalGameBridge:
    mode = GameMode.API_CONTROL

    def __init__(
        self, game_id: str, base_url: str,
        client: httpx.AsyncClient | None = None, token: str | None = None,
    ) -> None:
        if game_id not in GAME_NAMES:
            raise ValueError("未知游戏")
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("游戏桥接必须运行在本机")
        self.game_id = game_id
        self.base_url = base_url.rstrip("/")
        self.client = client
        self.token = token

    async def _request(self, method: str, suffix: str, payload: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            if self.client is None:
                async with httpx.AsyncClient(timeout=3) as client:
                    response = await client.request(
                        method, self.base_url + suffix, json=payload, headers=headers
                    )
            else:
                response = await self.client.request(
                    method, self.base_url + suffix, json=payload, headers=headers
                )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise TypeError("非对象响应")
            return result
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise GameBridgeError("游戏桥接未连接或返回格式无效") from exc

    async def observe(self) -> GameSnapshot:
        data = await self._request("GET", "/snapshot")
        try:
            snapshot = GameSnapshot.model_validate(data)
        except ValueError as exc:
            raise GameBridgeError("游戏桥接状态格式无效") from exc
        if snapshot.game_id != self.game_id or snapshot.captured_at.tzinfo is None:
            raise GameBridgeError("游戏桥接返回了错误游戏或无时间戳的状态")
        return snapshot

    async def apply_action(self, action: GameAction) -> None:
        data = await self._request("POST", "/actions", action.model_dump(mode="json"))
        if data.get("action_id") != action.action_id or data.get("accepted") is not True:
            raise GameBridgeError("游戏桥接未接受动作")

    def verify_action(
        self, action: GameAction, before: GameSnapshot, after: GameSnapshot
    ) -> tuple[bool, dict]:
        receipt = after.state.get("last_action")
        verified = (
            isinstance(receipt, dict)
            and receipt.get("action_id") == action.action_id
            and receipt.get("status") == "verified"
            and after.captured_at >= before.captured_at
        )
        return verified, {
            "last_action": receipt if isinstance(receipt, dict) else None,
            "before": before.captured_at.isoformat(),
            "after": after.captured_at.isoformat(),
        }
