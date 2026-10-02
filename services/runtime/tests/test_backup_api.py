"""Authenticated backup routes and lifespan ordering, using only isolated data."""

import asyncio
import base64
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ella_runtime import api
from ella_runtime.modules.agent.contracts import TaskState
from ella_runtime.modules.backup import MAX_ARCHIVE_BYTES, BackupError, BackupService
from ella_runtime.modules.memory.contracts import MemoryCreate, MemoryKind
from ella_runtime.modules.memory.store import MemoryStore
from ella_runtime.runtime_session import RuntimeSession


class Cached:
    def __init__(self, value, *, cached=True, name="", events=None):
        self.value = value
        self.cached = cached
        self.calls = 0
        self.name = name
        self.events = events

    def __call__(self):
        self.cached = True
        self.calls += 1
        return self.value

    def cache_info(self):
        return SimpleNamespace(currsize=int(self.cached))

    def cache_clear(self):
        self.cached = False
        if self.events is not None:
            self.events.append("clear:" + self.name)


@pytest.fixture
def backup_api(tmp_path, monkeypatch):
    source = BackupService(tmp_path / "source")
    MemoryStore(source.data_dir / "memory.sqlite3").create(
        MemoryCreate(kind=MemoryKind.FACT, content="备份中的隔离记忆"),
    )
    archive = source.export_archive().read_bytes()
    target = BackupService(tmp_path / "target")
    memory = MemoryStore(target.data_dir / "memory.sqlite3")
    original = memory.create(MemoryCreate(kind=MemoryKind.FACT, content="现场隔离记忆"))
    monkeypatch.setenv("ELLA_DATA_DIR", str(target.data_dir))
    monkeypatch.setenv("ELLA_GAME_SETUP_AUTO", "0")
    session = RuntimeSession(tmp_path / "test.session", token="backup-api-test-token")
    monkeypatch.setattr(api, "get_runtime_session", Cached(session))
    monkeypatch.setattr(api, "get_backup_service", Cached(target))
    voice = SimpleNamespace(state=SimpleNamespace(value="idle"))
    monkeypatch.setattr(api, "get_voice_session", Cached(voice))
    tasks = []
    monkeypatch.setattr(api, "get_agent_engine", Cached(SimpleNamespace(
        store=SimpleNamespace(has_running=lambda: any(task.state == TaskState.RUNNING for task in tasks)),
    )))
    playing = {game_id: "idle" for game_id in api.GAME_NAMES}
    monkeypatch.setattr(api, "get_game_play_manager", Cached(SimpleNamespace(
        status=lambda game_id: {"status": playing[game_id]},
    )))
    monkeypatch.setattr(api, "_active_voice_sockets", 0)
    tts = SimpleNamespace(busy=False)
    monkeypatch.setattr(api, "_notification_lock", SimpleNamespace(locked=lambda: tts.busy))

    def forbidden_apply():
        raise AssertionError("HTTP routes must never install databases directly")

    monkeypatch.setattr(target, "apply_pending_before_start", forbidden_apply)
    client = TestClient(api.app, base_url="http://127.0.0.1:8766",
                        headers={"Authorization": f"Bearer {session.token}"})
    try:
        yield SimpleNamespace(client=client, service=target, archive=archive,
                              encoded=base64.b64encode(archive).decode(), memory=memory,
                              original=original, tasks=tasks, voice=voice, tts=tts, playing=playing)
    finally:
        client.close()
        target.close()
        source.close()


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/api/backup/status", None),
    ("GET", "/api/backup/export", None),
    ("POST", "/api/backup/preview", {"archive_base64": "eA=="}),
    ("POST", "/api/backup/confirm", {"id": "f" * 32}),
    ("DELETE", "/api/backup/staged/" + "f" * 32, None),
])
def test_all_backup_routes_require_the_desktop_bearer(backup_api, method, path, payload):
    response = backup_api.client.request(method, path, json=payload,
                                         headers={"Authorization": ""})
    assert response.status_code == 401
    assert list((backup_api.service.root / "staged").iterdir()) == []
    assert backup_api.memory.get(backup_api.original.id).content == "现场隔离记忆"


def test_authenticated_export_preview_confirm_and_cancel_never_install_database(backup_api):
    client = backup_api.client
    export = client.get("/api/backup/export")
    assert export.status_code == 200 and export.headers["content-type"] == "application/zip"
    assert ".zip" in export.headers["content-disposition"]
    preview = client.post("/api/backup/preview", json={"archive_base64": backup_api.encoded})
    assert preview.status_code == 200 and len(preview.json()["files"]) == 9
    identity = preview.json()["id"]
    assert backup_api.memory.get(backup_api.original.id) is not None
    confirmed = client.post("/api/backup/confirm", json={"id": identity})
    assert confirmed.status_code == 200 and confirmed.json()["restart_required"] is True
    assert client.get("/api/backup/status").json()["pending"]["id"] == identity
    assert backup_api.memory.get(backup_api.original.id) is not None
    assert client.delete(f"/api/backup/staged/{identity}").status_code == 204
    assert client.get("/api/backup/status").json()["pending"] is None
    assert backup_api.memory.get(backup_api.original.id) is not None


@pytest.mark.parametrize("busy", ["voice_socket", "tts", "voice_thinking", "task", "game"])
def test_busy_backup_operations_are_rejected_but_status_and_cancel_remain_available(backup_api, monkeypatch, busy):
    client = backup_api.client
    preview = client.post("/api/backup/preview", json={"archive_base64": backup_api.encoded}).json()
    if busy == "voice_socket": monkeypatch.setattr(api, "_active_voice_sockets", 1)
    elif busy == "tts": backup_api.tts.busy = True
    elif busy == "voice_thinking": backup_api.voice.state.value = "thinking"
    elif busy == "task": backup_api.tasks.append(SimpleNamespace(state=TaskState.RUNNING))
    elif busy == "game": backup_api.playing[next(iter(api.GAME_NAMES))] = "running"
    assert client.get("/api/backup/export").status_code == 409
    assert client.post("/api/backup/preview", json={"archive_base64": backup_api.encoded}).status_code == 409
    assert client.post("/api/backup/confirm", json={"id": preview["id"]}).status_code == 409
    assert client.get("/api/backup/status").status_code == 200
    assert client.delete(f'/api/backup/staged/{preview["id"]}').status_code == 204
    assert backup_api.service.status()["pending"] is None


@pytest.mark.parametrize("encoded,status", [("!!!!", 400), (" eA==", 400), ("", 422), ("eA==", 400)])
def test_preview_rejects_invalid_base64_empty_payload_and_nonzip(backup_api, encoded, status):
    response = backup_api.client.post("/api/backup/preview", json={"archive_base64": encoded})
    assert response.status_code == status
    assert list((backup_api.service.root / "staged").iterdir()) == []


def test_preview_rejects_decoded_archive_above_eight_mib(backup_api):
    encoded = base64.b64encode(b"x" * (MAX_ARCHIVE_BYTES + 1)).decode()
    response = backup_api.client.post("/api/backup/preview", json={"archive_base64": encoded})
    assert response.status_code == 413
    assert list((backup_api.service.root / "staged").iterdir()) == []


def test_invalid_restore_identity_and_service_errors_use_structured_http_errors(backup_api, monkeypatch):
    client = backup_api.client
    assert client.post("/api/backup/confirm", json={"id": "invalid"}).status_code == 422
    assert client.post("/api/backup/confirm", json={"id": "f" * 32}).status_code == 400
    assert client.delete("/api/backup/staged/not-an-id").status_code == 400

    def unavailable():
        raise BackupError("隔离备份不可用")

    monkeypatch.setattr(backup_api.service, "export_archive", unavailable)
    response = client.get("/api/backup/export")
    assert response.status_code == 400 and response.json()["detail"] == "隔离备份不可用"


class Resource:
    def __init__(self, name, events, *, fail=False):
        self.name, self.events, self.fail = name, events, fail

    async def aclose(self):
        self.events.append("close:" + self.name)
        if self.fail: raise RuntimeError("isolated shutdown error")

    async def close(self):
        await self.aclose()

    def start_auto(self):
        self.events.append("start:games")


def lifespan_fakes(monkeypatch, tmp_path, *, fail_start=False, fail_shutdown=False):
    events = []
    monkeypatch.setenv("ELLA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ELLA_GAME_SETUP_AUTO", "1")

    class Backup:
        def apply_pending_before_start(self):
            events.append("apply:backup")
            if fail_start: raise BackupError("isolated startup failure")

        def close(self):
            events.append("close:lease")

    class Session:
        def publish(self): events.append("publish:session")
        def close(self): events.append("close:session")

    backup = Cached(Backup(), name="backup", events=events)
    session = Cached(Session(), cached=False)
    monkeypatch.setattr(api, "get_backup_service", backup)
    monkeypatch.setattr(api, "get_runtime_session", session)
    for getter_name, label in (
        ("get_game_setup_service", "setup"), ("get_dialogue_actions", "dialogue"),
        ("get_game_launch_coordinator", "launcher"), ("get_game_play_manager", "games"),
        ("get_conversation_service", "conversation"), ("get_browser_session", "browser"),
        ("get_memory_retriever", "memory"),
    ):
        monkeypatch.setattr(api, getter_name, Cached(
            Resource(label, events, fail=fail_shutdown and label == "setup"),
            name=label, events=events,
        ))
    monkeypatch.setattr(api, "_game_controller_instances", [])
    return events, backup, session


@pytest.mark.parametrize("fail_shutdown", [False, True])
def test_lifespan_applies_before_session_publish_and_releases_lease_after_all_resources(monkeypatch, tmp_path, fail_shutdown):
    events, backup, _ = lifespan_fakes(monkeypatch, tmp_path, fail_shutdown=fail_shutdown)

    async def run():
        async with api.runtime_lifespan(api.app):
            assert events[:3] == ["apply:backup", "publish:session", "start:games"]
            events.append("inside:runtime")

    asyncio.run(run())
    assert events[-3:] == ["close:session", "close:lease", "clear:backup"]
    for name in ("setup", "dialogue", "launcher", "games", "conversation", "browser", "memory"):
        assert events.index("close:" + name) < events.index("close:lease")
        assert "clear:" + name in events
    assert backup.cached is False


def test_startup_failure_never_publishes_session_and_releases_backup_lease(monkeypatch, tmp_path):
    events, backup, session = lifespan_fakes(monkeypatch, tmp_path, fail_start=True)

    async def run():
        async with api.runtime_lifespan(api.app):
            raise AssertionError("failed restore must not start the runtime")

    with pytest.raises(BackupError): asyncio.run(run())
    assert "publish:session" not in events and session.calls == 0
    assert events[-2:] == ["close:lease", "clear:backup"]
    assert backup.cached is False
