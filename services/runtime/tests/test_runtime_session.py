import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ella_runtime import api
from ella_runtime.__main__ import main
from ella_runtime.modules.games.push_bridge import PushGameBridge
from ella_runtime.runtime_session import RuntimeSession, loopback_host_allowed
from tests.api_client import authorized_client

BASE_URL = "http://127.0.0.1:8766"


def test_session_file_is_private_and_removed_only_by_its_owner(tmp_path):
    path = tmp_path / "runtime.session"
    first = RuntimeSession(path, token="a" * 43)
    first.publish()
    assert path.read_text(encoding="ascii") == first.token
    second = RuntimeSession(path, token="b" * 43)
    second.publish()
    first.close()
    assert path.read_text(encoding="ascii") == second.token
    second.close()
    assert not path.exists()


def test_runtime_lifespan_publishes_and_removes_current_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ELLA_GAME_SETUP_AUTO", "0")
    session = RuntimeSession(tmp_path / "runtime.session")
    monkeypatch.setattr(api, "get_runtime_session", lambda: session)
    with TestClient(api.app, base_url=BASE_URL) as client:
        assert session.path.read_text(encoding="ascii") == session.token
        assert client.get(
            "/api/modules", headers={"Authorization": f"Bearer {session.token}"}
        ).status_code == 200
    assert not session.path.exists()


def test_http_requires_desktop_token_for_read_and_write_routes():
    anonymous = TestClient(api.app, base_url=BASE_URL)
    assert anonymous.get("/health").status_code == 200
    for path in ("/api/models/status", "/api/memory/export", "/api/tasks"):
        assert anonymous.get(path).status_code == 401
    assert anonymous.post("/api/tasks", json={"goal": "创建文件"}).status_code == 401
    assert anonymous.get(
        "/api/models/status", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401

    accepted = authorized_client(api.app)
    assert accepted.get("/api/modules").status_code == 200
    assert accepted.get(
        "/api/modules", headers={"Origin": "https://untrusted.example"}
    ).status_code == 403
    assert accepted.get(
        "/api/modules", headers={"Host": "untrusted.example"}
    ).status_code == 400
    # Plugin routes retain their independent bridge credential instead of desktop auth.
    assert anonymous.get("/api/game-plugins/unknown/commands/next").status_code == 404


def test_request_body_limit_applies_with_or_without_content_length():
    client = authorized_client(api.app)
    oversized = 12 * 1024 * 1024 + 1
    assert client.post(
        "/api/tasks", content=b"x", headers={"Content-Length": str(oversized)}
    ).status_code == 413
    assert client.post(
        "/api/tasks", content=(b"x" * 1024 * 1024 for _ in range(13))
    ).status_code == 413


def test_game_plugin_rejects_desktop_token_but_accepts_its_own(tmp_path, monkeypatch):
    bridge = PushGameBridge("minecraft", data_dir=tmp_path)
    monkeypatch.setattr(api, "get_push_bridge", lambda _game_id: bridge)
    path = "/api/game-plugins/minecraft/commands/next"
    assert authorized_client(api.app).get(path).status_code == 401
    assert TestClient(api.app, base_url=BASE_URL).get(
        path, headers={"Authorization": f"Bearer {bridge.token}"}
    ).status_code == 409
    asyncio.run(bridge.publish({"client_id": "client", "session_id": "session", "save_id": "save", "observation_seq": 1, "full_snapshot": True, "player": {"health": 20}}))
    assert TestClient(api.app, base_url=BASE_URL).get(path, params={"client_id": "client", "session_id": "session"}, headers={"Authorization": f"Bearer {bridge.token}"}).status_code == 200


def test_websocket_rejects_missing_or_wrong_session_protocol():
    client = TestClient(api.app, base_url=BASE_URL)
    for protocols in ([], ["ella-auth", "wrong"]):
        with (
            pytest.raises(WebSocketDisconnect) as failure,
            client.websocket_connect(
                "/api/voice/stream",
                headers={"Origin": "http://127.0.0.1:1421", "Host": "127.0.0.1:8766"},
                subprotocols=protocols,
            ),
        ):
            pass
        assert failure.value.code == 1008


def test_runtime_refuses_non_loopback_bind(monkeypatch):
    monkeypatch.setenv("ELLA_RUNTIME_HOST", "0.0.0.0")
    with pytest.raises(ValueError, match="回环"):
        main()
    assert loopback_host_allowed("localhost:8766", port=8766)
    assert not loopback_host_allowed("attacker.example:8766", port=8766)
    assert not loopback_host_allowed("127.0.0.1:9999", port=8766)
