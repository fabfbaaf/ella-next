"""Constrained project inspection, exact edits, checks, and Git review tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

from ella_runtime.modules.agent.contracts import ToolEvidence, ToolPreconditionError
from ella_runtime.storage_paths import default_data_dir

_TEXT_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".md", ".toml",
    ".css", ".html", ".yaml", ".yml", ".txt", ".rs", ".java", ".cs",
}
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "target", "dist", "build"}


def default_code_workspace() -> Path:
    configured = os.getenv("ELLA_CODE_WORKSPACE") or os.getenv("ELLA_AGENT_WORKSPACE")
    if configured:
        return Path(configured).expanduser().resolve()
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists() and (candidate / "apps" / "desktop").is_dir():
            return candidate
    return (default_data_dir() / "workspace").resolve()


class CodingWorkspace:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root if root is not None else default_code_workspace()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, value: Any) -> Path:
        if not isinstance(value, str) or not value.strip() or Path(value).is_absolute():
            raise ToolPreconditionError("必须指定工作区内的相对路径")
        target = (self.root / value).resolve()
        if target == self.root or not target.is_relative_to(self.root):
            raise ToolPreconditionError("目标路径超出工作区")
        if target.suffix.lower() not in _TEXT_EXTENSIONS:
            raise ToolPreconditionError("只支持常见源码和文本文件")
        return target

    @staticmethod
    def _content(path: Path) -> str:
        if not path.is_file() or path.stat().st_size > 200_000:
            raise ValueError("文件不存在或超过 200 KB")
        return path.read_bytes().decode("utf-8")

    @classmethod
    def preflight_content(cls, path: Path) -> str:
        try:
            return cls._content(path)
        except (OSError, UnicodeError, ValueError) as exc:
            raise ToolPreconditionError(f"无法读取目标文件，尚未修改：{exc}") from exc


class ListProjectFilesTool:
    name = "code.list_files"
    description = '列出编程工作区内的源码文件。参数：{"contains":"可选文件名片段"}。'

    def __init__(self, root: Path | None = None) -> None:
        self.workspace = CodingWorkspace(root)

    async def execute(self, arguments: dict, *, action_id: str) -> ToolEvidence:
        needle = arguments.get("contains", "")
        if not isinstance(needle, str) or len(needle) > 100:
            raise ToolPreconditionError("文件名筛选条件无效")
        files = []
        for base, directories, names in os.walk(self.workspace.root):
            directories[:] = [name for name in directories if name not in _SKIP_DIRS]
            for name in names:
                path = Path(base) / name
                relative = path.relative_to(self.workspace.root).as_posix()
                if path.suffix.lower() in _TEXT_EXTENSIONS and needle.casefold() in relative.casefold():
                    files.append(relative)
                    if len(files) >= 200:
                        break
            if len(files) >= 200:
                break
        return ToolEvidence(
            verified=True,
            details={
                "workspace": str(self.workspace.root),
                "files": sorted(files),
                "truncated": len(files) >= 200,
            },
        )

    async def reconcile(self, arguments: dict, *, action_id: str) -> ToolEvidence | None:
        return await self.execute(arguments, action_id=action_id)


class ReadProjectFileTool:
    name = "code.read_file"
    description = '读取工作区内源码文件，最多 200 KB。参数：{"path":"相对路径"}。'

    def __init__(self, root: Path | None = None) -> None:
        self.workspace = CodingWorkspace(root)

    async def execute(self, arguments: dict, *, action_id: str) -> ToolEvidence:
        target = self.workspace.path(arguments.get("path"))
        content = self.workspace.preflight_content(target)
        return ToolEvidence(
            verified=True,
            details={
                "path": str(target), "content": content,
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
            },
        )

    async def reconcile(self, arguments: dict, *, action_id: str) -> ToolEvidence | None:
        return await self.execute(arguments, action_id=action_id)


class ReplaceProjectTextTool:
    name = "code.replace_text"
    description = (
        "精确替换工作区现有源码文件中唯一的一段文字，保留其他内容并核验。参数："
        '{"path":"相对路径","old":"原文","new":"新文",'
        '"expected_sha256":"读取文件得到的哈希，可选"}。'
    )

    def __init__(self, root: Path | None = None, *, receipt_path: Path | None = None) -> None:
        self.workspace = CodingWorkspace(root)
        workspace_id = hashlib.sha256(str(self.workspace.root).encode()).hexdigest()
        self.receipt_path = receipt_path or default_data_dir() / "code-edits" / f"{workspace_id}.sqlite3"

    def _arguments(self, arguments: dict) -> tuple[Path, str, str, str | None]:
        path = self.workspace.path(arguments.get("path"))
        old, new, expected = arguments.get("old"), arguments.get("new"), arguments.get("expected_sha256")
        if (
            set(arguments) - {"path", "old", "new", "expected_sha256"}
            or not isinstance(old, str) or not old or len(old) > 100_000
            or not isinstance(new, str) or len(new) > 100_000 or old == new
            or expected is not None and (
                not isinstance(expected, str) or len(expected) != 64
                or any(char not in "0123456789abcdef" for char in expected)
            )
        ):
            raise ToolPreconditionError("替换参数无效")
        return path, old, new, expected

    @staticmethod
    def _request_hash(arguments: dict) -> str:
        return hashlib.sha256(json.dumps(
            arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.receipt_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS code_edit_receipts (
                action_id TEXT PRIMARY KEY, path TEXT NOT NULL, request_hash TEXT NOT NULL,
                before_sha256 TEXT NOT NULL, after_sha256 TEXT NOT NULL
            )
        """)
        return connection

    async def execute(self, arguments: dict, *, action_id: str) -> ToolEvidence:
        path, old, new, expected = self._arguments(arguments)
        if not isinstance(action_id, str) or not action_id:
            raise ToolPreconditionError("编辑操作缺少有效 action id")
        # An action's first receipt is immutable: never replay an interrupted edit.
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT action_id FROM code_edit_receipts WHERE action_id = ?", (action_id,)
            ).fetchone()
            if prior is not None:
                raise RuntimeError("该编辑操作已有记录，请核对现场，不能再次执行")
            content = self.workspace.preflight_content(path)
            before_hash = hashlib.sha256(content.encode()).hexdigest()
            if expected is not None and before_hash != expected:
                raise ToolPreconditionError("文件内容已变化，已停止编辑")
            if content.count(old) != 1:
                raise ToolPreconditionError("原文在文件中必须恰好出现一次")
            updated = content.replace(old, new, 1).encode("utf-8")
            if len(updated) > 200_000:
                raise ToolPreconditionError("替换后文件超过 200 KB，已停止编辑")
            after_hash = hashlib.sha256(updated).hexdigest()
            connection.execute(
                "INSERT INTO code_edit_receipts VALUES (?, ?, ?, ?, ?)",
                (action_id, str(path), self._request_hash(arguments), before_hash, after_hash),
            )
        # The durable full-result hash exists before any target write, including deletion.
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(updated)
                stream.flush()
                os.fsync(stream.fileno())
            if self.workspace.path(arguments["path"]) != path or hashlib.sha256(path.read_bytes()).hexdigest() != before_hash:
                raise ToolPreconditionError("写入前文件内容或位置已变化，目标尚未修改")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        return ToolEvidence(
            verified=actual_hash == after_hash,
            details={"path": str(path), "before_sha256": before_hash,
                     "after_sha256": after_hash, "read_back_sha256": actual_hash},
        )

    async def reconcile(self, arguments: dict, *, action_id: str) -> ToolEvidence | None:
        path, _, _, _ = self._arguments(arguments)
        if not self.receipt_path.is_file():
            return ToolEvidence(verified=False, details={"path": str(path), "replayed": False, "error": "缺少该编辑操作的持久化回执，不能仅凭替换文字推断成功"})
        with closing(self._connect()) as connection:
            receipt = connection.execute(
                "SELECT * FROM code_edit_receipts WHERE action_id = ?", (action_id,)
            ).fetchone()
        if receipt is None or receipt["path"] != str(path) or receipt["request_hash"] != self._request_hash(arguments):
            return ToolEvidence(verified=False, details={"path": str(path), "replayed": False, "error": "操作回执缺失或与当前参数不一致"})
        try:
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            return ToolEvidence(verified=False, details={"path": str(path), "replayed": False, "error": str(exc)[:1000]})
        return ToolEvidence(
            verified=actual_hash == receipt["after_sha256"],
            details={"path": str(path), "before_sha256": receipt["before_sha256"],
                     "after_sha256": receipt["after_sha256"], "read_back_sha256": actual_hash,
                     "replayed": False},
        )


def _run_command(command: list[str], root: Path, timeout: int = 120) -> dict[str, Any]:
    completed = subprocess.run(
        command, cwd=root, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, check=False,
    )
    return {
        "exit_code": completed.returncode,
        "stdout": completed.stdout[-20_000:],
        "stderr": completed.stderr[-10_000:],
    }


class RunProjectCheckTool:
    name = "code.run_check"
    description = (
        "在编程工作区执行受限检查；参数："
        '{"check":"pytest|ruff|pnpm-build|cargo-check"}。'
        "不运行任意 shell 命令。"
    )

    def __init__(self, root: Path | None = None) -> None:
        self.workspace = CodingWorkspace(root)

    async def execute(self, arguments: dict, *, action_id: str) -> ToolEvidence:
        check = arguments.get("check")
        commands = {
            "pytest": [sys.executable, "-m", "pytest", "-q"],
            "ruff": [sys.executable, "-m", "ruff", "check", "."],
            "pnpm-build": ["pnpm", "build"],
            "cargo-check": ["cargo", "check"],
        }
        if not isinstance(check, str) or check not in commands:
            raise ToolPreconditionError("不支持的检查命令")
        result = await asyncio.to_thread(_run_command, commands[check], self.workspace.root)
        return ToolEvidence(verified=result["exit_code"] == 0, details={"check": check, **result})

    async def reconcile(self, arguments: dict, *, action_id: str) -> ToolEvidence | None:
        return None


class GitReviewTool:
    name = "code.git_review"
    description = "只读查看编程工作区的 Git 状态和未提交差异。参数：{}。不会提交。"

    def __init__(self, root: Path | None = None) -> None:
        self.workspace = CodingWorkspace(root)

    async def execute(self, arguments: dict, *, action_id: str) -> ToolEvidence:
        status = await asyncio.to_thread(
            _run_command, ["git", "status", "--short"], self.workspace.root, 20
        )
        if status["exit_code"] != 0:
            return ToolEvidence(
                verified=False,
                details={
                    "workspace": str(self.workspace.root),
                    "error": status["stderr"].splitlines()[0] if status["stderr"] else "Git 状态读取失败",
                },
            )
        diff = await asyncio.to_thread(
            _run_command, ["git", "diff", "--"], self.workspace.root, 20
        )
        return ToolEvidence(
            verified=diff["exit_code"] == 0,
            details={
                "workspace": str(self.workspace.root), "status": status["stdout"],
                "diff": diff["stdout"], "errors": diff["stderr"],
            },
        )

    async def reconcile(self, arguments: dict, *, action_id: str) -> ToolEvidence | None:
        return await self.execute(arguments, action_id=action_id)


class OpenCodeWorkspaceTool:
    name = "code.open_workspace"
    description = "在 Windows 资源管理器中打开当前编程工作区。参数：{}。"

    def __init__(self, root: Path | None = None) -> None:
        self.workspace = CodingWorkspace(root)

    async def execute(self, arguments: dict, *, action_id: str) -> ToolEvidence:
        if os.name != "nt":
            raise ToolPreconditionError("打开工作区目录仅支持 Windows")
        if arguments:
            raise ToolPreconditionError("打开工作区不需要参数")
        await asyncio.to_thread(os.startfile, str(self.workspace.root))
        return ToolEvidence(
            verified=True,
            details={"workspace": str(self.workspace.root), "system_open_accepted": True},
        )

    async def reconcile(self, arguments: dict, *, action_id: str) -> ToolEvidence | None:
        return None
