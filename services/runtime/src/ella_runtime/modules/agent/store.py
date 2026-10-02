"""Local durable task journal."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from ella_runtime.modules.agent.contracts import AgentTask, StepState, TaskState
from ella_runtime.storage_paths import default_data_dir


class TaskStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_data_dir() / "tasks.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    id TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def save(self, task: AgentTask) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO agent_tasks(id, updated_at, state, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    state=excluded.state,
                    payload=excluded.payload
                """,
                (task.id, task.updated_at.isoformat(), task.state.value, task.model_dump_json()),
            )

    def get(self, identity: str) -> AgentTask:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload FROM agent_tasks WHERE id = ?", (identity,)
            ).fetchone()
        if row is None:
            raise KeyError(identity)
        return AgentTask.model_validate_json(row["payload"])

    def claim_ready(self, identity: str) -> AgentTask:
        """Atomically reserve one approved task so two requests cannot run it twice."""
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM agent_tasks WHERE id = ?", (identity,)
            ).fetchone()
            if row is None:
                raise KeyError(identity)
            task = AgentTask.model_validate_json(row["payload"])
            if task.state != TaskState.READY:
                raise ValueError("任务未获批准或需要先核对现场")
            if task.approved_plan_hash != task.plan_hash:
                raise ValueError("计划内容与原批准内容不一致，请重新规划")
            task.state = TaskState.RUNNING
            task.updated_at = datetime.now().astimezone()
            connection.execute(
                "UPDATE agent_tasks SET updated_at = ?, state = ?, payload = ? WHERE id = ?",
                (task.updated_at.isoformat(), task.state.value, task.model_dump_json(), identity),
            )
            connection.commit()
            return task

    def approve_plan(self, identity: str, expected_plan_hash: str) -> AgentTask:
        """Serialize approval with plan revision and execution claims."""
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM agent_tasks WHERE id = ?", (identity,)
            ).fetchone()
            if row is None:
                raise KeyError(identity)
            task = AgentTask.model_validate_json(row["payload"])
            if task.state != TaskState.WAITING_APPROVAL:
                raise ValueError("任务当前不能批准")
            if task.plan_hash != expected_plan_hash:
                raise ValueError("计划内容已变化，请刷新后重新确认")
            task.approved_plan_hash = task.plan_hash
            task.error = None
            task.state = TaskState.READY
            task.updated_at = datetime.now().astimezone()
            connection.execute(
                "UPDATE agent_tasks SET updated_at = ?, state = ?, payload = ? WHERE id = ?",
                (task.updated_at.isoformat(), task.state.value, task.model_dump_json(), identity),
            )
            return task

    def replace_plan(self, task: AgentTask, expected_plan_hash: str) -> None:
        """Commit a revision only while the persisted plan is still editable."""
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM agent_tasks WHERE id = ?", (task.id,)
            ).fetchone()
            if row is None:
                raise KeyError(task.id)
            current = AgentTask.model_validate_json(row["payload"])
            if current.plan_hash != expected_plan_hash:
                raise ValueError("计划内容已变化，请刷新后重新修改")
            if current.state not in {TaskState.WAITING_APPROVAL, TaskState.READY, TaskState.FAILED}:
                raise ValueError("当前任务不能修改计划；结果未知的步骤须先核对现场")
            if any(step.state in {StepState.RUNNING, StepState.NEEDS_RECONCILIATION} for step in current.steps):
                raise ValueError("结果未知的步骤须先核对现场，不能通过修改计划重放")
            completed = [step for step in current.steps if step.state == StepState.COMPLETE]
            if [step.model_dump() for step in task.steps[:len(completed)]] != [step.model_dump() for step in completed]:
                raise ValueError("已完成的步骤或证据已变化，请刷新后重新修改")
            connection.execute(
                "UPDATE agent_tasks SET updated_at = ?, state = ?, payload = ? WHERE id = ?",
                (task.updated_at.isoformat(), task.state.value, task.model_dump_json(), task.id),
            )

    def list(self, *, limit: int = 100) -> list[AgentTask]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT payload FROM agent_tasks ORDER BY updated_at DESC LIMIT ?",
                (min(max(limit, 1), 100),),
            ).fetchall()
        return [AgentTask.model_validate_json(row["payload"]) for row in rows]

    def has_running(self) -> bool:
        """Do not hide an active task behind the paginated history limit."""
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT 1 FROM agent_tasks WHERE state = ? LIMIT 1", (TaskState.RUNNING.value,)
            ).fetchone() is not None

    def mark_interrupted(self) -> list[AgentTask]:
        """On process start, never replay an action whose outcome may be unknown."""
        interrupted: list[AgentTask] = []
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT payload FROM agent_tasks WHERE state = ?", (TaskState.RUNNING.value,)
            ).fetchall()
        for row in rows:
            task = AgentTask.model_validate_json(row["payload"])
            task.state = TaskState.NEEDS_RECONCILIATION
            for step in task.steps:
                if step.state == StepState.RUNNING:
                    step.state = StepState.NEEDS_RECONCILIATION
            self.save(task)
            interrupted.append(task)
        return interrupted
