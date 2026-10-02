"""Durable local chat history used as short-term conversation context."""

import asyncio
import logging
import sqlite3
import weakref
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from ella_runtime.modules.models.contracts import ModelMessage, ModelPurpose, ModelRequest
from ella_runtime.modules.models.gateway import ModelGateway
from ella_runtime.storage_paths import default_data_dir

logger = logging.getLogger(__name__)


class ConversationSummarizer(Protocol):
    async def extract(self, messages: list[ModelMessage], *, source_ref: str) -> int | None: ...


class ConversationStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_data_dir() / "conversations.sqlite3"
        self._delete_listeners: list[Callable[[str], None]] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT,
                    kind TEXT NOT NULL DEFAULT 'chat'
                )
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(conversations)")
            }
            if "archived_at" not in columns:
                connection.execute("ALTER TABLE conversations ADD COLUMN archived_at TEXT")
            if "kind" not in columns:
                connection.execute(
                    "ALTER TABLE conversations ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat'"
                )
            connection.execute(
                "UPDATE conversations SET kind = 'voice' WHERE id = 'voice-main'"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, id)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS conversation_summary_windows (
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    end_message_count INTEGER NOT NULL,
                    processed_at TEXT NOT NULL,
                    PRIMARY KEY(conversation_id, end_message_count)
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA secure_delete=ON")
        return connection

    def create(self) -> str:
        identity = str(uuid4())
        now = datetime.now().astimezone().isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO conversations(id, created_at, updated_at) VALUES (?, ?, ?)",
                (identity, now, now),
            )
        return identity

    def ensure(self, identity: str, *, kind: str = "chat") -> str:
        """Keep a named conversation available across runtime restarts."""
        if kind not in {"chat", "voice"}:
            raise ValueError("未知对话类型")
        now = datetime.now().astimezone().isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR IGNORE INTO conversations(id, created_at, updated_at, kind) "
                "VALUES (?, ?, ?, ?)",
                (identity, now, now, kind),
            )
            if kind == "voice":
                connection.execute(
                    "UPDATE conversations SET kind = 'voice' WHERE id = ?", (identity,)
                )
        return identity

    def list_chats(self) -> list[dict[str, str | int | None]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT c.id, c.created_at, c.updated_at, c.archived_at,
                       COALESCE((SELECT substr(m.content, 1, 80) FROM messages AS m
                                 WHERE m.conversation_id = c.id AND m.role = 'user'
                                 ORDER BY m.id ASC LIMIT 1), '新对话') AS preview,
                       (SELECT COUNT(*) FROM messages AS m
                        WHERE m.conversation_id = c.id) AS message_count
                   FROM conversations AS c WHERE c.kind = 'chat'
                   ORDER BY c.updated_at DESC, c.id DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def chat_metadata(self, identity: str) -> dict[str, str | int | None]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT c.id, c.created_at, c.updated_at, c.archived_at,
                       COALESCE((SELECT substr(m.content, 1, 80) FROM messages AS m
                                 WHERE m.conversation_id = c.id AND m.role = 'user'
                                 ORDER BY m.id ASC LIMIT 1), '新对话') AS preview,
                       (SELECT COUNT(*) FROM messages AS m
                        WHERE m.conversation_id = c.id) AS message_count
                   FROM conversations AS c WHERE c.id = ? AND c.kind = 'chat'""",
                (identity,),
            ).fetchone()
        if row is None:
            raise KeyError(identity)
        return dict(row)

    def set_archived(self, identity: str, archived: bool) -> dict[str, str | int | None]:
        now = datetime.now().astimezone().isoformat()
        with closing(self._connect()) as connection, connection:
            changed = connection.execute(
                """UPDATE conversations SET archived_at = ?, updated_at = ?
                   WHERE id = ? AND kind = 'chat'""",
                (now if archived else None, now, identity),
            ).rowcount
        if not changed:
            raise KeyError(identity)
        return self.chat_metadata(identity)

    def is_archived(self, identity: str) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT archived_at FROM conversations WHERE id = ?", (identity,)
            ).fetchone()
        if row is None:
            raise KeyError(identity)
        return row["archived_at"] is not None

    def exists(self, identity: str) -> bool:
        with closing(self._connect()) as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM conversations WHERE id = ?", (identity,)
                ).fetchone()
                is not None
            )

    def history(self, identity: str, *, limit: int = 20) -> list[ModelMessage]:
        if not self.exists(identity):
            raise KeyError(identity)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
                (identity, min(max(limit, 1), 100)),
            ).fetchall()
        return [ModelMessage(role=row["role"], content=row["content"]) for row in reversed(rows)]

    def history_window(self, identity: str, start: int, end: int) -> list[ModelMessage]:
        if start < 1 or end < start or end - start >= 100:
            raise ValueError("对话来源范围无效")
        if not self.exists(identity):
            raise KeyError(identity)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT role, content FROM messages WHERE conversation_id = ? "
                "ORDER BY id LIMIT ? OFFSET ?",
                (identity, end - start + 1, start - 1),
            ).fetchall()
        return [ModelMessage(role=row["role"], content=row["content"]) for row in rows]

    def append_pair(self, identity: str, user: str, assistant: str) -> None:
        now = datetime.now().astimezone().isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            conversation = connection.execute(
                "SELECT archived_at FROM conversations WHERE id = ?", (identity,)
            ).fetchone()
            if conversation is None:
                raise KeyError(identity)
            if conversation["archived_at"] is not None:
                raise ValueError("对话已归档，请先恢复")
            connection.executemany(
                "INSERT INTO messages(conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                [(identity, "user", user, now), (identity, "assistant", assistant, now)],
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, identity)
            )

    def message_count(self, identity: str) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM messages WHERE conversation_id = ?", (identity,)
            ).fetchone()
        return int(row["count"])

    def summary_window_processed(self, identity: str, count: int) -> bool:
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT 1 FROM conversation_summary_windows "
                "WHERE conversation_id = ? AND end_message_count = ?",
                (identity, count),
            ).fetchone() is not None

    def mark_summary_window(self, identity: str, count: int) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR IGNORE INTO conversation_summary_windows "
                "(conversation_id, end_message_count, processed_at) VALUES (?, ?, ?)",
                (identity, count, datetime.now().astimezone().isoformat()),
            )

    def add_delete_listener(self, callback: Callable[[str], None]) -> None:
        self._delete_listeners.append(callback)

    def remove_delete_listener(self, callback: Callable[[str], None]) -> None:
        self._delete_listeners.remove(callback)

    def delete(self, identity: str) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute("DELETE FROM conversations WHERE id = ?", (identity,))
            deleted = cursor.rowcount > 0
        if deleted:
            for callback in tuple(self._delete_listeners):
                try:
                    callback(identity)
                except Exception:
                    logger.exception("Conversation delete listener failed for %s", identity)
        return deleted


class ConversationService:
    def __init__(
        self, gateway: ModelGateway, store: ConversationStore,
        summarizer: ConversationSummarizer | None = None,
        dialogue_actions=None,
    ) -> None:
        self.gateway = gateway
        self.store = store
        self.summarizer = summarizer
        self.dialogue_actions = dialogue_actions
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._summary_tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False
        self.store.add_delete_listener(self._on_deleted)

    def _lock_for(self, identity: str) -> asyncio.Lock:
        lock = self._locks.get(identity)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[identity] = lock
        return lock

    async def reply(self, message: str, conversation_id: str | None = None) -> tuple[str, str]:
        text = message.strip()
        if not text:
            raise ValueError("消息不能为空")
        if self._closed:
            raise RuntimeError("对话服务已关闭")
        identity = conversation_id or self.store.create()
        async with self._lock_for(identity):
            if self.store.is_archived(identity):
                raise ValueError("对话已归档，请先恢复")
            history = self.store.history(identity)
            action_reply = await self.dialogue_actions.process(text, history, identity) if self.dialogue_actions else None
            if action_reply is None:
                response = await self.gateway.generate(
                    ModelRequest(
                        purpose=ModelPurpose.CHAT,
                        messages=[*history, ModelMessage(role="user", content=text)],
                        task_id=identity,
                    )
                )
                reply = response.text
            else:
                reply = action_reply
            self.store.append_pair(identity, text, reply)
            self._schedule_summary(identity, self.store.message_count(identity))
            return identity, reply

    def _schedule_summary(self, identity: str, count: int) -> None:
        if self._closed or self.summarizer is None or count < 16:
            return
        current = self._summary_tasks.get(identity)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._summarize_pending(identity))
        self._summary_tasks[identity] = task
        task.add_done_callback(lambda done: self._summary_done(identity, done))

    def _summary_done(self, identity: str, task: asyncio.Task[None]) -> None:
        if self._summary_tasks.get(identity) is task:
            self._summary_tasks.pop(identity, None)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Unexpected conversation summary task failure for %s", identity)

    async def _summarize_pending(self, identity: str) -> None:
        summarizer = self.summarizer
        if summarizer is None:
            return
        try:
            while self.store.exists(identity):
                count = self.store.message_count(identity)
                pending = [
                    end for end in range(16, count + 1, 16)
                    if not self.store.summary_window_processed(identity, end)
                ]
                if not pending:
                    return
                for end in pending:
                    if not self.store.exists(identity):
                        return
                    messages = self.store.history_window(identity, end - 15, end)
                    if len(messages) != 16:
                        logger.warning(
                            "Incomplete conversation summary window for %s at %s", identity, end
                        )
                        return
                    source_ref = f"chat:{identity}:messages:{end - 15}-{end}"
                    saved = await summarizer.extract(messages, source_ref=source_ref)
                    if saved is None:
                        logger.warning(
                            "Conversation summary failed for %s at %s; will retry later",
                            identity, end,
                        )
                        return
                    if not self.store.exists(identity):
                        return
                    self.store.mark_summary_window(identity, end)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Conversation summary failed for %s; will retry later", identity)

    def _on_deleted(self, identity: str) -> None:
        task = self._summary_tasks.get(identity)
        if task is not None and not task.done():
            loop = task.get_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(task.cancel)

    async def delete(self, identity: str) -> bool:
        """Stop any in-flight extraction before removing the conversation."""
        async with self._lock_for(identity):
            task = self._summary_tasks.get(identity)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            return self.store.delete(identity)

    async def drain(self) -> None:
        """Wait for currently queued summaries, useful for orderly shutdown/tests."""
        while self._summary_tasks:
            tasks = tuple(self._summary_tasks.values())
            await asyncio.gather(*tasks, return_exceptions=True)
            for identity, task in tuple(self._summary_tasks.items()):
                if task.done() and self._summary_tasks.get(identity) is task:
                    self._summary_tasks.pop(identity, None)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._summary_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.store.remove_delete_listener(self._on_deleted)
