import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from ella_runtime.modules.games.contracts import (
    ActionStatus,
    GameAction,
    GameMode,
    GameSnapshot,
)
from ella_runtime.modules.games.controller import GameController
from ella_runtime.modules.games.journal import GameJournal
from ella_runtime.modules.games.play import GamePlayManager, GamePlaySession
from ella_runtime.modules.games.push_bridge import PushGameBridge


def test_push_bridge_delivers_once_and_verifies_real_plugin_receipt(tmp_path):
    async def run():
        bridge = PushGameBridge("minecraft", data_dir=tmp_path)
        bridge.authenticate(f"Bearer {bridge.token}")
        identity = {"client_id": "one", "session_id": "session", "save_id": "world"}
        await bridge.publish({**identity, "observation_seq": 1, "integration": "fabric", "capabilities": ["move"], "position": 0, "full_snapshot": True, "player": {"health": 20, "food": 18}})
        controller = GameController(bridge, GameJournal(tmp_path / "actions.sqlite3"))
        action = GameAction(
            action_id="move-once", name="move",
            parameters={"direction": "forward", "ticks": 10},
        )
        task = asyncio.create_task(controller.perform(action))
        command = None
        for _ in range(30):
            await asyncio.sleep(0.01)
            command = await bridge.next_command("one", "session")
            if command:
                break
        assert command.name == action.name
        assert command.client_id == "one" and command.session_id == "session"
        assert await bridge.next_command("one", "session") is None
        await bridge.receipt(action.action_id, True, {**identity, "observation_seq": 2, "position": 1})
        result = await task
        assert result.status == ActionStatus.VERIFIED
        assert result.after.state["position"] == 1
        assert result.after.state["player"] == {"health": 20, "food": 18}
        assert result.after.state["capabilities"] == ["move"]
        next_tick = asyncio.create_task(bridge.wait_for_telemetry(result.after))
        await bridge.publish({**identity, "observation_seq": 3, "full_snapshot": True, "player": {"health": 20, "food": 17}, "position": 1})
        await next_tick
        with pytest.raises(RuntimeError, match="未知或过期"):
            await bridge.receipt(action.action_id, True, {**identity, "observation_seq": 4, "position": 2})

    asyncio.run(run())


def test_plugin_http_requires_game_token(monkeypatch, tmp_path):
    from ella_runtime import api

    bridge = PushGameBridge("minecraft", data_dir=tmp_path)
    monkeypatch.setattr(api, "get_push_bridge", lambda _game_id: bridge)
    events = []
    monkeypatch.setattr(api, "queue_progress", lambda *args: events.append(args))

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api.app), base_url="http://127.0.0.1:8766"
        ) as client:
            path = "/api/game-plugins/minecraft/state"
            bad = await client.post(path, json={"state": {"player": {"health": 20}}})
            assert bad.status_code == 401
            good = await client.post(
                path, json={"state": {"player": {"health": 20}}},
                headers={"Authorization": f"Bearer {bridge.token}"},
            )
            assert good.status_code == 200
            assert (await bridge.observe()).state["player"]["health"] == 20
            event = await client.post(
                "/api/game-plugins/minecraft/events",
                json={"type": "minecraft.player.damaged", "payload": {"health": 10}},
                headers={"Authorization": f"Bearer {bridge.token}"},
            )
            assert event.status_code == 200
            assert events[0][0:2] == ("game", "minecraft")

    asyncio.run(run())


class FakeGame:
    game_id = "minecraft"
    mode = GameMode.API_CONTROL

    def __init__(self):
        self.position = 0
        self.last_action = None

    async def observe(self):
        return GameSnapshot(
            game_id=self.game_id, captured_at=datetime.now(UTC),
            state={"position": self.position, "last_action": self.last_action},
        )

    async def apply_action(self, action):
        self.position += 1
        self.last_action = {"action_id": action.action_id, "status": "verified"}

    def verify_action(self, action, before, after):
        return after.state["position"] == before.state["position"] + 1, {}


class FakeGateway:
    class Settings:
        @staticmethod
        def for_purpose(_purpose):
            return type("Config", (), {"configured": True})()

    settings = Settings()

    def __init__(self):
        self.calls = 0

    async def generate(self, _request):
        self.calls += 1
        return type("Response", (), {"text": (
            '{"name":"move","parameters":{"direction":"forward","ticks":5}}'
            if self.calls == 1 else '{"done":true}'
        )})()


def test_game_play_loop_stops_when_goal_done(tmp_path):
    async def run():
        game = FakeGame()
        gateway = FakeGateway()
        manager = GamePlayManager(gateway)
        controller = GameController(game, GameJournal(tmp_path / "actions.sqlite3"))
        await manager.start("minecraft", "往前走一步", controller, max_steps=3)
        await manager.tasks["minecraft"]
        status = manager.status("minecraft")
        assert status["status"] == "awaiting_confirmation"
        await manager.confirm("minecraft", controller)
        assert manager.status("minecraft")["status"] == "completed"
        assert status["step"] == 1
        assert status["last_result"] == ActionStatus.VERIFIED.value
        assert game.position == 1

    asyncio.run(run())


def test_large_game_tool_catalog_uses_two_decisions():
    class TwoStepGateway(FakeGateway):
        async def generate(self, _request):
            self.calls += 1
            text = (
                '{"name":"bannerlord_party_move_to_point"}'
                if self.calls == 1
                else '{"name":"bannerlord_party_move_to_point","parameters":{"x":4}}'
            )
            return type("Response", (), {"text": text})()

    async def run():
        gateway = TwoStepGateway()
        manager = GamePlayManager(gateway)
        catalog = [{"name": f"bannerlord_tool_{index}", "description": "action"}
                   for index in range(30)]
        catalog.append({
            "name": "bannerlord_party_move_to_point",
            "description": "move", "parameters": {"x": "number"},
        })
        result = await manager._decide(
            GamePlaySession("bannerlord", "移动到指定位置", 5), {"position": 0}, catalog
        )
        assert result["parameters"] == {"x": 4}
        assert gateway.calls == 2

    asyncio.run(run())
