"""Local pet state, one-time reminders, and quiet-hour-aware prompts."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from ella_runtime.storage_paths import default_data_dir


class CompanionStore:
    def __init__(self, path: Path | None = None, *, timezone: str = "Asia/Shanghai") -> None:
        self.path = path or default_data_dir() / "companion.sqlite3"
        self.zone = ZoneInfo(timezone)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS companion_state (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    hunger_base REAL NOT NULL,
                    hunger_updated_at TEXT NOT NULL,
                    last_interaction_at TEXT NOT NULL,
                    last_question_at TEXT,
                    quiet_start TEXT NOT NULL,
                    quiet_end TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT
                )
                """
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS activity_events (
                    id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    UNIQUE(source_type, source_id, stage)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS companion_notifications (
                    id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT NOT NULL,
                    read_at TEXT
                )"""
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(companion_notifications)")}
            for field in ("spoken_at", "speech_error"):
                if field not in columns:
                    connection.execute(f"ALTER TABLE companion_notifications ADD COLUMN {field} TEXT")
            now = datetime.now(UTC).isoformat()
            connection.execute(
                """
                INSERT OR IGNORE INTO companion_state
                    (id, hunger_base, hunger_updated_at, last_interaction_at, quiet_start, quiet_end)
                VALUES (1, 80, ?, ?, '22:00', '08:00')
                """,
                (now, now),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def status(self, *, now: datetime | None = None) -> dict[str, object]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM companion_state WHERE id = 1").fetchone()
        elapsed = max(
            0.0, (current - datetime.fromisoformat(row["hunger_updated_at"])).total_seconds()
        )
        hunger = max(0, round(float(row["hunger_base"]) - elapsed / 3600 * 4))
        return {
            "hunger": hunger,
            "hungry": hunger <= 25,
            "quiet_start": row["quiet_start"],
            "quiet_end": row["quiet_end"],
            "quiet_now": self._quiet(current, row["quiet_start"], row["quiet_end"]),
        }

    def feed(self, *, now: datetime | None = None) -> dict[str, object]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        hunger = min(100, int(self.status(now=current)["hunger"]) + 30)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE companion_state SET hunger_base = ?, hunger_updated_at = ?,
                    last_interaction_at = ? WHERE id = 1
                """,
                (hunger, current.isoformat(), current.isoformat()),
            )
        return self.status(now=current)

    def touch(self, *, now: datetime | None = None) -> None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE companion_state SET last_interaction_at = ? WHERE id = 1",
                (current.isoformat(),),
            )

    def set_quiet_hours(self, start: str, end: str) -> dict[str, object]:
        self._parse_time(start)
        self._parse_time(end)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE companion_state SET quiet_start = ?, quiet_end = ? WHERE id = 1",
                (start, end),
            )
        return self.status()

    def add_reminder(self, title: str, due_at: datetime) -> dict[str, str | None]:
        if not title.strip() or due_at.tzinfo is None:
            raise ValueError("提醒需要标题和带时区的时间")
        identity = str(uuid4())
        now = datetime.now(UTC).isoformat()
        due = due_at.astimezone(UTC).isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO reminders(id, title, due_at, created_at) VALUES (?, ?, ?, ?)",
                (identity, title.strip(), due, now),
            )
        return {"id": identity, "title": title.strip(), "due_at": due, "delivered_at": None}

    def list_reminders(self) -> list[dict[str, str | None]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT r.id, r.title, r.due_at, r.delivered_at, n.read_at
                   FROM reminders AS r
                   LEFT JOIN companion_notifications AS n ON n.id = r.id
                   ORDER BY r.due_at ASC LIMIT 200"""
            ).fetchall()
        return [dict(row) for row in rows]

    def list_notifications(self, *, limit: int = 50) -> list[dict[str, str | None]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT id, source_type AS type, text, created_at, delivered_at, read_at, spoken_at, speech_error
                   FROM companion_notifications
                   WHERE read_at IS NULL OR id IN (
                       SELECT id FROM companion_notifications WHERE read_at IS NOT NULL
                       ORDER BY delivered_at DESC LIMIT ?
                   )
                   ORDER BY delivered_at DESC""",
                (min(max(limit, 1), 100),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_notification(self, identity: str) -> dict[str, str | None]:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM companion_notifications WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError(identity)
        return dict(row)

    def speech_receipt(self, identity: str, status: str, error: str | None = None) -> bool:
        if status not in {"played", "failed"}:
            raise ValueError("通知播放状态无效")
        with closing(self._connect()) as connection, connection:
            if status == "played":
                changed = connection.execute("UPDATE companion_notifications SET spoken_at=COALESCE(spoken_at,?), speech_error=NULL WHERE id=?", (datetime.now(UTC).isoformat(), identity)).rowcount
            else:
                changed = connection.execute("UPDATE companion_notifications SET speech_error=? WHERE id=? AND spoken_at IS NULL", ((error or "语音播报失败")[:300], identity)).rowcount
        return bool(changed)

    def mark_notification_read(self, identity: str, *, now: datetime | None = None) -> bool:
        current = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with closing(self._connect()) as connection, connection:
            return connection.execute(
                """UPDATE companion_notifications SET read_at = COALESCE(read_at, ?)
                   WHERE id = ?""",
                (current, identity),
            ).rowcount > 0

    def delete_reminder(self, identity: str) -> bool:
        with closing(self._connect()) as connection, connection:
            removed = connection.execute(
                "DELETE FROM reminders WHERE id = ?", (identity,)
            ).rowcount > 0
            if removed:
                connection.execute(
                    "DELETE FROM companion_notifications WHERE id = ? AND source_type = 'reminder'",
                    (identity,),
                )
            return removed

    def record_progress(
        self, source_type: str, source_id: str, stage: str, text: str,
        *, now: datetime | None = None,
    ) -> None:
        if not all(value.strip() for value in (source_type, source_id, stage, text)):
            raise ValueError("进展事件缺少来源或内容")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        with closing(self._connect()) as connection, connection:
            if source_type == "game":
                connection.execute(
                    "DELETE FROM activity_events WHERE source_type = 'game' "
                    "AND source_id = ? AND delivered_at IS NULL",
                    (source_id,),
                )
            connection.execute(
                """INSERT OR IGNORE INTO activity_events
                   (id, source_type, source_id, stage, text, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (str(uuid4()), source_type, source_id, stage, text[:300], current.isoformat()),
            )

    def poll_events(self, *, now: datetime | None = None) -> list[dict[str, str]]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        events: list[dict[str, str]] = []
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute("SELECT * FROM companion_state WHERE id = 1").fetchone()
            if self._quiet(current, state["quiet_start"], state["quiet_end"]):
                return []
            due = connection.execute(
                """
                SELECT id, title FROM reminders
                WHERE delivered_at IS NULL AND due_at <= ? ORDER BY due_at ASC LIMIT 20
                """,
                (current.isoformat(),),
            ).fetchall()
            for item in due:
                events.append({"type": "reminder", "id": item["id"], "text": item["title"]})
                connection.execute(
                    "UPDATE reminders SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL",
                    (current.isoformat(), item["id"]),
                )
                self._record_notification(
                    connection, item["id"], "reminder", item["title"], current
                )
            last_activity = connection.execute(
                "SELECT delivered_at FROM activity_events WHERE delivered_at IS NOT NULL "
                "ORDER BY delivered_at DESC LIMIT 1"
            ).fetchone()
            may_comment = (
                last_activity is None
                or current - datetime.fromisoformat(last_activity["delivered_at"])
                >= timedelta(seconds=30)
            )
            if may_comment:
                activity = connection.execute(
                    """SELECT id, text FROM activity_events WHERE delivered_at IS NULL
                       ORDER BY created_at ASC LIMIT 1"""
                ).fetchone()
                if activity is not None:
                    activity_type = connection.execute(
                        "SELECT source_type FROM activity_events WHERE id = ?", (activity["id"],)
                    ).fetchone()["source_type"]
                    events.append({
                        "type": activity_type if activity_type == "question" else "activity",
                        "id": activity["id"], "text": activity["text"]
                    })
                    connection.execute(
                        "UPDATE activity_events SET delivered_at = ? WHERE id = ?",
                        (current.isoformat(), activity["id"]),
                    )
                    self._record_notification(
                        connection, activity["id"],
                        "question" if activity_type == "question" else "activity",
                        activity["text"], current,
                    )
            last_interaction = datetime.fromisoformat(state["last_interaction_at"])
            last_question = (
                datetime.fromisoformat(state["last_question_at"])
                if state["last_question_at"]
                else None
            )
            if not events and current - last_interaction >= timedelta(hours=2) and (
                last_question is None or current - last_question >= timedelta(hours=4)
            ):
                question = {
                    "type": "question", "id": str(uuid4()),
                    "text": "最近怎么样？想聊聊，还是让我帮你做点事？",
                }
                events.append(question)
                self._record_notification(
                    connection, question["id"], question["type"], question["text"], current
                )
                connection.execute(
                    "UPDATE companion_state SET last_question_at = ? WHERE id = 1",
                    (current.isoformat(),),
                )
        return events

    @staticmethod
    def _record_notification(
        connection: sqlite3.Connection, identity: str, source_type: str,
        text: str, current: datetime,
    ) -> None:
        connection.execute(
            """INSERT OR IGNORE INTO companion_notifications
               (id, source_type, text, created_at, delivered_at)
               VALUES (?, ?, ?, ?, ?)""",
            (identity, source_type, text, current.isoformat(), current.isoformat()),
        )

    def _quiet(self, current: datetime, start: str, end: str) -> bool:
        local = current.astimezone(self.zone).time().replace(tzinfo=None)
        start_time = self._parse_time(start)
        end_time = self._parse_time(end)
        if start_time == end_time:
            return False
        if start_time < end_time:
            return start_time <= local < end_time
        return local >= start_time or local < end_time

    @staticmethod
    def _parse_time(value: str) -> time:
        try:
            parsed = time.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("安静时段使用 HH:MM 格式") from exc
        if parsed.tzinfo is not None or parsed.second or parsed.microsecond or len(value) != 5:
            raise ValueError("安静时段使用 HH:MM 格式")
        return parsed
