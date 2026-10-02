import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from ella_runtime.modules.games.contracts import (
    ActionStatus,
    GameAction,
    GameMode,
    GameSnapshot,
)
from ella_runtime.modules.games.controller import GameControlError, GameController
from ella_runtime.modules.games.journal import GameJournal


class FakeGame:
    game_id = "minecraft"

    def __init__(self, mode=GameMode.API_CONTROL):
        self.mode = mode
        self.position = 0
        self.actions = 0
        self.fail_after_action = False
        self.stale = False

    async def observe(self):
        return GameSnapshot(
            game_id=self.game_id,
            captured_at=datetime.now(UTC) - (timedelta(seconds=30) if self.stale else timedelta()),
            state={"position": self.position},
        )

    async def apply_action(self, action):
        self.actions += 1
        self.position += action.parameters["steps"]
        if self.fail_after_action:
            raise ConnectionError("bridge response lost")

    def verify_action(self, action, before, after):
        moved = after.state["position"] - before.state["position"]
        return moved == action.parameters["steps"], {"movement": moved}


def test_game_action_observes_and_verifies_once(tmp_path):
    adapter = FakeGame()
    journal = GameJournal(tmp_path / "actions.sqlite3")
    controller = GameController(adapter, journal)
    action = GameAction(action_id="move-1", name="move", parameters={"steps": 3})
    result = asyncio.run(controller.perform(action))
    assert result.status == ActionStatus.VERIFIED
    assert result.before.state["position"] == 0
    assert result.after.state["position"] == 3
    assert journal.get(action.action_id)[3].status == ActionStatus.VERIFIED
    with pytest.raises(GameControlError, match="重复执行"):
        asyncio.run(controller.perform(action))
    assert adapter.actions == 1


def test_unknown_game_result_reconciles_without_replaying(tmp_path):
    adapter = FakeGame()
    adapter.fail_after_action = True
    controller = GameController(adapter, GameJournal(tmp_path / "actions.sqlite3"))
    action = GameAction(action_id="move-2", name="move", parameters={"steps": 2})
    result = asyncio.run(controller.perform(action))
    assert result.status == ActionStatus.UNKNOWN
    assert result.evidence["replay_allowed"] is False
    verified = asyncio.run(controller.reconcile(action.action_id))
    assert verified.status == ActionStatus.VERIFIED
    assert verified.evidence["replayed"] is False
    assert adapter.actions == 1


def test_screen_mode_stale_state_and_foreground_guard(tmp_path):
    action = GameAction(action_id="move-3", name="move", parameters={"steps": 1})
    screen = FakeGame(GameMode.SCREEN_CHAT)
    with pytest.raises(GameControlError, match="屏幕聊天"):
        asyncio.run(
            GameController(screen, GameJournal(tmp_path / "screen.sqlite3")).perform(action)
        )
    assert screen.actions == 0

    script = FakeGame(GameMode.SCRIPT_CONTROL)
    controller = GameController(script, GameJournal(tmp_path / "script.sqlite3"))
    with pytest.raises(GameControlError, match="前台"):
        asyncio.run(controller.perform(action))
    assert script.actions == 0

    script.stale = True
    foreground = GameController(
        script,
        GameJournal(tmp_path / "script.sqlite3"),
        script_window_is_foreground=lambda game_id: game_id == "minecraft",
    )
    with pytest.raises(GameControlError, match="过期"):
        asyncio.run(foreground.perform(action))
    assert script.actions == 0
