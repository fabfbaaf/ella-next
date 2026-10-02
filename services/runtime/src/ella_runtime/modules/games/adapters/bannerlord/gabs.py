"""Direct Bannerlord control through the old project's GABS protocol."""

from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ella_runtime.modules.games.adapters.bannerlord.gabs_client import GabsError, GabsMcpClient
from ella_runtime.modules.games.adapters.bannerlord.risk import (
    BannerlordCapability,
    BannerlordToolRisk,
    capability_group,
    classify_tool_risk,
    effect_preview,
    preview_supported_tool,
    schema_fingerprint,
    tool_schema,
    validate_tool_arguments,
)
from ella_runtime.modules.games.adapters.bannerlord.verification import (
    observer_categories_for_tool,
    verify_bannerlord_action,
)
from ella_runtime.modules.games.bridge import GameBridgeError
from ella_runtime.modules.games.contracts import GameAction, GameMode, GameSnapshot

READ_TOOLS = (
    "bannerlord_core_get_game_state",
    "bannerlord_hero_get_player",
    "bannerlord_party_get_player_party",
)
PREVIEW_LIFETIME = timedelta(minutes=5)


@dataclass(frozen=True)
class HighImpactPreview:
    id: str
    action: GameAction
    group: BannerlordCapability
    save_id: str
    schema_hash: str
    effect: str
    expires_at: datetime


def current_save_id(state: dict[str, Any]) -> str | None:
    """Only explicit, structured GABS campaign IDs can scope a permission."""
    core = state.get("bannerlord_core_get_game_state")
    if not isinstance(core, dict):
        return None
    for candidate in (core, core.get("campaign"), core.get("save")):
        if not isinstance(candidate, dict):
            continue
        for key in ("saveId", "save_id"):
            value = candidate.get(key)
            if isinstance(value, str) and 1 <= len(value) <= 200:
                return value
        campaign = candidate.get("campaignId") or candidate.get("campaign_id")
        slot = candidate.get("saveName") or candidate.get("save_name") or candidate.get("saveSlot")
        if isinstance(campaign, str) and isinstance(slot, str) and (
            1 <= len(campaign) <= 200 and 1 <= len(slot) <= 200
        ):
            return f"{campaign} / {slot}"
    return None


def _tool_names(payload: Any) -> list[str]:
    if isinstance(payload, str):
        try:
            return _tool_names(json.loads(payload))
        except json.JSONDecodeError:
            return [line.strip(" -*`") for line in payload.splitlines() if "bannerlord" in line.lower()]
    if isinstance(payload, list):
        return [item if isinstance(item, str) else str(item.get("name") or item.get("tool"))
                for item in payload if isinstance(item, (str, dict))]
    if isinstance(payload, dict):
        for key in ("tools", "names", "toolNames", "items", "result"):
            if key in payload:
                names = _tool_names(payload[key])
                if names:
                    return names
    return []


class BannerlordGabsBridge:
    game_id = "bannerlord"
    mode = GameMode.API_CONTROL

    def __init__(self, client: GabsMcpClient | None = None) -> None:
        self.client = client or GabsMcpClient()
        self._ready = False
        self._lock = asyncio.Lock()
        self._approval_lock = asyncio.Lock()
        self._tools: list[str] = []
        self._details: dict[str, Any] = {}
        self._catalog: list[dict[str, Any]] | None = None
        self._last_action: dict[str, Any] | None = None
        self._previews: dict[str, HighImpactPreview] = {}
        self._approved_preview: HighImpactPreview | None = None
        self._approved_task: asyncio.Task[Any] | None = None

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            try:
                await self.client.start()
                try:
                    await self.client.call_tool(
                        "games_connect", {"gameId": "bannerlord", "forceTakeover": False}
                    )
                except GabsError:
                    pass  # Already connected is common; tool discovery is the readiness check.
                names = _tool_names(await self.client.call_tool(
                    "games_tool_names", {"gameId": "bannerlord", "brief": True}
                ))
                self._tools = sorted({name for name in names if name.startswith("bannerlord_")})
                if not self._tools:
                    raise GabsError("没有发现骑砍游戏工具")
                self._catalog = None
                self._details.clear()
                self._previews.clear()
                self._approved_preview = None
                self._ready = True
            except GabsError as exc:
                raise GameBridgeError("骑砍 GABS 未连接或没有游戏工具") from exc

    async def _game_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            return await self.client.call_tool(
                "games_call_tool", {"tool": name, "arguments": arguments}
            )
        except GabsError as exc:
            self._ready = False
            raise GameBridgeError("骑砍工具调用失败，动作结果需要核对") from exc

    async def _read_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {}
        for name in READ_TOOLS:
            if name not in self._tools:
                continue
            try:
                state[name] = await self._game_tool(name, {})
            except GameBridgeError:
                state[name] = {"error": "unavailable"}
        if not state or all(isinstance(value, dict) and "error" in value for value in state.values()):
            raise GameBridgeError("骑砍没有返回可用的实时状态")
        return state

    async def observe(self) -> GameSnapshot:
        await self._ensure_ready()
        state = await self._read_state()
        state["available_actions"] = await self.available_actions()
        state["save_id"] = current_save_id(state)
        core = state.get("bannerlord_core_get_game_state", {})
        live = isinstance(core, dict) and (state["save_id"] is not None or any(
            core.get(key) is True for key in ("isCampaignActive", "isInGame", "campaignLoaded")
        ) or str(core.get("gameState", core.get("state", ""))).casefold() in {
            "campaign", "battle", "mission", "in_game", "playing",
        })
        state["bridge_ready"] = bool(state["available_actions"]) and live
        if self._last_action is not None:
            state["last_action"] = self._last_action
        return GameSnapshot(game_id=self.game_id, captured_at=datetime.now(UTC), state=state)

    async def available_actions(self) -> list[dict[str, Any]]:
        await self._ensure_ready()
        if self._catalog is not None:
            return self._catalog
        catalog: list[dict[str, Any]] = []
        for name in self._tools:
            try:
                detail = await self._detail(name)
            except GameBridgeError:
                continue
            risk = classify_tool_risk(name, detail)
            schema = tool_schema(detail)
            if risk in {BannerlordToolRisk.READ, BannerlordToolRisk.HIGH_IMPACT_GAME_ACTION} or schema is None:
                continue
            payload = detail if isinstance(detail, dict) else {}
            nested = payload.get("tool") if isinstance(payload.get("tool"), dict) else {}
            catalog.append({
                "name": name,
                "description": str(payload.get("description") or nested.get("description") or ""),
                "parameters": schema,
                "risk": risk.value,
            })
        self._catalog = catalog
        return catalog

    async def _detail(self, name: str) -> Any:
        if name not in self._details:
            try:
                self._details[name] = await self.client.call_tool(
                    "games_tool_detail", {"tool": name}
                )
            except GabsError as exc:
                raise GameBridgeError("无法读取骑砍工具的真实参数结构") from exc
        return self._details[name]

    async def high_impact_actions(self) -> dict[str, Any]:
        await self._ensure_ready()
        state = await self._read_state()
        actions: list[dict[str, Any]] = []
        for name in self._tools:
            try:
                detail = await self._detail(name)
            except GameBridgeError:
                continue
            if classify_tool_risk(name, detail) != BannerlordToolRisk.HIGH_IMPACT_GAME_ACTION:
                continue
            group = capability_group(name, detail)
            schema = tool_schema(detail)
            actions.append({
                "name": name, "group": group.value, "parameters": schema,
                "can_preview": schema is not None and preview_supported_tool(name, group),
            })
        return {"save_id": current_save_id(state), "actions": actions}

    async def preview_high_impact(self, action: GameAction) -> dict[str, Any]:
        await self._ensure_ready()
        if action.name not in self._tools:
            raise GameBridgeError("骑砍工具不在当前游戏会话中")
        detail = await self._detail(action.name)
        if classify_tool_risk(action.name, detail) != BannerlordToolRisk.HIGH_IMPACT_GAME_ACTION:
            raise GameBridgeError("此动作属于普通游玩，不需要高影响许可")
        schema = tool_schema(detail)
        if schema is None:
            raise GameBridgeError("工具参数结构不可验证，禁止授权")
        try:
            validate_tool_arguments(schema, action.parameters)
        except (TypeError, ValueError) as exc:
            raise GameBridgeError(str(exc)) from exc
        group = capability_group(action.name, detail)
        effect = effect_preview(action.name, group, action.parameters)
        if effect is None:
            raise GameBridgeError("此动作的具体效果无法安全预览，禁止授权")
        save_id = current_save_id(await self._read_state())
        if save_id is None:
            raise GameBridgeError("GABS 未提供可验证的当前存档 ID，禁止高影响操作")
        now = datetime.now(UTC)
        self._previews = {
            key: value for key, value in self._previews.items() if value.expires_at > now
        }
        if len(self._previews) >= 20:
            raise GameBridgeError("待确认的高影响预览过多，请稍后重试")
        preview = HighImpactPreview(
            id=secrets.token_urlsafe(24), action=action, group=group, save_id=save_id,
            schema_hash=schema_fingerprint(schema), effect=effect,
            expires_at=now + PREVIEW_LIFETIME,
        )
        self._previews[preview.id] = preview
        return {
            "preview_id": preview.id, "save_id": preview.save_id, "group": preview.group.value,
            "effect": preview.effect, "tool": action.name, "arguments": action.parameters,
            "expires_at": preview.expires_at.isoformat(),
        }

    async def execute_high_impact(
        self, preview_id: str, controller: Any,
    ) -> Any:
        preview = self._previews.pop(preview_id, None)
        if preview is None or datetime.now(UTC) >= preview.expires_at:
            raise GameBridgeError("高影响操作预览已失效，请重新预览")
        async with self._approval_lock:
            if self._approved_preview is not None:
                raise GameBridgeError("已有一项高影响操作正在执行")
            self._approved_preview = preview
            self._approved_task = asyncio.current_task()
            try:
                return await controller.perform(preview.action)
            finally:
                self._approved_preview = None
                self._approved_task = None

    def bind_action(self, action: GameAction, before: GameSnapshot) -> GameAction:
        # Exact approved actions retain their preview identity; the preview itself
        # already binds the save and is rechecked immediately before dispatch.
        if (
            self._approved_preview is not None and self._approved_preview.action == action
            and self._approved_task is asyncio.current_task()
        ):
            return action
        save_id = current_save_id(before.state)
        if action.save_id is not None and action.save_id != save_id:
            raise GameBridgeError("骑砍存档在决策后已切换，禁止执行旧动作")
        return action.model_copy(update={"save_id": save_id}) if save_id is not None else action

    def _allows_save_change(self, action: GameAction) -> bool:
        detail = self._details.get(action.name)
        return classify_tool_risk(action.name, detail) == BannerlordToolRisk.HIGH_IMPACT_GAME_ACTION and (
            capability_group(action.name, detail) in {
                BannerlordCapability.SAVE, BannerlordCapability.LOAD, BannerlordCapability.NEW_GAME,
            }
        )

    async def preflight_action(self, action: GameAction, before: GameSnapshot) -> None:
        await self._ensure_ready()
        if action.name not in self._tools:
            raise GameBridgeError("骑砍工具不在当前游戏会话的工具列表中")
        detail = await self._detail(action.name)
        schema = tool_schema(detail)
        if schema is None:
            raise GameBridgeError("骑砍工具缺少可验证的参数结构")
        try:
            validate_tool_arguments(schema, action.parameters)
        except (TypeError, ValueError) as exc:
            raise GameBridgeError(str(exc)) from exc
        if action.save_id is not None and action.save_id != current_save_id(before.state):
            raise GameBridgeError("骑砍存档在决策后已切换，禁止执行旧动作")
        risk = classify_tool_risk(action.name, detail)
        if risk == BannerlordToolRisk.READ:
            raise GameBridgeError("只读工具不能作为游戏动作执行")
        if risk != BannerlordToolRisk.HIGH_IMPACT_GAME_ACTION:
            return
        preview = self._approved_preview
        if (
            preview is None or self._approved_task is not asyncio.current_task()
            or datetime.now(UTC) >= preview.expires_at
            or preview.action != action
            or preview.group != capability_group(action.name, detail)
            or preview.schema_hash != schema_fingerprint(schema)
            or preview.save_id != current_save_id(before.state)
        ):
            raise GameBridgeError("高影响骑砍动作未获当前存档的单次许可")

    async def _targeted_observers(self, action: GameAction) -> dict[str, Any]:
        observed = {}
        for category in observer_categories_for_tool(action.name):
            name = "bannerlord_" + category
            if name not in self._tools:
                continue
            try:
                detail = await self._detail(name)
                schema = tool_schema(detail)
                if classify_tool_risk(name, detail) == BannerlordToolRisk.READ and schema is not None and not schema.get("required"):
                    observed[name] = await self._game_tool(name, {})
            except GameBridgeError:
                continue
        return observed

    async def apply_action(self, action: GameAction) -> None:
        before = await self._read_state()
        await self.preflight_action(
            action, GameSnapshot(game_id=self.game_id, captured_at=datetime.now(UTC), state=before)
        )
        risk = classify_tool_risk(action.name, self._details[action.name])
        specific_before = await self._targeted_observers(action)
        # Observer calls can yield while the user loads another save. Check again
        # after them, at the final client-side boundary before the mutating call.
        dispatch = await self._read_state()
        if current_save_id(dispatch) != current_save_id(before):
            raise GameBridgeError("骑砍存档在动作准备期间已切换，禁止执行旧动作")
        await self.preflight_action(
            action, GameSnapshot(game_id=self.game_id, captured_at=datetime.now(UTC), state=dispatch)
        )
        if risk == BannerlordToolRisk.HIGH_IMPACT_GAME_ACTION:
            self._approved_preview = None  # One action, one permission, even if GABS later fails.
        result = await self._game_tool(action.name, action.parameters)
        specific_after = await self._targeted_observers(action)
        after = await self._read_state()
        decision = verify_bannerlord_action(
            action.name,
            risk=risk,
            result=result,
            before=before,
            after=after, observer_before=specific_before, observer_after=specific_after,
        )
        same_save = current_save_id(before) == current_save_id(after)
        verified = decision.verified and (same_save or self._allows_save_change(action))
        self._last_action = {
            "action_id": action.action_id,
            "status": "verified" if verified else "unverified",
            "strategy": decision.strategy,
            "confidence": decision.confidence,
            "save_id": current_save_id(before),
            "save_consistent": same_save,
        }

    def verify_action(
        self, action: GameAction, before: GameSnapshot, after: GameSnapshot
    ) -> tuple[bool, dict[str, Any]]:
        receipt = after.state.get("last_action")
        before_save = current_save_id(before.state)
        after_save = current_save_id(after.state)
        save_consistent = before_save == after_save or self._allows_save_change(action)
        verified = (
            isinstance(receipt, dict)
            and receipt.get("action_id") == action.action_id
            and receipt.get("status") == "verified"
            and receipt.get("save_id") == before_save
            and (action.save_id is None or action.save_id == before_save)
            and save_consistent
            and after.captured_at >= before.captured_at
        )
        return verified, {"last_action": receipt, "save_consistent": save_consistent}
