import asyncio
from datetime import UTC, datetime

import pytest

from ella_runtime.modules.games.bridge import GameBridgeError
from ella_runtime.modules.games.contracts import GameAction, GameSnapshot
from ella_runtime.modules.games.journal import GameJournal
from ella_runtime.modules.games.launch import GameLaunchCoordinator, game_executable
from ella_runtime.modules.games.play import (
    GamePlayError,
    GamePlayManager,
    GamePlaySession,
    completion_evidence,
)
from ella_runtime.modules.games.push_bridge import PushGameBridge


def state(client="one", session="session", seq=1):
    return {"client_id": client, "session_id": session, "save_id": "world", "observation_seq": seq, "full_snapshot": True, "player": {"health": 20}}


def test_another_client_cannot_steal_commands_and_old_sequence_is_rejected(tmp_path):
    async def run():
        bridge = PushGameBridge("minecraft", data_dir=tmp_path)
        await bridge.publish(state())
        with pytest.raises(GameBridgeError):
            await bridge.publish(state("two"))
        with pytest.raises(GameBridgeError):
            await bridge.publish(state(seq=1))
        with pytest.raises(GameBridgeError):
            await bridge.next_command("two", "session")
        await bridge.publish(state(session="new", seq=1))
        with pytest.raises(GameBridgeError):
            await bridge.publish(state(seq=2))
        assert (await bridge.observe()).state["session_id"] == "new"
    asyncio.run(run())


def test_old_session_receipt_cannot_settle_new_action(tmp_path):
    async def run():
        bridge = PushGameBridge("minecraft", data_dir=tmp_path)
        await bridge.publish(state())
        snapshot = await bridge.observe()
        action = bridge.bind_action(GameAction(action_id="id", name="wait", parameters={"ticks": 1}), snapshot)
        task = asyncio.create_task(bridge.apply_action(action))
        await asyncio.sleep(0)
        with pytest.raises(GameBridgeError):
            await bridge.receipt("id", True, state("two", seq=2))
        await bridge.receipt("id", True, state(seq=2))
        await task
    asyncio.run(run())


def test_restart_restores_paused_without_executing(tmp_path):
    journal = GameJournal(tmp_path / "game.sqlite3")
    journal.save_session(GamePlaySession("minecraft", "goal", 10, binding={"save_id": "world"}, pending_action_id="unknown").public())
    manager = GamePlayManager(None, journal=journal)
    restored = manager.status("minecraft")
    assert restored["status"] == "paused" and restored["recovery_pending"]
    assert restored["pending_action_id"] == "unknown" and not manager.tasks


def test_resume_cannot_switch_save(tmp_path):
    journal = GameJournal(tmp_path / "game.sqlite3")
    manager = GamePlayManager(None, journal=journal)
    manager.sessions["minecraft"] = GamePlaySession("minecraft", "goal", 10, status="paused", binding={"client_id": "one", "session_id": "s", "save_id": "old"})
    class Controller:
        async def observe(self):
            return GameSnapshot(game_id="minecraft", captured_at=datetime.now(UTC), state={"client_id": "one", "session_id": "s", "save_id": "new"})
    with pytest.raises(GamePlayError):
        asyncio.run(manager.resume("minecraft", Controller()))
    assert manager.status("minecraft")["status"] == "paused"


def test_completion_requires_real_condition():
    condition = {"path": ["player", "money"], "op": "gte", "value": 500}
    assert not completion_evidence({"player": {"money": 100}}, [condition])["verified"]
    assert completion_evidence({"player": {"money": 600}}, [condition])["verified"]
    assert not completion_evidence({}, [condition])["verified"]


def test_launch_waits_for_save_and_cancel_does_not_start():
    async def run():
        events = []
        class Controller:
            ready = False
            async def observe(self):
                return GameSnapshot(game_id="minecraft", captured_at=datetime.now(UTC), state={"bridge_ready": self.ready})
        controller = Controller()
        coordinator = GameLaunchCoordinator(timeout=0.3, interval=0.01)
        async def launch(): events.append("launch")
        async def start(): events.append("start")
        await coordinator.begin("minecraft", "goal", controller, launch, start)
        await asyncio.sleep(0.03)
        assert not events and coordinator.status("minecraft")["status"] == "waiting_for_save"
        await coordinator.cancel("minecraft")
        controller.ready = True
        await coordinator.begin("minecraft", "goal", controller, launch, start)
        await coordinator.tasks["minecraft"]
        assert events == ["start"]
    asyncio.run(run())


def test_game_install_discovery_supports_custom_library(tmp_path):
    executable = tmp_path / "steamapps/common/Stardew Valley/StardewModdingAPI.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture")
    assert game_executable("stardew_valley", [tmp_path]) == executable.resolve()


def test_plugin_config_updates_endpoint_and_preserves_token(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("ELLA_RUNTIME_PORT", "18766")
    first = PushGameBridge("minecraft", data_dir=tmp_path)
    monkeypatch.setenv("ELLA_RUNTIME_PORT", "28766")
    second = PushGameBridge("minecraft", data_dir=tmp_path)
    config = json.loads((tmp_path / "game-bridge/minecraft.json").read_text())
    assert second.token == first.token == config["token"]
    assert config["base_url"] == "http://127.0.0.1:28766"
