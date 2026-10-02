"""Durable memory with provenance, correction history, and hard deletion."""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from ella_runtime.modules.memory.contracts import (
    MemoryCorrection,
    MemoryCreate,
    MemoryKind,
    MemoryRecord,
    MemorySource,
    preference_claim,
)
from ella_runtime.storage_paths import default_data_dir


def _now() -> datetime:
    return datetime.now().astimezone()


def _bigrams(value: str) -> set[str]:
    compact = "".join(char for char in value.casefold() if char.isalnum())
    return {compact[index : index + 2] for index in range(len(compact) - 1)}


class MemoryStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_data_dir() / "memory.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_items (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_ref TEXT,
                    confidence REAL NOT NULL,
                    tags_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(memory_items)")}
            additions = {
                "topic": "TEXT", "preference_value": "TEXT",
                "preference_negative": "INTEGER NOT NULL DEFAULT 0",
                "active": "INTEGER NOT NULL DEFAULT 1", "superseded_by": "TEXT",
                "inactive_reason": "TEXT",
            }
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE memory_items ADD COLUMN {name} {definition}")
            # Add metadata to old explicit preferences without invalidating any
            # historical record merely because a newer item exists.
            for row in connection.execute(
                "SELECT id, content FROM memory_items WHERE kind = 'preference' AND topic IS NULL"
            ).fetchall():
                claim = preference_claim(row["content"])
                if claim:
                    connection.execute(
                        "UPDATE memory_items SET topic=?, preference_value=?, preference_negative=? WHERE id=?",
                        (claim.topic, claim.value, int(claim.negative), row["id"]),
                    )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
                    old_content TEXT NOT NULL,
                    old_source_type TEXT NOT NULL,
                    old_source_ref TEXT,
                    new_content TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    changed_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS memory_items_updated ON memory_items(updated_at)"
            )
            connection.execute(
                """CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    memory_id UNINDEXED, content, tags, tokenize='trigram'
                )"""
            )
            connection.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS memory_fts_insert AFTER INSERT ON memory_items BEGIN
                    INSERT INTO memory_fts(memory_id, content, tags)
                    VALUES (new.id, new.content, new.tags_json);
                END;
                CREATE TRIGGER IF NOT EXISTS memory_fts_update AFTER UPDATE ON memory_items BEGIN
                    DELETE FROM memory_fts WHERE memory_id = old.id;
                    INSERT INTO memory_fts(memory_id, content, tags)
                    VALUES (new.id, new.content, new.tags_json);
                END;
                CREATE TRIGGER IF NOT EXISTS memory_fts_delete AFTER DELETE ON memory_items BEGIN
                    DELETE FROM memory_fts WHERE memory_id = old.id;
                END;
                """
            )
            connection.execute(
                """INSERT INTO memory_fts(memory_id, content, tags)
                   SELECT id, content, tags_json FROM memory_items
                   WHERE id NOT IN (SELECT memory_id FROM memory_fts)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS memory_vectors (
                    memory_id TEXT PRIMARY KEY REFERENCES memory_items(id) ON DELETE CASCADE,
                    model TEXT NOT NULL,
                    vector_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA secure_delete=ON")
        return connection

    @staticmethod
    def _record(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"],
            kind=MemoryKind(row["kind"]),
            content=row["content"],
            source_type=MemorySource(row["source_type"]),
            source_ref=row["source_ref"],
            confidence=row["confidence"],
            tags=json.loads(row["tags_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            topic=row["topic"], preference_value=row["preference_value"],
            preference_negative=bool(row["preference_negative"]), active=bool(row["active"]),
            superseded_by=row["superseded_by"], inactive_reason=row["inactive_reason"],
        )

    def create(
        self, item: MemoryCreate, *, supersede_topic: str | None = None,
        supersede_value: str | None = None, supersede_reason: str | None = None,
    ) -> MemoryRecord:
        identity = str(uuid4())
        created_at = _now().isoformat(timespec="seconds")
        claim = preference_claim(item.content) if item.kind == MemoryKind.PREFERENCE else None
        topic = item.topic or (claim.topic if claim else None)
        value = item.preference_value or (claim.value if claim else None)
        negative = item.preference_negative or bool(claim and claim.negative)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO memory_items (
                    id, kind, content, source_type, source_ref, confidence,
                    tags_json, created_at, updated_at, topic, preference_value, preference_negative
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity,
                    item.kind.value,
                    item.content,
                    item.source_type.value,
                    item.source_ref,
                    item.confidence,
                    json.dumps(item.tags, ensure_ascii=False),
                    created_at,
                    created_at,
                    topic, value, int(negative),
                ),
            )
            if supersede_topic:
                where = "AND preference_value = ?" if supersede_value is not None else ""
                parameters = [identity, supersede_reason or "用户明确更新偏好", created_at,
                              supersede_topic, identity]
                if supersede_value is not None:
                    parameters.append(supersede_value)
                connection.execute(
                    "UPDATE memory_items SET active=0, superseded_by=?, inactive_reason=?, updated_at=? "
                    "WHERE kind='preference' AND active=1 AND topic=? AND id != ? " + where,
                    parameters,
                )
        result = self.get(identity)
        assert result is not None
        return result

    def get(self, identity: str) -> MemoryRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM memory_items WHERE id = ?", (identity,)
            ).fetchone()
        return self._record(row) if row is not None else None

    def has_source(self, source_ref: str) -> bool:
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT 1 FROM memory_items WHERE source_ref = ? LIMIT 1", (source_ref,)
            ).fetchone() is not None

    def list(
        self, *, kind: MemoryKind | None = None, limit: int = 100, active_only: bool = False,
    ) -> list[MemoryRecord]:
        limit = max(1, min(limit, 10000))
        filters = []
        values = []
        if kind:
            filters.append("kind = ?")
            values.append(kind.value)
        if active_only:
            filters.append("active = 1")
        where = "WHERE " + " AND ".join(filters) if filters else ""
        values.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM memory_items {where} ORDER BY updated_at DESC, id LIMIT ?", values
            ).fetchall()
        return [self._record(row) for row in rows]

    def search(self, query: str, *, limit: int = 5) -> list[MemoryRecord]:
        return self.search_hybrid(query, limit=limit)

    def set_embedding(self, identity: str, *, model: str, vector: list[float]) -> None:
        if not model.strip() or not vector or len(vector) > 8192:
            raise ValueError("无效的记忆向量")
        if not all(math.isfinite(value) for value in vector):
            raise ValueError("记忆向量包含无效数值")
        with closing(self._connect()) as connection, connection:
            if connection.execute("SELECT 1 FROM memory_items WHERE id = ? AND active = 1", (identity,)).fetchone() is None:
                raise KeyError(identity)
            connection.execute(
                """INSERT INTO memory_vectors(memory_id, model, vector_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(memory_id) DO UPDATE SET
                   model=excluded.model, vector_json=excluded.vector_json,
                   updated_at=excluded.updated_at""",
                (identity, model.strip(), json.dumps(vector), _now().isoformat(timespec="seconds")),
            )

    def missing_embeddings(self, model: str, *, limit: int = 20) -> list[MemoryRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT m.* FROM memory_items m LEFT JOIN memory_vectors v ON v.memory_id=m.id
                   WHERE m.active = 1 AND (v.memory_id IS NULL OR v.model != ?)
                   ORDER BY m.updated_at DESC LIMIT ?""",
                (model, max(1, min(limit, 100))),
            ).fetchall()
        return [self._record(row) for row in rows]

    def search_hybrid(
        self, query: str, *, limit: int = 5,
        query_vector: list[float] | None = None, embedding_model: str | None = None,
    ) -> list[MemoryRecord]:
        query = query.strip().casefold()
        if not query:
            return []
        query_compact = "".join(char for char in query if char.isalnum())
        query_grams = _bigrams(query)
        fts_ids: set[str] = set()
        semantic: dict[str, float] = {}
        with closing(self._connect()) as connection:
            grams = list(dict.fromkeys(query_compact[i:i + 3] for i in range(len(query_compact) - 2)))[:32]
            if grams:
                expression = " OR ".join('"' + gram.replace('"', '""') + '"' for gram in grams)
                try:
                    fts_ids = {
                        row["memory_id"] for row in connection.execute(
                            "SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ? LIMIT 500",
                            (expression,),
                        )
                    }
                except sqlite3.OperationalError:
                    # A short or provider-specific FTS query can have no valid trigram.
                    pass
            if query_vector is not None and embedding_model:
                for row in connection.execute(
                    "SELECT memory_id, vector_json FROM memory_vectors WHERE model = ?",
                    (embedding_model,),
                ):
                    stored = json.loads(row["vector_json"])
                    if len(stored) != len(query_vector):
                        continue
                    denominator = math.sqrt(sum(x * x for x in stored)) * math.sqrt(
                        sum(x * x for x in query_vector)
                    )
                    if denominator:
                        semantic[row["memory_id"]] = sum(
                            x * y for x, y in zip(stored, query_vector)
                        ) / denominator
        ranked: list[tuple[float, MemoryRecord]] = []
        for record in self.list(limit=10000, active_only=True):
            content_compact = "".join(char for char in record.content.casefold() if char.isalnum())
            score = 0.0
            if query_compact in content_compact or content_compact in query_compact:
                score += 4
            overlap = len(query_grams & _bigrams(record.content))
            if overlap >= 2 and query_grams:
                score += overlap / len(query_grams)
            for tag in record.tags:
                normalized = tag.casefold()
                if normalized and normalized in query:
                    score += 3
            if record.id in fts_ids:
                score += 0.5
            similarity = semantic.get(record.id, 0.0)
            if similarity >= 0.55:
                score += 2 * similarity
            if score >= 0.18:
                ranked.append((score, record))
        ranked.sort(key=lambda pair: (pair[0], pair[1].updated_at), reverse=True)
        return [record for _, record in ranked[: max(1, min(limit, 20))]]

    def correct(self, identity: str, correction: MemoryCorrection) -> MemoryRecord:
        changed_at = _now().isoformat(timespec="seconds")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM memory_items WHERE id = ?", (identity,)
            ).fetchone()
            if row is None:
                raise KeyError(identity)
            current = self._record(row)
            tags = current.tags if correction.tags is None else correction.tags
            claim = preference_claim(correction.content) if current.kind == MemoryKind.PREFERENCE else None
            connection.execute(
                """
                INSERT INTO memory_revisions (
                    memory_id, old_content, old_source_type, old_source_ref,
                    new_content, reason, changed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity,
                    current.content,
                    current.source_type.value,
                    current.source_ref,
                    correction.content,
                    correction.reason,
                    changed_at,
                ),
            )
            connection.execute(
                """
                UPDATE memory_items
                SET content = ?, source_type = ?, source_ref = NULL,
                    confidence = 1.0, tags_json = ?, updated_at = ?, topic = ?,
                    preference_value = ?, preference_negative = ?, active = 1,
                    superseded_by = NULL, inactive_reason = NULL
                WHERE id = ?
                """,
                (
                    correction.content,
                    MemorySource.MANUAL.value,
                    json.dumps(tags, ensure_ascii=False),
                    changed_at,
                    claim.topic if claim else None,
                    claim.value if claim else None,
                    int(bool(claim and claim.negative)),
                    identity,
                ),
            )
            connection.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (identity,))
        result = self.get(identity)
        assert result is not None
        return result

    def delete(self, identity: str) -> bool:
        with closing(self._connect()) as connection, connection:
            removed = connection.execute("DELETE FROM memory_items WHERE id = ?", (identity,))
            return removed.rowcount > 0

    def export(self) -> dict[str, object]:
        with closing(self._connect()) as connection:
            items = connection.execute(
                "SELECT * FROM memory_items ORDER BY created_at, id"
            ).fetchall()
            revisions = connection.execute(
                """
                SELECT memory_id, old_content, old_source_type, old_source_ref,
                       new_content, reason, changed_at
                FROM memory_revisions ORDER BY id
                """
            ).fetchall()
        return {
            "version": 1,
            "items": [self._record(row).model_dump(mode="json") for row in items],
            "revisions": [dict(row) for row in revisions],
        }
