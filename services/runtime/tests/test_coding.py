import asyncio
import os
import shutil

import pytest

from ella_runtime.api import app, get_agent_engine
from ella_runtime.modules.agent.engine import AgentEngine
from ella_runtime.modules.agent.store import TaskStore
from ella_runtime.modules.applications import coding
from ella_runtime.modules.applications.coding import (
    GitReviewTool,
    ListProjectFilesTool,
    OpenCodeWorkspaceTool,
    ReadProjectFileTool,
    ReplaceProjectTextTool,
    RunProjectCheckTool,
)
from ella_runtime.modules.applications.workspace_text import WorkspaceTextTool
from tests.api_client import authorized_client


def test_coding_tools_confine_reads_and_exact_edits(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "app.py"
    source.write_text("answer = 1\n", encoding="utf-8", newline="")
    listing = ListProjectFilesTool(project)
    reader = ReadProjectFileTool(project)
    editor = ReplaceProjectTextTool(project)

    async def run():
        found = await listing.execute({}, action_id="list")
        assert found.details["files"] == ["app.py"]
        assert found.details["workspace"] == str(project.resolve())
        prior = await reader.execute({"path": "app.py"}, action_id="read")
        assert prior.details["content"] == "answer = 1\n"
        changed = await editor.execute({
            "path": "app.py", "old": "answer = 1", "new": "answer = 2",
            "expected_sha256": prior.details["sha256"],
        }, action_id="edit")
        assert changed.verified
        assert source.read_text(encoding="utf-8") == "answer = 2\n"
        with pytest.raises(ValueError, match="已变化"):
            await editor.execute({
                "path": "app.py", "old": "answer = 2", "new": "answer = 3",
                "expected_sha256": prior.details["sha256"],
            }, action_id="stale")
        with pytest.raises(ValueError, match="超出工作区"):
            await reader.execute({"path": "../outside.py"}, action_id="escape")

    asyncio.run(run())



def test_code_edit_rejects_oversize_result_before_writing(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "large.py"
    before = "x" * 190_000 + "OLD"
    source.write_text(before, encoding="utf-8")
    editor = ReplaceProjectTextTool(project)

    with pytest.raises(ValueError, match="替换后文件超过 200 KB"):
        asyncio.run(editor.execute({
            "path": "large.py", "old": "OLD", "new": "Y" * 20_000,
        }, action_id="oversize"))
    assert source.read_text(encoding="utf-8") == before


def test_coding_check_rejects_arbitrary_shell_command(tmp_path):
    tool = RunProjectCheckTool(tmp_path)
    with pytest.raises(ValueError, match="不支持"):
        asyncio.run(tool.execute({"check": "powershell -Command Remove-Item"}, action_id="bad"))


def test_default_code_workspace_finds_source_checkout(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / "apps" / "desktop").mkdir(parents=True)
    source = project / "services" / "runtime" / "src" / "ella_runtime" / "modules" / "applications" / "coding.py"
    source.parent.mkdir(parents=True)
    monkeypatch.delenv("ELLA_AGENT_WORKSPACE", raising=False)
    monkeypatch.delenv("ELLA_CODE_WORKSPACE", raising=False)
    monkeypatch.setattr(coding, "__file__", str(source))
    assert coding.default_code_workspace() == project


@pytest.mark.skipif(os.name != "nt", reason="Windows Explorer is required")
def test_open_code_workspace_requests_shell_open(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(coding.os, "startfile", lambda path: opened.append(path))
    evidence = asyncio.run(OpenCodeWorkspaceTool(tmp_path).execute({}, action_id="open"))
    assert evidence.verified
    assert evidence.details["system_open_accepted"] is True
    assert opened == [str(tmp_path.resolve())]


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_git_review_reports_non_repository_concisely(tmp_path):
    evidence = asyncio.run(GitReviewTool(tmp_path).execute({}, action_id="review"))
    assert not evidence.verified
    assert evidence.details["workspace"] == str(tmp_path.resolve())
    assert "not a git repository" in evidence.details["error"]
    assert "usage:" not in str(evidence.details)


def test_workspace_status_exposes_distinct_code_and_output_roots(tmp_path):
    project = tmp_path / "project"
    files = tmp_path / "files"
    engine = AgentEngine(
        object(), TaskStore(tmp_path / "tasks.sqlite3"),
        [ListProjectFilesTool(project), OpenCodeWorkspaceTool(project), WorkspaceTextTool(files)],
    )
    app.dependency_overrides[get_agent_engine] = lambda: engine
    try:
        response = authorized_client(app).get("/api/workspaces")
        assert response.status_code == 200
        assert response.json() == {"code": str(project.resolve()), "files": str(files.resolve())}
    finally:
        app.dependency_overrides.clear()
