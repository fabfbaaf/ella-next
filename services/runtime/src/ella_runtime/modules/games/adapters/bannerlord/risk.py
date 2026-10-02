"""Bannerlord tool authority, based on names and structured schemas only."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any


class BannerlordToolRisk(StrEnum):
    READ = "read"
    SAFE_ACTION = "safe_action"
    STATE_CHANGING = "state_changing"
    HIGH_IMPACT_GAME_ACTION = "high_impact_game_action"


class BannerlordCapability(StrEnum):
    SAVE = "save"
    LOAD = "load"
    NEW_GAME = "new_game"
    DIPLOMACY = "diplomacy"
    CHARACTER = "character"
    CHEAT = "cheat"
    COMMAND = "command"
    UNKNOWN = "unknown"


def _normalized(tool: str) -> str:
    return tool.lower().replace("/", "_").replace(".", "_").replace("-", "_")


def tool_schema(detail: Any) -> dict[str, Any] | None:
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            return None
    if not isinstance(detail, dict):
        return None
    nested = detail.get("tool")
    if not isinstance(nested, dict):
        nested = {}
    schema = detail.get("inputSchema") or nested.get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return None
    if not isinstance(schema.get("properties"), dict):
        return None
    return schema


def schema_fingerprint(schema: dict[str, Any]) -> str:
    encoded = json.dumps(schema, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _dangerous_name(name: str) -> bool:
    return any(tag in name for tag in (
        "declare_war", "make_peace", "change_relation", "leave_kingdom", "create_kingdom",
        "abdicate", "expel", "destroy_", "delete_", "remove_clan", "disband_clan",
        "kill_hero", "execute_hero", "retire_hero", "give_gold", "add_gold", "set_gold",
        "save_game", "save_campaign", "quick_save", "load_save", "load_game", "quick_load",
        "new_game", "new_campaign", "start_campaign", "restart_campaign", "reset_campaign",
        "run_command", "execute_command", "console", "cheat", "run_script", "execute_script",
    ))


def _generic_command_schema(schema: dict[str, Any] | None) -> bool:
    if schema is None:
        return False
    for key, prop in schema["properties"].items():
        normalized = _normalized(key)
        if any(tag in normalized for tag in (
            "command", "console", "script", "expression", "operation", "method", "cheat",
        )) or normalized in {"cmd", "code", "action"}:
            return True
        if isinstance(prop, dict):
            choices = prop.get("enum")
            if isinstance(choices, list) and any(
                isinstance(value, str) and _dangerous_name(_normalized(value))
                for value in choices
            ):
                return True
    return False


def classify_tool_risk(tool: str, detail: Any = None) -> BannerlordToolRisk:
    """Unknown and command-like actions fail closed; descriptions never lower risk."""
    name = _normalized(tool)
    if _dangerous_name(name) or _generic_command_schema(tool_schema(detail)):
        return BannerlordToolRisk.HIGH_IMPACT_GAME_ACTION
    if any(tag in name for tag in (
        "_get_", "_list_", "_check_", "_status", "_ping", "_scan_", "_detect_",
        "_history_", "_quest_list", "_quest_get", "_get_recent_",
    )):
        return BannerlordToolRisk.READ
    if any(tag in name for tag in (
        "move_to_", "move_to_point", "follow_party", "enter_settlement", "leave_settlement",
        "select_option", "continue", "click_widget", "answer_inquiry", "set_time_speed",
        "wait_", "flee_to_safety", "engage_party",
    )):
        return BannerlordToolRisk.SAFE_ACTION
    if any(tag in name for tag in (
        "battle_", "trade_", "inventory_", "recruit_", "conversation_", "smith_",
        "quest_accept", "quest_complete", "ui_", "menu_",
    )):
        return BannerlordToolRisk.STATE_CHANGING
    return BannerlordToolRisk.HIGH_IMPACT_GAME_ACTION


def capability_group(tool: str, detail: Any = None) -> BannerlordCapability:
    name = _normalized(tool)
    if any(tag in name for tag in (
        "new_game", "new_campaign", "start_campaign", "restart_campaign", "reset_campaign",
    )):
        return BannerlordCapability.NEW_GAME
    if any(tag in name for tag in ("load_save", "load_game", "quick_load")):
        return BannerlordCapability.LOAD
    if any(tag in name for tag in ("save_game", "save_campaign", "quick_save")):
        return BannerlordCapability.SAVE
    if any(tag in name for tag in (
        "declare_war", "make_peace", "change_relation", "leave_kingdom", "create_kingdom",
    )):
        return BannerlordCapability.DIPLOMACY
    if any(tag in name for tag in (
        "kill_hero", "execute_hero", "retire_hero", "abdicate", "expel", "remove_clan",
        "disband_clan", "destroy_",
    )):
        return BannerlordCapability.CHARACTER
    if "cheat" in name or any(tag in name for tag in ("give_gold", "add_gold", "set_gold")):
        return BannerlordCapability.CHEAT
    if any(tag in name for tag in (
        "command", "console", "script", "execute_",
    )) or _generic_command_schema(tool_schema(detail)):
        return BannerlordCapability.COMMAND
    return BannerlordCapability.UNKNOWN


def validate_tool_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    """Validate the supported, explicit subset of GABS JSON schemas; reject ambiguity."""
    if any(key in schema for key in ("$ref", "allOf", "anyOf", "oneOf", "not")):
        raise ValueError("骑砍工具参数结构过于复杂，不能安全执行")
    properties = schema.get("properties")
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise TypeError("骑砍工具参数结构无效")
    if set(arguments) - set(properties) or any(key not in arguments for key in required):
        raise ValueError("骑砍动作参数与工具结构不一致")
    for key, value in arguments.items():
        prop = properties[key]
        if not isinstance(prop, dict) or any(
            item in prop for item in ("$ref", "allOf", "anyOf", "oneOf", "not")
        ):
            raise ValueError("骑砍工具参数结构不受支持")
        kind = prop.get("type")
        valid_type = {
            "string": isinstance(value, str),
            "integer": type(value) is int,
            "number": type(value) in {int, float},
            "boolean": type(value) is bool,
        }.get(kind)
        if valid_type is not True:
            raise ValueError(f"骑砍动作参数 {key} 类型无效或不受支持")
        choices = prop.get("enum")
        if choices is not None and (not isinstance(choices, list) or value not in choices):
            raise ValueError(f"骑砍动作参数 {key} 不在允许值中")
        if "const" in prop and value != prop["const"]:
            raise ValueError(f"骑砍动作参数 {key} 不符合固定值")
        if isinstance(value, str):
            if "pattern" in prop:
                raise ValueError("骑砍工具包含尚未支持的文本模式约束")
            if "minLength" in prop and len(value) < prop["minLength"]:
                raise ValueError(f"骑砍动作参数 {key} 过短")
            if "maxLength" in prop and len(value) > prop["maxLength"]:
                raise ValueError(f"骑砍动作参数 {key} 过长")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in prop and value < prop["minimum"]:
                raise ValueError(f"骑砍动作参数 {key} 小于下限")
            if "maximum" in prop and value > prop["maximum"]:
                raise ValueError(f"骑砍动作参数 {key} 超过上限")


def _target(arguments: dict[str, Any], *fragments: str) -> str | None:
    for key, value in arguments.items():
        normalized = _normalized(key)
        if any(fragment in normalized for fragment in fragments) and isinstance(value, (str, int)):
            shown = str(value).strip()
            if shown:
                return shown[:160]
    return None


def preview_supported_tool(tool: str, group: BannerlordCapability) -> bool:
    name = _normalized(tool)
    supported = {
        BannerlordCapability.SAVE: ("save_game", "save_campaign"),
        BannerlordCapability.LOAD: ("load_save", "load_game"),
        BannerlordCapability.NEW_GAME: ("new_game", "new_campaign", "start_campaign"),
        BannerlordCapability.DIPLOMACY: (
            "declare_war", "make_peace", "leave_kingdom", "create_kingdom",
        ),
        BannerlordCapability.CHARACTER: (
            "kill_hero", "execute_hero", "retire_hero", "abdicate", "expel", "remove_clan",
        ),
    }
    return any(token in name for token in supported.get(group, ()))


def effect_preview(
    tool: str, group: BannerlordCapability, arguments: dict[str, Any],
) -> str | None:
    """Only expose actions whose concrete effect can be described from exact parameters."""
    if not preview_supported_tool(tool, group):
        return None
    name = _normalized(tool)
    if group in {BannerlordCapability.SAVE, BannerlordCapability.LOAD}:
        target = _target(arguments, "save", "slot", "name")
        if target is None:
            return None
        if group == BannerlordCapability.SAVE:
            return f"将当前战役保存到 {target}；同名存档可能被覆盖"
        return f"读取存档 {target}；当前未保存的进度可能丢失"
    if group == BannerlordCapability.NEW_GAME:
        return "开始新战役并离开当前战役；当前未保存进度可能丢失"
    if group == BannerlordCapability.DIPLOMACY:
        if "leave_kingdom" in name:
            return "离开当前王国，永久改变当前战役的阵营关系"
        target = _target(arguments, "kingdom", "faction", "target", "name")
        if target is None:
            return None
        if "declare_war" in name:
            return f"向 {target} 宣战，改变当前战役的战争状态"
        if "make_peace" in name:
            return f"与 {target} 议和，改变当前战役的战争状态"
        if "create_kingdom" in name:
            return f"建立王国 {target}，永久改变当前战役的势力状态"
    if group == BannerlordCapability.CHARACTER:
        if "abdicate" in name:
            return "让当前角色退位，永久改变当前战役的统治状态"
        target = _target(arguments, "hero", "clan", "target", "name")
        if target is None:
            return None
        return f"对 {target} 执行 {tool}；角色或家族状态可能永久改变"
    return None
