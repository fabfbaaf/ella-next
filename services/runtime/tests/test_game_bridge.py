import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from ella_runtime.modules.games.bridge import LocalGameBridge, bridge_url
from ella_runtime.modules.games.contracts import ActionStatus, GameAction
from ella_runtime.modules.games.controller import GameController
from ella_runtime.modules.games.journal import GameJournal


def test_local_game_bridge_observes_applies_and_verifies(tmp_path):
    state = {"position": 0, "last_action": None}

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/snapshot":
            return httpx.Response(200, json={
                "game_id": "minecraft", "captured_at": datetime.now(UTC).isoformat(),
                "state": state,
            })
        action = json.loads(request.content)
        state["position"] += action["parameters"]["steps"]
        state["last_action"] = {"action_id": action["action_id"], "status": "verified"}
        return httpx.Response(200, json={"action_id": action["action_id"], "accepted": True})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            controller = GameController(
                LocalGameBridge("minecraft", "http://127.0.0.1:9901", client),
                GameJournal(tmp_path / "game.sqlite3"),
            )
            result = await controller.perform(GameAction(
                action_id="move-bridge-1", name="move", parameters={"steps": 2}
            ))
            assert result.status == ActionStatus.VERIFIED
            assert result.after.state["position"] == 2

    asyncio.run(run())


def test_game_bridge_rejects_remote_address(monkeypatch):
    with pytest.raises(ValueError, match="本机"):
        LocalGameBridge("minecraft", "https://remote.example")
    monkeypatch.setenv("ELLA_GAME_MINECRAFT_BRIDGE_URL", "https://remote.example")
    with pytest.raises(RuntimeError, match="本机"):
        bridge_url("minecraft")
