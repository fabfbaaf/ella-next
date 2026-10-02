"""Persist exact provider-reported token counts for the management view."""

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from ella_runtime.modules.models.contracts import TokenUsage
from ella_runtime.storage_paths import default_data_dir


class UsageStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_data_dir() / "usage.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS token_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    task_id TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    cache_read_tokens INTEGER
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS token_usage_date ON token_usage(occurred_at)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def record(self, usage: TokenUsage) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO token_usage (
                    occurred_at, provider, model, purpose, task_id,
                    input_tokens, output_tokens, cache_read_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    usage.occurred_at.isoformat(timespec="seconds"),
                    usage.provider,
                    usage.model,
                    usage.purpose.value,
                    usage.task_id,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cache_read_tokens,
                ),
            )

    def summary(
        self,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
        task_id: str | None = None,
    ) -> dict[str, object]:
        conditions: list[str] = []
        values: list[str] = []
        if from_date is not None:
            conditions.append("substr(occurred_at, 1, 10) >= ?")
            values.append(from_date.isoformat())
        if to_date is not None:
            conditions.append("substr(occurred_at, 1, 10) <= ?")
            values.append(to_date.isoformat())
        if task_id is not None:
            conditions.append("task_id = ?")
            values.append(task_id)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        aggregate = """
            COUNT(*) AS requests,
            SUM(CASE WHEN input_tokens IS NOT NULL AND output_tokens IS NOT NULL
                THEN 1 ELSE 0 END) AS reported_requests,
            SUM(CASE WHEN input_tokens IS NULL OR output_tokens IS NULL
                THEN 1 ELSE 0 END) AS unreported_requests,
            SUM(CASE WHEN input_tokens IS NOT NULL AND output_tokens IS NOT NULL
                THEN input_tokens ELSE 0 END) AS input_tokens,
            SUM(CASE WHEN input_tokens IS NOT NULL AND output_tokens IS NOT NULL
                THEN output_tokens ELSE 0 END) AS output_tokens,
            SUM(COALESCE(cache_read_tokens, 0)) AS cache_read_tokens
        """
        with closing(self._connect()) as connection, connection:
            totals = connection.execute(
                f"SELECT {aggregate} FROM token_usage {where}", values
            ).fetchone()
            grouped = connection.execute(
                f"""
                SELECT provider, model, purpose, task_id, {aggregate}
                FROM token_usage {where}
                GROUP BY provider, model, purpose, task_id
                ORDER BY requests DESC, provider, model
                LIMIT 200
                """,
                values,
            ).fetchall()
        return {
            "totals": self._numbers(totals),
            "groups": [
                {
                    "provider": row["provider"],
                    "model": row["model"],
                    "purpose": row["purpose"],
                    "task_id": row["task_id"],
                    **self._numbers(row),
                }
                for row in grouped
            ],
        }

    @staticmethod
    def _numbers(row: sqlite3.Row) -> dict[str, int]:
        keys = (
            "requests",
            "reported_requests",
            "unreported_requests",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
        )
        return {key: int(row[key] or 0) for key in keys}
