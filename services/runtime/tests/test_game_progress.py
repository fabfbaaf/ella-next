import asyncio
from datetime import UTC, datetime

import pytest

from ella_runtime.modules.games.contracts import GameAction, GameMode, GameSnapshot
from ella_runtime.modules.games.controller import GameController
from ella_runtime.modules.games.journal import GameJournal
from ella_runtime.modules.games.play import (
    GamePlayError,
    GamePlayManager,
    GamePlaySession,
    track_progress,
)


class Gateway:
    class Settings:
        @staticmethod
        def for_purpose(_purpose):
            return type("Config", (), {"configured": True})()

    settings = Settings()

    def __init__(self, name="move"):
        self.calls = 0
        self.name = name

    async def generate(self, _request):
        self.calls += 1
        params = '{"direction":"forward","ticks":5}' if self.name == "move" else '{"ticks":5}'
        return type("Response", (), {"text": f'{{"name":"{self.name}","parameters":{params}}}'})()


class ObservedGame:
    game_id = "minecraft"
    mode = GameMode.API_CONTROL

    def __init__(self, *, advances=False, catalog=None):
        self.position = 0
        self.seq = 0
        self.actions = 0
        self.advances = advances
        self.catalog = catalog

    async def observe(self):
        self.seq += 1
        state = {
            "client_id": "client", "session_id": "session", "save_id": "save",
            "position": self.position, "observation_seq": self.seq,
            "source_observation_seq": self.seq,
            "timestamp": datetime.now(UTC).isoformat(),
            "last_action": {"action_id": str(self.seq), "status": "verified"},
        }
        if self.catalog is not None:
            state["available_actions"] = self.catalog
        return GameSnapshot(game_id=self.game_id, captured_at=datetime.now(UTC), state=state)

    async def apply_action(self, _action):
        self.actions += 1
        self.position += int(self.advances)

    def verify_action(self, _action, _before, _after):
        # A verified input can still be blocked by terrain; receipts are not progress.
        return True, {"input_completed": True}


def manager_and_controller(tmp_path, game, gateway):
    journal = GameJournal(tmp_path / "game.sqlite3")
    return GamePlayManager(gateway, journal=journal), GameController(game, journal)


def test_verified_inputs_with_changing_telemetry_pause_when_no_game_progress(tmp_path):
    async def run():
        game, gateway = ObservedGame(), Gateway()
        manager, controller = manager_and_controller(tmp_path, game, gateway)
        session = await manager.start("minecraft", "向前移动", controller, max_steps=10)
        await manager.tasks["minecraft"]
        assert session.status == "paused"
        assert session.step == session.stagnant_steps == game.actions == 4
        assert session.pending_action_id == ""
        assert "观测没有变化" in session.error
        restored = GamePlayManager(gateway, journal=manager.journal).sessions["minecraft"]
        assert restored.stagnant_steps == 4 and restored.decision_calls == 4
        game.advances = True
        await manager.resume("minecraft", controller)
        await manager.tasks["minecraft"]
        assert session.status == "step_limit" and session.step == 10
        assert session.error == "" and session.stagnant_steps == 0

    asyncio.run(run())


@pytest.mark.parametrize("name", ["wait", "look", "turn", "face", "bannerlord_camera_move"])
def test_wait_and_camera_actions_are_not_progress_stalls(name):
    session = GamePlaySession("minecraft", "goal", 10)
    for index in range(6):
        action = GameAction(action_id=str(index), name=name)
        assert not track_progress(session, action, {"position": 0}, {"position": 0})
    assert session.stagnant_steps == 0


def test_action_or_game_fact_change_resets_stall_count():
    session = GamePlaySession("minecraft", "goal", 10, stall_threshold=2)
    first = GameAction(action_id="one", name="move", parameters={"direction": "left"})
    second = GameAction(action_id="two", name="move", parameters={"direction": "right"})
    assert not track_progress(session, first, {"position": 0}, {"position": 0})
    assert not track_progress(session, second, {"position": 0}, {"position": 0})
    assert not track_progress(session, second, {"position": 0}, {"position": 1})
    assert session.stagnant_steps == 0


def test_nested_telemetry_does_not_mask_tool_stagnation():
    session = GamePlaySession("stardew_valley", "goal", 10, stall_threshold=2)
    action = GameAction(action_id="one", name="use_tool")
    before = {"player": {"x": 1, "y": 2}, "inventory": [], "world": {"ticks": 1}}
    after = {"player": {"x": 1, "y": 2}, "inventory": [], "world": {"ticks": 2}}
    assert not track_progress(session, action, before, after)
    assert track_progress(session, action, before, after)
    after["inventory"] = [{"item": "wood", "count": 1}]
    assert not track_progress(session, action, before, after)


def test_decision_budget_pauses_before_an_extra_paid_request_and_persists(tmp_path):
    async def run():
        game, gateway = ObservedGame(advances=True), Gateway()
        manager, controller = manager_and_controller(tmp_path, game, gateway)
        session = await manager.start("minecraft", "持续前进", controller, max_steps=10, max_decisions=2)
        await manager.tasks["minecraft"]
        assert session.status == "paused" and session.step == 2
        assert session.decision_calls == gateway.calls == game.actions == 2
        assert "预算已用完" in session.error
        restored = GamePlayManager(gateway, journal=manager.journal)
        assert restored.sessions["minecraft"].decision_calls == 2
        with pytest.raises(GamePlayError, match="预算已用完"):
            await restored.resume("minecraft", controller)
        assert gateway.calls == 2

    asyncio.run(run())


def test_two_stage_catalog_costs_two_decisions_and_does_not_dispatch_on_exhaustion(tmp_path):
    async def run():
        catalog = [{"name": "move"}] + [{"name": f"tool_{i}"} for i in range(30)]
        game, gateway = ObservedGame(catalog=catalog), Gateway()
        manager, controller = manager_and_controller(tmp_path, game, gateway)
        session = await manager.start("minecraft", "移动", controller, max_steps=5, max_decisions=1)
        await manager.tasks["minecraft"]
        assert session.status == "paused"
        assert session.step == game.actions == 0
        assert session.decision_calls == gateway.calls == 1
        assert session.pending_action_id == ""

    asyncio.run(run())


@pytest.mark.parametrize("options", [
    {"max_decisions": 0}, {"max_decisions": 401}, {"max_decisions": True},
    {"stall_threshold": 0}, {"stall_threshold": 21}, {"stall_threshold": True},
])
def test_invalid_limits_are_rejected_before_any_model_call(tmp_path, options):
    async def run():
        game, gateway = ObservedGame(), Gateway()
        manager, controller = manager_and_controller(tmp_path, game, gateway)
        with pytest.raises(GamePlayError):
            await manager.start("minecraft", "goal", controller, **options)
        assert gateway.calls == game.actions == 0

    asyncio.run(run())
