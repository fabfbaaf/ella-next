from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .risk import BannerlordToolRisk

_MISSING = object()


def _explicit_failure(result: Any) -> bool:
    """A game-level rejection overrides telemetry drift and claimed evidence."""
    if not isinstance(result, dict):
        return False
    if any(result.get(key) is False for key in (
        "success", "ok", "accepted", "verified", "postconditionVerified",
    )) or result.get("isError") is True:
        return True
    if result.get("error") not in (None, False, "", {}, []):
        return True
    status = result.get("status")
    if isinstance(status, str) and status.casefold() in {
        "failed", "failure", "error", "rejected", "unverified", "cancelled", "canceled",
    }:
        return True
    return any(_explicit_failure(result.get(key)) for key in ("result", "data"))


def _find_first(value: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if key in value:
                return value[key]
        for child in value.values():
            found = _find_first(child, keys)
            if found is not _MISSING:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_first(child, keys)
            if found is not _MISSING:
                return found
    return _MISSING


def _changed(before: Any, after: Any, keys: tuple[str, ...]) -> bool:
    left = _find_first(before, keys)
    right = _find_first(after, keys)
    return left is not _MISSING and right is not _MISSING and left != right


@dataclass(slots=True)
class VerificationDecision:
    verified: bool
    confidence: float
    reason: str
    strategy: str
    evidence: dict[str, Any]


def verify_bannerlord_action(
    tool: str,
    *,
    risk: BannerlordToolRisk,
    result: Any,
    before: dict[str, Any],
    after: dict[str, Any],
    observer_before: dict[str, Any] | None = None,
    observer_after: dict[str, Any] | None = None,
) -> VerificationDecision:
    name = tool.lower().replace("/", "_").replace(".", "_").replace("-", "_")
    observer_before = observer_before or {}
    observer_after = observer_after or {}
    merged_before = {"baseline": before, "specific": observer_before}
    merged_after = {"baseline": after, "specific": observer_after}

    if _explicit_failure(result):
        return VerificationDecision(
            False, 1.0, "The Bannerlord tool explicitly rejected or failed the action.",
            "explicit_failure", {"explicit_failure": True},
        )

    if risk == BannerlordToolRisk.READ:
        return VerificationDecision(True, 1.0, "Read-only Bannerlord tool returned structured data.", "read_only", {})

    rules: list[tuple[str, tuple[str, ...], str]] = [
        ("movement", ("settlement", "settlementName", "currentSettlement", "position", "targetSettlement", "target"), "move_to_"),
        ("trade", ("gold", "inventory", "items", "itemCount", "quantity"), "trade_"),
        ("inventory", ("gold", "inventory", "items", "itemCount", "quantity"), "inventory_"),
        ("recruitment", ("partySize", "memberCount", "troopCount", "troops", "roster"), "recruit"),
        ("conversation", ("conversation", "dialogue", "currentLine", "options", "conversationState"), "conversation_"),
        ("battle", ("battle", "mission", "formation", "enemy", "casualties", "battleState"), "battle_"),
        ("diplomacy", ("wars", "war", "peace", "relation", "kingdom", "clan", "faction"), "diplomacy_"),
        ("kingdom", ("wars", "war", "peace", "relation", "kingdom", "clan", "faction"), "kingdom_"),
        ("smithing", ("inventory", "items", "materials", "weapon", "smithing"), "smith"),
        ("ui", ("screen", "menu", "widget", "viewModel", "viewmodel", "inquiry"), "ui_"),
        ("menu", ("screen", "menu", "options", "currentMenu"), "menu_"),
        ("save_load", ("state", "gameState", "save", "campaign", "screen"), "load_save"),
    ]
    for strategy, keys, token in rules:
        if token in name and _changed(merged_before, merged_after, keys):
            return VerificationDecision(
                True, 1.0, f"{strategy} post-condition changed after the Bannerlord action.", strategy,
                {"changed_keys": list(keys)},
            )

    if isinstance(result, dict) and (
        result.get("verified") is True
        or result.get("postconditionVerified") is True
        or result.get("observed") not in (None, False, {}, [])
    ):
        return VerificationDecision(
            True, 0.85, "The upstream Bannerlord tool returned explicit verification evidence.", "upstream_evidence", {}
        )

    # A live game changes continuously. A generic before/after delta is evidence worth
    # retaining, but it is not a post-condition and must never prove that our action
    # succeeded. Targeted observers are treated the same way unless one of the rules
    # above matched the action's expected state.
    if observer_before != observer_after and observer_after:
        return VerificationDecision(
            False,
            0.70,
            "A targeted Bannerlord observer changed, but no action-specific post-condition matched; natural game changes may be responsible.",
            "observer_change_unproven",
            {"observer_changed": True},
        )
    if before != after:
        return VerificationDecision(
            False,
            0.62,
            "The live Bannerlord state changed, but generic state drift is not accepted as proof of action success.",
            "baseline_change_unproven",
            {"baseline_changed": True},
        )
    return VerificationDecision(
        False,
        0.50 if risk == BannerlordToolRisk.HIGH_IMPACT_GAME_ACTION else 0.58,
        "GABS returned from the action, but no reliable Bannerlord post-condition was observed; the action was not retried.",
        "unproven",
        {},
    )


def observer_categories_for_tool(tool: str) -> tuple[str, ...]:
    name = tool.lower().replace("/", "_").replace(".", "_").replace("-", "_")
    if "conversation_" in name:
        return ("conversation_get_state",)
    if "battle_" in name:
        return ("battle_get_state",)
    if "diplomacy_" in name:
        return ("kingdom_list_wars", "hero_get_relationships")
    if "kingdom_" in name:
        return ("kingdom_list_wars",)
    if "trade_" in name or "inventory_" in name or "smith" in name:
        return ("inventory_get_inventory", "hero_get_player")
    if "recruit" in name or "party_" in name:
        return ("party_get_player_party", "party_get_troop_roster")
    if "ui_" in name:
        return ("ui_get_screen",)
    if "menu_" in name:
        return ("menu_get_current",)
    if "load_save" in name or "save_game" in name:
        return ("core_get_game_state",)
    return ()
