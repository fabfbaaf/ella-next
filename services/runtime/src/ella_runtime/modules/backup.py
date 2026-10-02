"""Bounded SQLite backups; restore is staged and applied only before startup."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import tempfile
import threading
import time
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ella_runtime.modules.agent.contracts import AgentTask, StepState, TaskState
from ella_runtime.modules.models.config_store import ModelConfigInput
from ella_runtime.modules.voice.config_store import VoiceConfigInput
from ella_runtime.storage_paths import default_data_dir

MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_DATABASE_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
DATABASES = {
    "memory.sqlite3": {
        "memory_items": "id kind content source_type source_ref confidence tags_json created_at updated_at topic preference_value preference_negative active superseded_by inactive_reason",
        "memory_revisions": "id memory_id old_content old_source_type old_source_ref new_content reason changed_at",
        "memory_vectors": "memory_id model vector_json updated_at",
        "memory_fts": "memory_id content tags",
        "memory_fts_data": "id block", "memory_fts_idx": "segid term pgno",
        "memory_fts_content": "id c0 c1 c2", "memory_fts_docsize": "id sz",
        "memory_fts_config": "k v", "sqlite_sequence": "name seq",
    },
    "conversations.sqlite3": {
        "conversations": "id created_at updated_at archived_at kind",
        "messages": "id conversation_id role content created_at",
        "conversation_summary_windows": "conversation_id end_message_count processed_at",
        "sqlite_sequence": "name seq",
    },
    "tasks.sqlite3": {"agent_tasks": "id updated_at state payload"},
    "companion.sqlite3": {
        "companion_state": "id hunger_base hunger_updated_at last_interaction_at last_question_at quiet_start quiet_end",
        "reminders": "id title due_at created_at delivered_at",
        "activity_events": "id source_type source_id stage text created_at delivered_at",
        "companion_notifications": "id source_type text created_at delivered_at read_at spoken_at speech_error",
    },
    "usage.sqlite3": {
        "token_usage": "id occurred_at provider model purpose task_id input_tokens output_tokens cache_read_tokens",
        "sqlite_sequence": "name seq",
    },
    "models.sqlite3": {"model_configs": "slot provider model base_url key_origin"},
    "voice.sqlite3": {"voice_configs": "slot provider base_url model voice key_origin"},
    "dialogue-actions.sqlite3": {
        "dialogue_bindings": "conversation task_id shown_hash full_preview preview_text",
        "dialogue_receipts": "identity reply task_id",
        "dialogue_handoffs": "voice_conversation source_conversation task_id",
    },
    "voice-context.sqlite3": {"voice_context": "slot source_chat_id"},
}
OPTIONAL_COLUMNS = {
    "memory_items": "topic preference_value preference_negative active superseded_by inactive_reason",
    "conversations": "archived_at kind",
    "model_configs": "key_origin", "voice_configs": "key_origin",
    "dialogue_bindings": "preview_text", "dialogue_receipts": "task_id",
    "companion_notifications": "spoken_at speech_error",
}
KNOWN_INDEXES = {"memory_items_updated", "messages_conversation", "token_usage_date"}
KNOWN_TRIGGERS = {
    "memory_fts_insert": "CREATE TRIGGER memory_fts_insert AFTER INSERT ON memory_items BEGIN INSERT INTO memory_fts(memory_id, content, tags) VALUES (new.id, new.content, new.tags_json); END;",
    "memory_fts_update": "CREATE TRIGGER memory_fts_update AFTER UPDATE ON memory_items BEGIN DELETE FROM memory_fts WHERE memory_id = old.id; INSERT INTO memory_fts(memory_id, content, tags) VALUES (new.id, new.content, new.tags_json); END;",
    "memory_fts_delete": "CREATE TRIGGER memory_fts_delete AFTER DELETE ON memory_items BEGIN DELETE FROM memory_fts WHERE memory_id = old.id; END;",
}


class BackupError(ValueError):
    """The backup cannot be exported, validated, or restored safely."""


def _hash(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _sql_normalized(value: str) -> str:
    return re.sub(r"\s+", "", value.replace("IF NOT EXISTS", "")).casefold().rstrip(";")


def _connect(path: Path, *, readonly: bool = False):
    connection = sqlite3.connect(path.as_uri() + "?mode=ro" if readonly else path, uri=readonly)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA trusted_schema=OFF")
    return connection


def _package_family_name() -> str | None:
    """Read the actual Windows package identity, never infer it from file paths."""
    if os.name != "nt":
        return None
    import ctypes

    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel.GetCurrentPackageFamilyName
        function.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_wchar_p]
        function.restype = ctypes.c_long
        buffer = ctypes.create_unicode_buffer(256)
        size = ctypes.c_uint32(len(buffer))
        if function(ctypes.byref(size), buffer) != 0:
            return None
        value = buffer.value
        return value if re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", value) and value not in {".", ".."} else None
    except (AttributeError, OSError):
        return None


def _packaged_data_alias(data_dir: Path) -> Path | None:
    """Preserve the merged MSIX view, including unpackaged child processes.

    Such a child can have no package identity while Windows still redirects its
    file I/O. Observe only a non-link directory and require the exact documented
    LocalCache layout and identical relative suffix; never accept an arbitrary
    resolved directory as another data root.
    """
    local = os.getenv("LOCALAPPDATA")
    if not local:
        return None
    local_path = Path(local).resolve()
    try:
        relative = data_dir.relative_to(local_path)
    except ValueError:
        return None
    family = _package_family_name()
    if family:
        return local_path / "Packages" / family / "LocalCache" / "Local" / relative
    packages = local_path / "Packages"
    for probe in (data_dir, data_dir / "backups"):
        if not probe.exists():
            continue
        current = probe
        linked = False
        while True:
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                linked = True
                break
            if current == data_dir:
                break
            current = current.parent
        if linked:
            continue
        resolved = probe.resolve()
        try:
            parts = resolved.relative_to(packages).parts
        except ValueError:
            continue
        if len(parts) < 4 or parts[1:3] != ("LocalCache", "Local"):
            continue
        observed_family = parts[0]
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", observed_family) or observed_family in {".", ".."}:
            continue
        alias = packages / observed_family / "LocalCache" / "Local" / relative
        if resolved == alias / probe.relative_to(data_dir):
            return alias
    return None


class BackupService:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = (data_dir or default_data_dir()).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.root = self.data_dir / "backups"
        self._data_alias = _packaged_data_alias(self.data_dir)
        self._lock = threading.RLock()
        self._started = False
        self._lease = None
        self._directory(self.root)
        self._data_alias = self._data_alias or _packaged_data_alias(self.data_dir)
        for name in ("exports", "staged", "work"):
            self._directory(self.root / name)

    def _acquire_runtime_lease(self) -> None:
        # Hold this byte lock until every runtime store/job has been closed.
        lock_path = self._safe(self.data_dir / "runtime-data.lock")
        lease = lock_path.open("a+b")
        try:
            if lock_path.stat().st_size == 0:
                lease.write(b"0")
                lease.flush()
            lease.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lease.close()
            raise BackupError("另一艾拉运行时仍在使用数据，请先关闭后重启") from exc
        self._lease = lease

    def _guard_older_runtime(self) -> None:
        # Older releases did not hold runtime-data.lock. A live local port and
        # existing session file prevent installing over such a process too.
        if not self._safe(self.data_dir / "runtime.session").is_file():
            return
        try:
            port = int(os.getenv("ELLA_RUNTIME_PORT", "8766"))
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                pass
        except ConnectionRefusedError:
            return
        except OSError as exc:
            raise BackupError("无法确认旧运行时已经退出，暂不恢复数据库") from exc
        raise BackupError("旧艾拉运行时仍在监听，请先关闭后重启恢复")

    def close(self) -> None:
        with self._lock:
            if self._lease is not None:
                self._lease.close()
                self._lease = None

    def _safe(self, path: Path, *, within_backups: bool = False) -> Path:
        root = self.root if within_backups else self.data_dir
        if not path.absolute().is_relative_to(root):
            raise BackupError("备份路径超出艾拉数据目录")
        resolved = path.resolve()
        alias_root = self._data_alias / "backups" if within_backups and self._data_alias else self._data_alias
        mapped = alias_root is not None and resolved == alias_root / path.absolute().relative_to(root)
        if not resolved.is_relative_to(root) and not mapped:
            raise BackupError("备份路径超出艾拉数据目录")
        current = path
        while current != root.parent:
            if current.exists() or current.is_symlink():
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise BackupError("备份路径不能使用链接或重解析点")
            if current == root:
                break
            current = current.parent
        return path

    def _directory(self, path: Path) -> None:
        self._safe(path)
        path.mkdir(parents=True, exist_ok=True)

    def _remove_tree(self, path: Path) -> None:
        self._safe(path, within_backups=True)
        if path == self.root:
            raise BackupError("不能删除备份根目录")
        shutil.rmtree(path)

    def _write_json(self, path: Path, value: dict) -> None:
        self._safe(path, within_backups=True)
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        temporary.write_bytes(_json_bytes(value))
        os.replace(temporary, path)

    def _read_json(self, path: Path) -> dict | None:
        self._safe(path, within_backups=True)
        if not path.exists():
            return None
        if path.stat().st_size > 128 * 1024:
            raise BackupError("备份元数据过大")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise BackupError("备份元数据无效")
        return value

    @staticmethod
    def _identity(identity: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{32}", identity):
            raise BackupError("恢复预览编号无效")
        return identity

    def _validate_database(self, path: Path, name: str) -> dict[str, int]:
        self._safe(path)
        if path.stat().st_size > MAX_DATABASE_BYTES or path.read_bytes()[:16] != b"SQLite format 3\x00":
            raise BackupError(f"{name} 不是支持的 SQLite 数据库或超过 32 MiB")
        try:
            with closing(_connect(path, readonly=True)) as connection:
                connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
                deadline = time.monotonic() + 15
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise BackupError(f"{name} 完整性检查未通过")
                counts = {}
                for row in connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master"):
                    kind, table = row["type"], row["name"]
                    if kind == "table":
                        if table not in DATABASES[name]:
                            raise BackupError(f"{name} 包含不支持的数据表")
                        sql = (row["sql"] or "").casefold()
                        if "create virtual table" in sql and (
                            name != "memory.sqlite3" or table != "memory_fts" or "using fts5(" not in sql
                        ):
                            raise BackupError("备份包含不支持的虚拟表")
                        columns = {column["name"] for column in connection.execute(f'PRAGMA table_info("{table}")')}
                        allowed = set(DATABASES[name][table].split())
                        required = allowed - set(OPTIONAL_COLUMNS.get(table, "").split())
                        if not required <= columns <= allowed:
                            raise BackupError(f"{name} 的数据列不受支持")
                        if not table.startswith("memory_fts_") and table != "sqlite_sequence":
                            counts[table] = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    elif kind == "trigger":
                        if name != "memory.sqlite3" or table not in KNOWN_TRIGGERS or _sql_normalized(row["sql"]) != _sql_normalized(KNOWN_TRIGGERS[table]):
                            raise BackupError("备份包含不支持的触发器")
                    elif kind == "index":
                        if row["sql"] is not None and table not in KNOWN_INDEXES:
                            raise BackupError("备份包含不支持的索引")
                    else:
                        raise BackupError("备份不能包含视图或未知数据库结构")
                if name == "models.sqlite3" and "model_configs" in counts:
                    for row in connection.execute("SELECT * FROM model_configs"):
                        if row["slot"] not in {"chat", "action", "vision", "persona"}:
                            raise BackupError("模型配置位置无效")
                        ModelConfigInput(provider=row["provider"], model=row["model"], base_url=row["base_url"])
                if name == "voice.sqlite3" and "voice_configs" in counts:
                    for row in connection.execute("SELECT * FROM voice_configs"):
                        if row["slot"] not in {"asr", "tts"} or (row["slot"] == "asr" and row["provider"] == "fish"):
                            raise BackupError("语音配置位置无效")
                        VoiceConfigInput(provider=row["provider"], model=row["model"], base_url=row["base_url"], voice=row["voice"])
                if name == "tasks.sqlite3" and "agent_tasks" in counts:
                    for row in connection.execute("SELECT payload FROM agent_tasks"):
                        AgentTask.model_validate_json(row["payload"])
                return counts
        except (sqlite3.Error, ValueError, KeyError, IndexError) as exc:
            if isinstance(exc, BackupError):
                raise
            raise BackupError(f"{name} 数据校验失败") from exc

    def _snapshot(self, name: str, destination: Path) -> None:
        source = self._safe(self.data_dir / name)
        self._safe(destination, within_backups=True)
        if source.exists() and not source.is_file():
            raise BackupError("艾拉数据库路径不是普通文件")
        deadline = time.monotonic() + 20

        def progress(_status, _remaining, total):
            if time.monotonic() > deadline or total * 4096 > MAX_DATABASE_BYTES:
                raise BackupError("数据库快照超时或超过大小限制")

        with closing(_connect(destination)) as output:
            if source.is_file():
                with closing(_connect(source, readonly=True)) as original:
                    original.backup(output, pages=128, progress=progress)
            else:
                output.execute("VACUUM")
        self._validate_database(destination, name)

    def _manifest(self, directory: Path) -> dict:
        entries = {}
        for name in DATABASES:
            path = directory / name
            entries[name] = {"size": path.stat().st_size, "sha256": _hash(path)}
        if sum(entry["size"] for entry in entries.values()) > MAX_EXPANDED_BYTES:
            raise BackupError("展开后的数据库超过 128 MiB")
        return {"app": "ella-next", "version": 1, "format": "sqlite-backup-v1",
                "created_at": datetime.now(UTC).isoformat(), "files": entries}

    def export_archive(self) -> Path:
        with self._lock:
            work = Path(tempfile.mkdtemp(prefix="export-", dir=self.root / "work"))
            try:
                for name in DATABASES:
                    self._snapshot(name, work / name)
                # Export must not carry credential origins that can revive vault keys.
                self._unbind_keys(work)
                manifest = self._manifest(work)
                destination = self.root / "exports" / f"ella-backup-{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid4().hex[:8]}.zip"
                self._safe(destination, within_backups=True)
                with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("manifest.json", _json_bytes(manifest))
                    for name in DATABASES:
                        archive.write(work / name, name)
                if destination.stat().st_size > MAX_ARCHIVE_BYTES:
                    destination.unlink()
                    raise BackupError("备份 ZIP 超过当前导入支持的 8 MiB，请先整理历史数据")
                return destination
            finally:
                self._remove_tree(work)

    def _unbind_keys(self, directory: Path) -> None:
        for name, table, value in (("models.sqlite3", "model_configs", "restore-unbound"),
                                   ("voice.sqlite3", "voice_configs", None)):
            with closing(_connect(directory / name)) as connection, connection:
                columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
                if not columns:
                    continue
                if "key_origin" not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN key_origin TEXT")
                connection.execute(f"UPDATE {table} SET key_origin=?", (value,))

    def _preview(self, identity: str, manifest: dict, directory: Path) -> dict:
        entries = manifest.get("files")
        if manifest.get("app") != "ella-next" or manifest.get("version") != 1 or manifest.get("format") != "sqlite-backup-v1" or not isinstance(entries, dict) or set(entries) != set(DATABASES):
            raise BackupError("需要包含九个白名单数据库的艾拉备份")
        files = []
        expanded = 0
        for name in DATABASES:
            path = self._safe(directory / name, within_backups=True)
            entry = entries[name]
            if not isinstance(entry, dict) or type(entry.get("size")) is not int or not isinstance(entry.get("sha256"), str):
                raise BackupError("备份清单字段无效")
            expanded += entry["size"]
            if entry["size"] != path.stat().st_size or entry["size"] > MAX_DATABASE_BYTES or entry["sha256"] != _hash(path):
                raise BackupError(f"{name} 大小或哈希不匹配")
            counts = self._validate_database(path, name)
            files.append({"name": name, **entry, "rows": counts})
        if expanded > MAX_EXPANDED_BYTES:
            raise BackupError("备份展开大小超过 128 MiB")
        return {"id": identity, "created_at": manifest.get("created_at"), "files": files,
                "effects": ["重启后覆盖九个艾拉数据库；空库也会覆盖现有数据。",
                            "恢复前自动保留当前数据库备份，失败时回退。",
                            "原任务批准与对话授权清除，未完成任务须重新核对。",
                            "保存的模型和语音密钥绑定失效，需要重新填写密钥。环境变量配置不属于备份范围。",
                            "不包含浏览器登录、录音、游戏本体与存档；SQLite逐库快照。"]}

    def stage_restore(self, raw_zip: bytes) -> dict:
        if not raw_zip or len(raw_zip) > MAX_ARCHIVE_BYTES:
            raise BackupError("备份 ZIP 需要在 8 MiB 以内")
        with self._lock:
            identity = uuid4().hex
            directory = self.root / "staged" / identity
            self._directory(directory)
            try:
                with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
                    members = archive.infolist()
                    allowed = {*DATABASES, "manifest.json"}
                    if len(members) != len(allowed) or {item.filename for item in members} != allowed:
                        raise BackupError("备份包含缺失、重复或非白名单文件")
                    total = 0
                    for item in members:
                        if item.is_dir() or item.flag_bits & 1 or stat.S_ISLNK(item.external_attr >> 16):
                            raise BackupError("备份不能包含目录、加密文件或链接")
                        cap = 128 * 1024 if item.filename == "manifest.json" else MAX_DATABASE_BYTES
                        total += item.file_size
                        if item.file_size > cap or total > MAX_EXPANDED_BYTES + 128 * 1024:
                            raise BackupError("备份展开体积超限")
                        target = directory / item.filename
                        with archive.open(item) as source, target.open("wb") as output:
                            written = 0
                            while chunk := source.read(64 * 1024):
                                written += len(chunk)
                                if written > cap:
                                    raise BackupError("备份文件展开体积超限")
                                output.write(chunk)
                manifest = self._read_json(directory / "manifest.json")
                preview = self._preview(identity, manifest or {}, directory)
                return preview
            except (zipfile.BadZipFile, OSError, ValueError, RuntimeError) as exc:
                self._remove_tree(directory)
                if isinstance(exc, BackupError):
                    raise
                raise BackupError("备份 ZIP 校验失败") from exc

    def confirm_restore(self, identity: str) -> dict:
        with self._lock:
            identity = self._identity(identity)
            directory = self.root / "staged" / identity
            manifest = self._read_json(directory / "manifest.json")
            preview = self._preview(identity, manifest or {}, directory)
            pending = self._read_json(self.root / "pending.json")
            if pending and pending.get("id") != identity:
                raise BackupError("已有待恢复备份，请先取消")
            self._write_json(self.root / "pending.json", {"id": identity, "manifest_hash": _hash(directory / "manifest.json")})
            return {**preview, "pending": True, "restart_required": True}

    def cancel_restore(self, identity: str) -> None:
        with self._lock:
            identity = self._identity(identity)
            pending = self._read_json(self.root / "pending.json")
            if pending and pending.get("id") == identity:
                (self.root / "pending.json").unlink()
            directory = self.root / "staged" / identity
            if directory.exists():
                self._remove_tree(directory)

    def status(self) -> dict:
        with self._lock:
            pending = self._read_json(self.root / "pending.json")
            preview = None
            if pending:
                identity = self._identity(pending.get("id", ""))
                directory = self.root / "staged" / identity
                preview = self._preview(identity, self._read_json(directory / "manifest.json") or {}, directory)
            return {"pending": preview, "last_restore": self._read_json(self.root / "last-restore.json"),
                    "max_archive_bytes": MAX_ARCHIVE_BYTES}

    def _sanitize_restore(self, directory: Path) -> None:
        self._unbind_keys(directory)
        with closing(_connect(directory / "tasks.sqlite3")) as connection, connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='agent_tasks'").fetchone():
                for row in connection.execute("SELECT payload FROM agent_tasks").fetchall():
                    task = AgentTask.model_validate_json(row["payload"])
                    task.approved_plan_hash = None
                    uncertain = task.state in {TaskState.RUNNING, TaskState.NEEDS_RECONCILIATION}
                    for step in task.steps:
                        if step.state in {StepState.RUNNING, StepState.NEEDS_RECONCILIATION}:
                            step.state = StepState.NEEDS_RECONCILIATION
                            uncertain = True
                    if uncertain:
                        task.state = TaskState.NEEDS_RECONCILIATION
                        task.error = "从备份恢复，需核对现场；原批准已失效。"
                    elif task.state in {TaskState.READY, TaskState.WAITING_APPROVAL}:
                        task.state = TaskState.WAITING_APPROVAL
                        task.error = "从备份恢复，原批准已失效，请重新核对计划。"
                    connection.execute("UPDATE agent_tasks SET state=?,payload=? WHERE id=?",
                                       (task.state.value, task.model_dump_json(), task.id))
        with closing(_connect(directory / "dialogue-actions.sqlite3")) as connection, connection:
            for table in ("dialogue_bindings", "dialogue_receipts", "dialogue_handoffs"):
                if connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                    connection.execute(f"DELETE FROM {table}")

    def _install_database(self, source: Path, name: str) -> None:
        target = self._safe(self.data_dir / name)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = self._safe(self.data_dir / (name + suffix))
            sidecar.unlink(missing_ok=True)
        temporary = self._safe(self.data_dir / ("." + name + ".restore-" + uuid4().hex))
        try:
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _rollback(self, journal: dict) -> None:
        identity = self._identity(journal.get("id", ""))
        previous = journal.get("previous")
        if not isinstance(previous, list) or not set(previous) <= set(DATABASES):
            raise BackupError("恢复回退日志无效")
        rollback = self.root / "work" / identity / "before"
        for name in DATABASES:
            snapshot = self._safe(rollback / name, within_backups=True)
            self._validate_database(snapshot, name)
            if name in previous:
                self._install_database(snapshot, name)
            else:
                target = self._safe(self.data_dir / name)
                target.unlink(missing_ok=True)
                for suffix in ("-wal", "-shm", "-journal"):
                    self._safe(self.data_dir / (name + suffix)).unlink(missing_ok=True)

    def apply_pending_before_start(self) -> dict | None:
        """Call first in lifespan, before publishing the session or opening stores.

        No HTTP endpoint may call this. A service instance that has entered
        startup cannot install a newly confirmed archive without process restart.
        """
        with self._lock:
            pending = self._read_json(self.root / "pending.json")
            journal = self._read_json(self.root / "restore-journal.json")
            if self._started:
                if pending or journal:
                    raise BackupError("恢复需要重启服务，不能在运行中替换数据库")
                return None
            self._acquire_runtime_lease()
            try:
                if pending or journal:
                    self._guard_older_runtime()
            except BackupError:
                self.close()
                raise
            self._started = True
            if journal:
                if journal.get("phase") != "complete":
                    self._rollback(journal)
                    result = {"status": "rolled_back", "message": "上次恢复被中断，已回退到恢复前数据。"}
                    self._write_json(self.root / "last-restore.json", result)
                    (self.root / "pending.json").unlink(missing_ok=True)
                    (self.root / "restore-journal.json").unlink()
                    return result
                (self.root / "pending.json").unlink(missing_ok=True)
                (self.root / "restore-journal.json").unlink()
                return self._read_json(self.root / "last-restore.json")
            if not pending:
                return None
            try:
                identity = self._identity(pending.get("id", ""))
                staged = self.root / "staged" / identity
                if _hash(self._safe(staged / "manifest.json", within_backups=True)) != pending.get("manifest_hash"):
                    raise BackupError("确认后的备份清单已变化")
                self._preview(identity, self._read_json(staged / "manifest.json") or {}, staged)
                work = self.root / "work" / identity
                before, after = work / "before", work / "after"
                self._directory(before)
                self._directory(after)
                previous = [name for name in DATABASES if self._safe(self.data_dir / name).exists()]
                rollback_archive = self.export_archive()
                for name in DATABASES:
                    self._snapshot(name, before / name)
                    shutil.copyfile(staged / name, after / name)
                self._sanitize_restore(after)
                for name in DATABASES:
                    self._validate_database(after / name, name)
                journal = {"id": identity, "previous": previous, "phase": "installing"}
                self._write_json(self.root / "restore-journal.json", journal)
                for name in DATABASES:
                    self._install_database(after / name, name)
                result = {"status": "restored", "id": identity, "restored_at": datetime.now(UTC).isoformat(),
                          "rollback_archive": rollback_archive.name, "message": "恢复完成；未完成任务需重新确认，保存的服务密钥需重配。"}
                self._write_json(self.root / "last-restore.json", result)
                self._write_json(self.root / "restore-journal.json", {**journal, "phase": "complete"})
                (self.root / "pending.json").unlink()
                (self.root / "restore-journal.json").unlink()
                for obsolete in (staged, work):
                    try:
                        self._remove_tree(obsolete)
                    except OSError:
                        pass  # Completed restore remains valid if old temporary files are busy.
                return result
            except Exception as exc:  # noqa: BLE001 - every install failure must roll back
                active_journal = self._read_json(self.root / "restore-journal.json")
                if active_journal and active_journal.get("phase") != "complete":
                    self._rollback(active_journal)
                    (self.root / "restore-journal.json").unlink()
                result = {"status": "failed", "message": "恢复失败，当前数据未改变或已回退。", "error": str(exc)[:250] if isinstance(exc, BackupError) else "备份读取或安装失败"}
                self._write_json(self.root / "last-restore.json", result)
                (self.root / "pending.json").unlink(missing_ok=True)
                return result
