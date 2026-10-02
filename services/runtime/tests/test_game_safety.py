"""Pure game regressions: no game process, SDK, or remote model is required."""

import asyncio
import json

import pytest

from ella_runtime.modules.games.adapters.bannerlord.risk import BannerlordToolRisk
from ella_runtime.modules.games.adapters.bannerlord.verification import verify_bannerlord_action
from ella_runtime.modules.games.contracts import GameAction
from ella_runtime.modules.games.play import GamePlayManager, GamePlaySession
from ella_runtime.modules.games.push_bridge import (
    available_plugin_actions,
    validate_plugin_action,
)


@pytest.mark.parametrize("result", [
    {"success": False},
    {"accepted": False},
    {"ok": False},
    {"verified": False, "postconditionVerified": True},
    {"success": False, "verified": True, "observed": {"gold": 101}},
    {"error": "not enough gold"},
    {"status": "REJECTED"},
    {"result": {"success": False}},
    {"data": {"isError": True}},
])
def test_explicit_game_failure_overrides_telemetry_drift_and_claimed_evidence(result):
    decision = verify_bannerlord_action(
        "bannerlord_trade_buy_item", risk=BannerlordToolRisk.STATE_CHANGING,
        result=result,
        before={"bannerlord_hero_get_player": {"gold": 100}},
        after={"bannerlord_hero_get_player": {"gold": 101}},
    )
    assert not decision.verified
    assert decision.strategy == "explicit_failure"


def test_nonfailure_metadata_keeps_explicit_positive_evidence():
    decision = verify_bannerlord_action(
        "bannerlord_trade_buy_item", risk=BannerlordToolRisk.STATE_CHANGING,
        result={"success": True, "error": None, "verified": True}, before={}, after={},
    )
    assert decision.verified
    assert decision.strategy == "upstream_evidence"


@pytest.mark.parametrize("done", [True, False])
def test_large_catalog_selection_receives_live_state_before_action_or_completion(done):
    state = {"player": {"horses": 0}, "position": {"x": 4}}
    catalog = [{"name": f"bannerlord_tool_{index}"} for index in range(31)]
    seen = []

    class Gateway:
        async def generate(self, request):
            context = json.loads(request.messages[0].content)
            seen.append(context)
            if len(seen) == 1:
                assert context["state"] == state
                assert context["last_result"] == "verified"
                payload = {"done": True} if done else {"name": "bannerlord_tool_1"}
            else:
                assert context["state"] == state
                payload = {"name": "bannerlord_tool_1", "parameters": {}}
            return type("Reply", (), {"text": json.dumps(payload)})()

    async def run():
        manager = GamePlayManager(Gateway())
        session = GamePlaySession("bannerlord", "购买一匹马", 5, last_result="verified")
        return await manager._decide(session, state, catalog)

    decision = asyncio.run(run())
    assert len(seen) == (1 if done else 2)
    assert decision == ({"done": True} if done else {"name": "bannerlord_tool_1", "parameters": {}})


def test_stardew_dialogue_catalog_cannot_issue_movement_or_tool_use():
    state = {
        "player_free": False, "dialogue_can_continue": True, "menu_closable": True,
        "capabilities": ["move", "face", "use_tool", "interact", "select_slot",
                         "dialogue_continue", "close_menu", "wait"],
    }
    names = {item["name"] for item in available_plugin_actions("stardew_valley", state)}
    assert names == {"dialogue_continue", "close_menu", "wait"}
    state.update(dialogue_can_continue=False, menu_closable=False)
    assert {item["name"] for item in available_plugin_actions("stardew_valley", state)} == {"wait"}


def test_stardew_free_player_can_select_slot_without_unsupported_menu_actions():
    catalog = available_plugin_actions("stardew_valley", {"player_free": True})
    names = {item["name"] for item in catalog}
    assert "select_slot" in names
    assert "close_menu" not in names
    assert "dialogue_continue" not in names
    action = GameAction(action_id="slot", name="select_slot", parameters={"slot": 0})
    validate_plugin_action("stardew_valley", action)
    with pytest.raises(RuntimeError):
        validate_plugin_action("stardew_valley", action.model_copy(update={"parameters": {"slot": 36}}))


def test_catalog_respects_older_plugin_capabilities():
    catalog = available_plugin_actions(
        "stardew_valley", {"player_free": True, "capabilities": ["move", "wait"]},
    )
    assert {item["name"] for item in catalog} == {"move", "wait"}
