import io
import json
import sqlite3
import zipfile
from contextlib import closing
from datetime import UTC, datetime

import pytest

from ella_runtime.modules.agent.contracts import AgentTask, StepState, TaskState, TaskStep
from ella_runtime.modules.agent.store import TaskStore
from ella_runtime.modules.backup import DATABASES, BackupError, BackupService
from ella_runtime.modules.companion.dialogue import DialogueActions
from ella_runtime.modules.companion.store import CompanionStore
from ella_runtime.modules.memory.contracts import MemoryCreate, MemoryKind
from ella_runtime.modules.memory.store import MemoryStore
from ella_runtime.modules.models.config_store import ModelConfigInput, ModelConfigStore
from ella_runtime.modules.models.conversations import ConversationStore
from ella_runtime.modules.models.usage_store import UsageStore
from ella_runtime.modules.voice.config_store import VoiceConfigInput, VoiceConfigStore
from ella_runtime.modules.voice.context import VoiceContext


class Vault:
    def __init__(self):
        self.values = {}
        self.block_reads = False

    def get(self, identity):
        assert not self.block_reads, "restored settings must not read machine credentials"
        return self.values.get(identity)

    def set(self, identity, value):
        self.values[identity] = value

    def delete(self, identity):
        self.values.pop(identity, None)


def seed(directory, *, content="原始记忆"):
    directory.mkdir(parents=True, exist_ok=True)
    memory = MemoryStore(directory / "memory.sqlite3")
    memory.create(MemoryCreate(kind=MemoryKind.FACT, content=content))
    conversation = ConversationStore(directory / "conversations.sqlite3")
    identity = conversation.create()
    conversation.append_pair(identity, "聊天内容", "收到")
    VoiceContext(conversation).set_context_source(identity)
    companion = CompanionStore(directory / "companion.sqlite3")
    DialogueActions(None, companion, None, path=directory / "dialogue-actions.sqlite3")
    with closing(sqlite3.connect(directory / "dialogue-actions.sqlite3")) as connection, connection:
        connection.execute("INSERT INTO dialogue_bindings VALUES(?,?,?,?,?)", (identity, "ready", "hash", 1, "允许执行"))
        connection.execute("INSERT INTO dialogue_receipts VALUES(?,?,?)", ("receipt", "原回执", "ready"))
        connection.execute("INSERT INTO dialogue_handoffs VALUES(?,?,?)", ("voice-main", identity, "ready"))
    UsageStore(directory / "usage.sqlite3")
    tasks = TaskStore(directory / "tasks.sqlite3")
    for task_id, state, step_state in (("ready", TaskState.READY, StepState.PENDING),
                                      ("running", TaskState.RUNNING, StepState.RUNNING),
                                      ("complete", TaskState.COMPLETE, StepState.COMPLETE)):
        task = AgentTask(id=task_id, goal="检查文档", created_at=datetime.now(UTC),
                         updated_at=datetime.now(UTC), state=state,
                         steps=[TaskStep(id="step", tool="browser.read_page", state=step_state)])
        task.approved_plan_hash = task.plan_hash
        tasks.save(task)
    vault = Vault()
    models = ModelConfigStore(directory / "models.sqlite3", vault)
    models.save("chat", ModelConfigInput(provider="openai", model="test", base_url="https://example.com/v1", api_key="vault-secret"))
    voice = VoiceConfigStore(directory / "voice.sqlite3", vault)
    voice.save("asr", VoiceConfigInput(base_url="https://example.com/v1", model="test", api_key="voice-secret"))
    return memory, vault


def mutate_zip(raw, mutate):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    mutate(files)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, contents in files.items():
            archive.writestr(name, contents)
    return output.getvalue()


def test_export_is_a_verified_sqlite_snapshot_without_credentials_or_other_files(tmp_path):
    data = tmp_path / "source"
    _, vault = seed(data)
    (data / "browser-profile").mkdir()
    (data / "browser-profile" / "cookies.json").write_text("private cookie")
    (data / "recording.wav").write_bytes(b"audio")
    (data / "game-save.dat").write_bytes(b"game")
    service = BackupService(data)
    archive_path = service.export_archive()
    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {*DATABASES, "manifest.json"}
        manifest = json.loads(archive.read("manifest.json"))
        assert set(manifest["files"]) == set(DATABASES)
        assert b"vault-secret" not in archive.read("models.sqlite3")
        assert b"voice-secret" not in archive.read("voice.sqlite3")
    preview = service.stage_restore(archive_path.read_bytes())
    assert len(preview["files"]) == 9
    assert next(item for item in preview["files"] if item["name"] == "memory.sqlite3")["rows"]["memory_items"] == 1
    # Export does not remove bindings from the live source.
    assert ModelConfigStore(data / "models.sqlite3", vault).public_status()["chat"]["key_saved"] is True


def test_restore_preview_and_confirmation_do_not_replace_running_databases(tmp_path):
    source = BackupService(tmp_path / "source")
    MemoryStore(source.data_dir / "memory.sqlite3").create(MemoryCreate(kind=MemoryKind.FACT, content="备份内容"))
    target = tmp_path / "target"
    memory = MemoryStore(target / "memory.sqlite3")
    old = memory.create(MemoryCreate(kind=MemoryKind.FACT, content="当前内容"))
    service = BackupService(target)
    assert service.apply_pending_before_start() is None
    preview = service.stage_restore(source.export_archive().read_bytes())
    service.confirm_restore(preview["id"])
    assert memory.get(old.id).content == "当前内容"
    with pytest.raises(BackupError, match="不能在运行中"):
        service.apply_pending_before_start()
    assert memory.get(old.id).content == "当前内容"
    service.cancel_restore(preview["id"])
    assert service.status()["pending"] is None
    service.close()


def test_restore_at_start_clears_task_approval_dialogue_authorization_and_key_bindings(tmp_path):
    source = tmp_path / "source"
    seed(source, content="新机器需要的记忆")
    archive = BackupService(source).export_archive().read_bytes()
    target = tmp_path / "target"
    old, vault = seed(target, content="接收机原数据")
    old_identity = old.list()[0].id
    service = BackupService(target)
    preview = service.stage_restore(archive)
    service.confirm_restore(preview["id"])
    result = BackupService(target).apply_pending_before_start()
    assert result["status"] == "restored"
    assert (target / "backups" / "exports" / result["rollback_archive"]).is_file()
    memory = MemoryStore(target / "memory.sqlite3")
    assert memory.get(old_identity) is None and memory.list()[0].content == "新机器需要的记忆"
    tasks = TaskStore(target / "tasks.sqlite3")
    assert tasks.get("ready").state == TaskState.WAITING_APPROVAL
    assert tasks.get("running").state == TaskState.NEEDS_RECONCILIATION
    assert tasks.get("running").steps[0].state == StepState.NEEDS_RECONCILIATION
    assert tasks.get("complete").state == TaskState.COMPLETE
    assert all(task.approved_plan_hash is None for task in tasks.list())
    with closing(sqlite3.connect(target / "dialogue-actions.sqlite3")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM dialogue_bindings").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM dialogue_receipts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM dialogue_handoffs").fetchone()[0] == 0
    vault.block_reads = True
    assert ModelConfigStore(target / "models.sqlite3", vault).public_status()["chat"]["key_saved"] is False
    assert VoiceConfigStore(target / "voice.sqlite3", vault).public_status()["asr"]["key_saved"] is False
    assert BackupService(target).status()["pending"] is None


@pytest.mark.parametrize("change", ["unexpected", "traversal", "hash", "missing", "corrupt", "duplicate"])
def test_import_rejects_nonwhitelist_files_hash_mismatch_and_corruption(tmp_path, change):
    raw = BackupService(tmp_path / "source").export_archive().read_bytes()

    def mutate(files):
        if change == "unexpected": files["browser-cookies.sqlite3"] = b"private"
        elif change == "traversal": files["../memory.sqlite3"] = files.pop("memory.sqlite3")
        elif change == "hash": files["memory.sqlite3"] += b"changed"
        elif change == "missing": files.pop("voice-context.sqlite3")
        elif change == "corrupt":
            files["memory.sqlite3"] = b"not sqlite"
            manifest = json.loads(files["manifest.json"])
            import hashlib
            manifest["files"]["memory.sqlite3"] = {"size": 10, "sha256": hashlib.sha256(b"not sqlite").hexdigest()}
            files["manifest.json"] = json.dumps(manifest).encode()
    broken = mutate_zip(raw, mutate)
    if change == "duplicate":
        out = io.BytesIO(broken)
        with zipfile.ZipFile(out, "a") as archive, pytest.warns(UserWarning):
            archive.writestr("memory.sqlite3", b"duplicate")
        broken = out.getvalue()
    target = BackupService(tmp_path / "target")
    with pytest.raises(BackupError): target.stage_restore(broken)
    assert not list((target.root / "staged").iterdir())
    assert not (tmp_path / "memory.sqlite3").exists()


def test_manifest_tampering_after_confirmation_never_installs_database(tmp_path):
    source = BackupService(tmp_path / "source")
    target = BackupService(tmp_path / "target")
    memory = MemoryStore(target.data_dir / "memory.sqlite3")
    old = memory.create(MemoryCreate(kind=MemoryKind.FACT, content="保留当前"))
    preview = target.stage_restore(source.export_archive().read_bytes())
    target.confirm_restore(preview["id"])
    manifest = target.root / "staged" / preview["id"] / "manifest.json"
    manifest.write_text(manifest.read_text() + " ")
    result = BackupService(target.data_dir).apply_pending_before_start()
    assert result["status"] == "failed"
    assert memory.get(old.id).content == "保留当前"


def test_mid_install_failure_rolls_back_current_data_and_absent_databases(tmp_path, monkeypatch):
    source = BackupService(tmp_path / "source")
    MemoryStore(source.data_dir / "memory.sqlite3").create(MemoryCreate(kind=MemoryKind.FACT, content="恢复内容"))
    target = BackupService(tmp_path / "target")
    old = MemoryStore(target.data_dir / "memory.sqlite3").create(MemoryCreate(kind=MemoryKind.FACT, content="恢复前"))
    preview = target.stage_restore(source.export_archive().read_bytes())
    target.confirm_restore(preview["id"])
    startup = BackupService(target.data_dir)
    original = startup._install_database
    calls = 0

    def failing_once(snapshot, name):
        nonlocal calls
        calls += 1
        if calls == 2: raise OSError("simulated disk failure")
        original(snapshot, name)

    monkeypatch.setattr(startup, "_install_database", failing_once)
    assert startup.apply_pending_before_start()["status"] == "failed"
    assert MemoryStore(target.data_dir / "memory.sqlite3").get(old.id).content == "恢复前"
    assert not (target.data_dir / "models.sqlite3").exists()
    assert not (target.root / "restore-journal.json").exists()


def test_interrupted_install_journal_is_rolled_back_on_next_start(tmp_path, monkeypatch):
    source = BackupService(tmp_path / "source")
    MemoryStore(source.data_dir / "memory.sqlite3").create(MemoryCreate(kind=MemoryKind.FACT, content="恢复内容"))
    target = BackupService(tmp_path / "target")
    old = MemoryStore(target.data_dir / "memory.sqlite3").create(MemoryCreate(kind=MemoryKind.FACT, content="恢复前"))
    preview = target.stage_restore(source.export_archive().read_bytes())
    target.confirm_restore(preview["id"])
    startup = BackupService(target.data_dir)
    original = startup._install_database
    calls = 0

    def interrupted(snapshot, name):
        nonlocal calls
        calls += 1
        if calls == 3: raise KeyboardInterrupt("simulate interrupted process")
        original(snapshot, name)

    monkeypatch.setattr(startup, "_install_database", interrupted)
    with pytest.raises(KeyboardInterrupt): startup.apply_pending_before_start()
    assert (target.root / "restore-journal.json").exists()
    startup.close()  # Simulated process exit releases its lifetime data lease.
    result = BackupService(target.data_dir).apply_pending_before_start()
    assert result["status"] == "rolled_back"
    assert MemoryStore(target.data_dir / "memory.sqlite3").get(old.id).content == "恢复前"
    assert target.status()["pending"] is None


def test_empty_source_has_valid_empty_databases_and_invalid_identifier_is_rejected(tmp_path):
    source = BackupService(tmp_path / "source")
    target = BackupService(tmp_path / "target")
    preview = target.stage_restore(source.export_archive().read_bytes())
    assert len(preview["files"]) == 9 and all(item["rows"] == {} for item in preview["files"])
    with pytest.raises(BackupError): target.confirm_restore("../../elsewhere")
    with pytest.raises(BackupError): target.cancel_restore("../elsewhere")
    target.cancel_restore(preview["id"])


def test_export_uses_sqlite_backup_to_include_live_wal_pages(tmp_path):
    service = BackupService(tmp_path / "data")
    store = MemoryStore(service.data_dir / "memory.sqlite3")
    record = store.create(MemoryCreate(kind=MemoryKind.FACT, content="旧内容"))
    with closing(sqlite3.connect(store.path)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("UPDATE memory_items SET content=? WHERE id=?", ("仍在 WAL 中的内容", record.id))
        connection.commit()
        assert store.path.with_name("memory.sqlite3-wal").is_file()
        preview = service.stage_restore(service.export_archive().read_bytes())
        snapshot = service.root / "staged" / preview["id"] / "memory.sqlite3"
        assert MemoryStore(snapshot).get(record.id).content == "仍在 WAL 中的内容"


def test_stage_rejects_archive_over_size_limit_before_expanding(tmp_path):
    service = BackupService(tmp_path / "data")
    with pytest.raises(BackupError, match="8 MiB"):
        service.stage_restore(b"x" * (8 * 1024 * 1024 + 1))
    assert list((service.root / "staged").iterdir()) == []


def test_running_service_lease_blocks_a_second_process_from_restoring(tmp_path):
    source = BackupService(tmp_path / "source")
    target = BackupService(tmp_path / "target")
    target.apply_pending_before_start()
    preview = target.stage_restore(source.export_archive().read_bytes())
    target.confirm_restore(preview["id"])
    other = BackupService(target.data_dir)
    with pytest.raises(BackupError, match="仍在使用数据"):
        other.apply_pending_before_start()
    assert target.status()["pending"]["id"] == preview["id"]
    target.close()
    assert other.apply_pending_before_start()["status"] == "restored"
    other.close()


def test_older_listening_runtime_blocks_restore_without_exposing_session_token(tmp_path, monkeypatch):
    from ella_runtime.modules import backup
    source = BackupService(tmp_path / "source")
    target = BackupService(tmp_path / "target")
    preview = target.stage_restore(source.export_archive().read_bytes())
    target.confirm_restore(preview["id"])
    (target.data_dir / "runtime.session").write_text("private-runtime-token")

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): return None

    monkeypatch.setattr(backup.socket, "create_connection", lambda *args, **kwargs: Connection())
    with pytest.raises(BackupError, match="仍在监听"):
        target.apply_pending_before_start()
    assert (target.data_dir / "runtime.session").read_text() == "private-runtime-token"
    assert target.status()["pending"]["id"] == preview["id"]
    assert target._lease is None
