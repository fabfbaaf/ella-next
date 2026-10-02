"""Durable game action journal; an action ID is never blindly replayed."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from ella_runtime.modules.games.contracts import (
    ActionResult,
    ActionStatus,
    GameAction,
    GameSnapshot,
)
from ella_runtime.storage_paths import default_data_dir


class GameJournal:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_data_dir() / "game_actions.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS game_actions (
                    action_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    action_json TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    result_json TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )

            connection.execute(
                "CREATE TABLE IF NOT EXISTS game_play_sessions "
                "(game_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)"
            )

    def save_session(self, payload: dict[str, Any]) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO game_play_sessions(game_id,payload_json) VALUES (?,?) "
                "ON CONFLICT(game_id) DO UPDATE SET payload_json=excluded.payload_json",
                (payload["game_id"], json.dumps(payload, ensure_ascii=False)),
            )

    def load_sessions(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT payload_json FROM game_play_sessions").fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def begin(self, game_id: str, action: GameAction, before: GameSnapshot) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO game_actions(action_id, game_id, status, action_json, before_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    action.action_id,
                    game_id,
                    ActionStatus.UNKNOWN.value,
                    action.model_dump_json(),
                    before.model_dump_json(),
                    datetime.now().astimezone().isoformat(),
                ),
            )

    def finish(self, game_id: str, result: ActionResult) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE game_actions SET status = ?, result_json = ?, updated_at = ?
                WHERE action_id = ? AND game_id = ?
                """,
                (
                    result.status.value,
                    result.model_dump_json(),
                    datetime.now().astimezone().isoformat(),
                    result.action_id,
                    game_id,
                ),
            )

    def get(self, action_id: str) -> tuple[str, GameAction, GameSnapshot, ActionResult | None]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM game_actions WHERE action_id = ?", (action_id,)
            ).fetchone()
        if row is None:
            raise KeyError(action_id)
        return (
            row["game_id"],
            GameAction.model_validate_json(row["action_json"]),
            GameSnapshot.model_validate_json(row["before_json"]),
            ActionResult.model_validate_json(row["result_json"]) if row["result_json"] else None,
        )
