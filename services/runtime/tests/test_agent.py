import asyncio
import json
from datetime import UTC, datetime

import pytest

from ella_runtime.api import app, get_agent_engine
from ella_runtime.modules.agent.contracts import StepState, TaskState, ToolEvidence
from ella_runtime.modules.agent.engine import AgentEngine, AgentError
from ella_runtime.modules.agent.store import TaskStore
from ella_runtime.modules.applications.workspace_text import DesktopTextTool, WorkspaceTextTool
from ella_runtime.modules.models.contracts import ModelPurpose, ModelResponse, TokenUsage
from tests.api_client import authorized_client


class Planner:
    def __init__(self, steps):
        self.steps = steps
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        return ModelResponse(
            text=json.dumps({"steps": self.steps}),
            provider="test",
            model="action",
            usage=TokenUsage(
                provider="test",
                model="action",
                purpose=ModelPurpose.ACTION,
                occurred_at=datetime.now(UTC),
            ),
        )


class Tool:
    name = "write_file"
    description = "Write a file and read it back"

    def __init__(self):
        self.actions = []
        self.actual = {}

    async def execute(self, arguments, *, action_id):
        self.actions.append(action_id)
        self.actual[action_id] = arguments["content"]
        return ToolEvidence(verified=True, details={"read_back": arguments["content"]})

    async def reconcile(self, arguments, *, action_id):
        if self.actual.get(action_id) == arguments["content"]:
            return ToolEvidence(verified=True, details={"read_back": arguments["content"]})
        return None


def test_action_plan_requires_approval_and_records_verified_result(tmp_path):
    tool = Tool()
    planner = Planner([{"tool": "write_file", "arguments": {"content": "hello"}, "reason": "save"}])
    engine = AgentEngine(planner, TaskStore(tmp_path / "tasks.sqlite3"), [tool])

    async def run():
        task = await engine.plan("写入文档")
        assert task.state == TaskState.WAITING_APPROVAL
        assert task.steps[0].risk_level == "review"
        assert not engine.is_low_risk(task)
        assert planner.requests[0].purpose == ModelPurpose.ACTION
        with pytest.raises(AgentError):
            await engine.run(task.id)
        engine.approve(task.id, task.plan_hash)
        completed = await engine.run(task.id)
        assert completed.state == TaskState.COMPLETE
        assert completed.steps[0].evidence == {"read_back": "hello"}
        with pytest.raises(AgentError):
            await engine.run(task.id)
        return task.id

    task_id = asyncio.run(run())
    assert len(tool.actions) == 1
    assert TaskStore(tmp_path / "tasks.sqlite3").get(task_id).state == TaskState.COMPLETE


def test_restart_reconciles_uncertain_action_without_replay(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    tool = Tool()
    planner = Planner([{"tool": "write_file", "arguments": {"content": "hello"}}])
    first = AgentEngine(planner, TaskStore(path), [tool])
    task = asyncio.run(first.plan("写入文档"))
    first.approve(task.id, task.plan_hash)
    claimed = first.store.claim_ready(task.id)
    claimed.steps[0].state = StepState.RUNNING
    first.store.save(claimed)
    tool.actual[claimed.steps[0].id] = "hello"

    restarted = AgentEngine(planner, TaskStore(path), [tool])
    assert restarted.store.get(task.id).state == TaskState.NEEDS_RECONCILIATION
    assert restarted.store.get(task.id).steps[0].state == StepState.NEEDS_RECONCILIATION

    async def run():
        checked = await restarted.reconcile(task.id)
        assert checked.state == TaskState.READY
        completed = await restarted.run(task.id)
        assert completed.state == TaskState.COMPLETE

    asyncio.run(run())
    assert tool.actions == []


def test_reconcile_unknown_result_stays_paused_and_invalid_tool_rejected(tmp_path):
    tool = Tool()
    planner = Planner([{"tool": "invented", "arguments": {}}])
    engine = AgentEngine(planner, TaskStore(tmp_path / "tasks.sqlite3"), [tool])
    with pytest.raises(AgentError, match="计划无效"):
        asyncio.run(engine.plan("操作游戏"))

    planner.steps = [{"tool": "write_file", "arguments": {"content": "x"}}]
    task = asyncio.run(engine.plan("写入文档"))
    engine.approve(task.id, task.plan_hash)
    claimed = engine.store.claim_ready(task.id)
    claimed.steps[0].state = StepState.NEEDS_RECONCILIATION
    claimed.state = TaskState.NEEDS_RECONCILIATION
    engine.store.save(claimed)
    result = asyncio.run(engine.reconcile(task.id))
    assert result.state == TaskState.NEEDS_RECONCILIATION
    assert tool.actions == []


def test_workspace_text_tool_is_confined_and_verifies_file(tmp_path):
    workspace = tmp_path / "workspace"
    tool = WorkspaceTextTool(workspace)

    async def run():
        evidence = await tool.execute(
            {"path": "notes/hello.md", "content": "你好"}, action_id="one"
        )
        assert evidence.verified
        assert evidence.details["bytes"] == len("你好".encode())
        assert (workspace / "notes" / "hello.md").read_text(encoding="utf-8") == "你好"
        assert (
            await tool.reconcile({"path": "notes/hello.md", "content": "你好"}, action_id="one")
        ).verified
        with pytest.raises(ValueError, match="超出工作区"):
            await tool.execute({"path": "../outside.txt", "content": "x"}, action_id="two")
        with pytest.raises(FileExistsError):
            await tool.execute({"path": "notes/hello.md", "content": "new"}, action_id="three")

    asyncio.run(run())


def test_desktop_text_tool_creates_exact_name_without_overwriting(tmp_path):
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    tool = DesktopTextTool(desktop)

    async def run():
        evidence = await tool.execute({"name": "测试", "content": "你好"}, action_id="one")
        assert evidence.verified
        assert evidence.details["path"] == str(desktop / "测试")
        assert (desktop / "测试").read_text(encoding="utf-8") == "你好"
        assert not (desktop / "测试.txt").exists()
        with pytest.raises(FileExistsError):
            await tool.execute({"name": "测试", "content": "新内容"}, action_id="two")
        assert (desktop / "测试").read_text(encoding="utf-8") == "你好"
        assert await tool.reconcile({"name": "测试", "content": "你好"}, action_id="other") is None
        (desktop / "已有").write_bytes(b"")
        with pytest.raises(FileExistsError):
            await tool.execute({"name": "已有"}, action_id="existing-empty")
        assert await tool.reconcile({"name": "已有"}, action_id="existing-empty") is None
        for invalid_name in ("../outside", "folder/file", "folder\\file", "C:\\outside", "CON"):
            with pytest.raises(ValueError):
                await tool.execute({"name": invalid_name, "content": "x"}, action_id="three")
        for office_name in ("报告.docx", "数据.xlsx", "展示.pptx"):
            with pytest.raises(ValueError, match="不能创建 Word、Excel 或 PPT"):
                await tool.execute({"name": office_name, "content": "fake"}, action_id="office")
            assert not (desktop / office_name).exists()
        with pytest.raises(ValueError, match="覆盖"):
            await tool.execute({"name": "another", "overwrite": True}, action_id="four")

    asyncio.run(run())


def test_desktop_goal_rejects_workspace_substitution_and_changed_filename(tmp_path):
    workspace = tmp_path / "workspace"
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    workspace_tool = WorkspaceTextTool(workspace)
    desktop_tool = DesktopTextTool(desktop)
    planner = Planner([
        {"tool": workspace_tool.name, "arguments": {"path": "测试.txt", "content": ""}}
    ])
    store = TaskStore(tmp_path / "tasks.sqlite3")
    engine = AgentEngine(planner, store, [workspace_tool, desktop_tool])
    goal = "在桌面创建一个新文件，文件名是测试"

    with pytest.raises(AgentError, match="没有在桌面"):
        asyncio.run(engine.plan(goal))
    assert store.list() == []
    assert not (workspace / "测试.txt").exists()

    planner.steps = [{"tool": desktop_tool.name, "arguments": {"name": "测试.txt"}}]
    with pytest.raises(AgentError, match="更改了用户指定的文件名"):
        asyncio.run(engine.plan(goal))
    assert store.list() == []

    planner.steps = [{"tool": desktop_tool.name, "arguments": {"name": "测试"}}]
    task = asyncio.run(engine.plan(goal))
    assert engine.is_low_risk(task)
    engine.approve(task.id, task.plan_hash)
    completed = asyncio.run(engine.run(task.id))
    assert completed.state == TaskState.COMPLETE
    assert completed.steps[0].evidence["path"] == str(desktop / "测试")
    assert (desktop / "测试").read_bytes() == b""


def test_desktop_goal_without_desktop_tool_fails_before_planning(tmp_path):
    planner = Planner([
        {"tool": "workspace.write_text", "arguments": {"path": "测试.txt", "content": ""}}
    ])
    engine = AgentEngine(
        planner, TaskStore(tmp_path / "tasks.sqlite3"),
        [WorkspaceTextTool(tmp_path / "workspace")],
    )
    with pytest.raises(AgentError, match="没有桌面文件工具"):
        asyncio.run(engine.plan("在桌面创建一个新文件，文件名是测试"))
    assert planner.requests == []


@pytest.mark.parametrize("goal", [
    "在桌面创建 Word 文档，文件名是报告.docx",
    "在桌面创建 Excel 工作簿，文件名是数据",
    "在桌面创建 PPT 演示文稿，文件名是展示.pptx",
    "在桌面创建一个文件，文件名是报告.docx",
])
def test_desktop_office_goal_cannot_be_completed_as_plain_text(tmp_path, goal):
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    desktop_tool = DesktopTextTool(desktop)
    planner = Planner([{"tool": desktop_tool.name, "arguments": {"name": "报告.docx"}}])
    store = TaskStore(tmp_path / "tasks.sqlite3")
    engine = AgentEngine(planner, store, [desktop_tool])

    with pytest.raises(AgentError, match="桌面文本工具不能创建"):
        asyncio.run(engine.plan(goal))
    assert planner.requests == []
    assert store.list() == []
    assert list(desktop.iterdir()) == []


def test_task_api_plans_then_waits_for_explicit_approval(tmp_path):
    tool = WorkspaceTextTool(tmp_path / "workspace")
    planner = Planner([{"tool": tool.name, "arguments": {"path": "hello.txt", "content": "hello"}}])
    engine = AgentEngine(planner, TaskStore(tmp_path / "tasks.sqlite3"), [tool])
    app.dependency_overrides[get_agent_engine] = lambda: engine
    try:
        client = authorized_client(app)
        created = client.post("/api/tasks", json={"goal": "创建文本"})
        assert created.status_code == 201
        task_id = created.json()["id"]
        assert created.json()["state"] == "waiting_approval"
        assert not (tmp_path / "workspace" / "hello.txt").exists()
        assert client.post(f"/api/tasks/{task_id}/run").status_code == 409
        assert client.post(
            f"/api/tasks/{task_id}/approve", json={"plan_hash": created.json()["plan_hash"]}
        ).json()["state"] == "ready"
        completed = client.post(f"/api/tasks/{task_id}/run")
        assert completed.status_code == 200
        assert completed.json()["state"] == "complete"
        assert (tmp_path / "workspace" / "hello.txt").read_text() == "hello"
        rejected = client.post(
            "/api/tasks", json={"goal": "恶意请求"}, headers={"Origin": "https://example.org"}
        )
        assert rejected.status_code == 403
        assert len(planner.requests) == 1
    finally:
        app.dependency_overrides.clear()


def test_task_api_auto_runs_only_safe_new_file_steps(tmp_path):
    tool = WorkspaceTextTool(tmp_path / "workspace")
    planner = Planner([{
        "tool": tool.name,
        "arguments": {"path": "safe.txt", "content": "hello", "overwrite": False},
    }])
    engine = AgentEngine(planner, TaskStore(tmp_path / "tasks.sqlite3"), [tool])
    app.dependency_overrides[get_agent_engine] = lambda: engine
    try:
        client = authorized_client(app)
        created = client.post(
            "/api/tasks", json={"goal": "创建文本", "auto_run_safe": True}
        )
        assert created.status_code == 201
        assert created.json()["state"] == "complete"
        assert (tmp_path / "workspace" / "safe.txt").read_text() == "hello"

        planner.steps = [{
            "tool": tool.name,
            "arguments": {"path": "safe.txt", "content": "changed", "overwrite": True},
        }]
        waiting = client.post(
            "/api/tasks", json={"goal": "覆盖文本", "auto_run_safe": True}
        )
        assert waiting.json()["state"] == "waiting_approval"
        assert (tmp_path / "workspace" / "safe.txt").read_text() == "hello"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(("path", "extra", "expected_risk"), [
    ("notes.md", {}, "low"),
    ("install.ps1", {}, "high"),
    ("run.cmd", {}, "high"),
    ("notes.md", {"unknown_option": True}, "review"),
    ("notes.md", {"overwrite": True}, "high"),
])
def test_file_auto_run_risk_uses_path_and_arguments(tmp_path, path, extra, expected_risk):
    tool = WorkspaceTextTool(tmp_path / "workspace")
    arguments = {"path": path, "content": "说明", **extra}
    planner = Planner([{"tool": tool.name, "arguments": arguments}])
    engine = AgentEngine(planner, TaskStore(tmp_path / "tasks.sqlite3"), [tool])
    app.dependency_overrides[get_agent_engine] = lambda: engine
    try:
        response = authorized_client(app).post(
            "/api/tasks", json={"goal": "创建说明", "auto_run_safe": True}
        )
        assert response.status_code == 201
        task = response.json()
        assert task["steps"][0]["risk_level"] == expected_risk
        assert task["state"] == ("complete" if expected_risk == "low" else "waiting_approval")
        assert (tmp_path / "workspace" / path).exists() == (expected_risk == "low")
    finally:
        app.dependency_overrides.clear()


def test_existing_file_and_high_impact_goal_never_auto_run(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_text("旧内容", encoding="utf-8")
    tool = WorkspaceTextTool(workspace)
    planner = Planner([{"tool": tool.name, "arguments": {"path": "notes.md", "content": "新内容"}}])
    engine = AgentEngine(planner, TaskStore(tmp_path / "tasks.sqlite3"), [tool])
    existing = asyncio.run(engine.plan("创建说明"))
    assert existing.steps[0].risk_level == "review"
    assert not engine.is_low_risk(existing)
    assert (workspace / "notes.md").read_text(encoding="utf-8") == "旧内容"

    planner.steps[0]["arguments"]["path"] = "new.md"
    sensitive = asyncio.run(engine.plan("创建说明并发布"))
    assert sensitive.steps[0].risk_level == "high"
    assert not engine.is_low_risk(sensitive)


def test_browser_input_requires_review_and_shows_destination(tmp_path):
    class BrowserTool(Tool):
        name = "browser.fill"

    tool = BrowserTool()
    planner = Planner([{
        "tool": tool.name,
        "arguments": {
            "url": "https://example.com/profile", "selector": "#display-name", "value": "艾拉",
        },
    }])
    engine = AgentEngine(planner, TaskStore(tmp_path / "tasks.sqlite3"), [tool])
    app.dependency_overrides[get_agent_engine] = lambda: engine
    try:
        result = authorized_client(app).post(
            "/api/tasks", json={"goal": "填写昵称", "auto_run_safe": True}
        )
        step = result.json()["steps"][0]
        assert result.json()["state"] == "waiting_approval"
        assert step["risk_level"] == "review"
        assert step["destination"] == "example.com"
        assert step["arguments"]["value"] == "艾拉"
        assert tool.actions == []

        planner.steps[0]["arguments"]["selector"] = "#password"
        sensitive = asyncio.run(engine.plan("填写密码"))
        assert sensitive.steps[0].risk_level == "high"
    finally:
        app.dependency_overrides.clear()


def test_opening_webpage_also_needs_review(tmp_path):
    class BrowserTool(Tool):
        name = "browser.open_page"

    tool = BrowserTool()
    planner = Planner([{
        "tool": tool.name, "arguments": {"url": "https://example.com/account"},
    }])
    engine = AgentEngine(planner, TaskStore(tmp_path / "tasks.sqlite3"), [tool])
    task = asyncio.run(engine.plan("打开账户页面"))
    assert task.steps[0].risk_level == "review"
    assert task.steps[0].destination == "example.com"
    assert not engine.is_low_risk(task)



def test_browser_read_requires_target_url_for_auto_run(tmp_path):
    class BrowserTool(Tool):
        name = "browser.read_page"

    tool = BrowserTool()
    planner = Planner([{
        "tool": tool.name, "arguments": {"selector": "body"},
    }])
    engine = AgentEngine(planner, TaskStore(tmp_path / "tasks.sqlite3"), [tool])
    unbound = asyncio.run(engine.plan("读取当前网页"))
    assert unbound.steps[0].risk_level == "review"
    assert not engine.is_low_risk(unbound)

    planner.steps = [{
        "tool": tool.name,
        "arguments": {"url": "https://example.com/page", "selector": "body"},
    }]
    bound = asyncio.run(engine.plan("读取当前网页"))
    assert bound.steps[0].risk_level == "review"
    assert bound.steps[0].destination == "example.com"
    assert not engine.is_low_risk(bound)


def test_approval_and_execution_bind_to_visible_plan(tmp_path):
    tool = WorkspaceTextTool(tmp_path / "workspace")
    planner = Planner([{"tool": tool.name, "arguments": {"path": "note.md", "content": "原文"}}])
    engine = AgentEngine(planner, TaskStore(tmp_path / "tasks.sqlite3"), [tool])
    app.dependency_overrides[get_agent_engine] = lambda: engine
    try:
        client = authorized_client(app)
        created = client.post("/api/tasks", json={"goal": "创建说明"}).json()
        task_id = created["id"]
        assert client.post(
            f"/api/tasks/{task_id}/approve", json={"plan_hash": "0" * 64}
        ).status_code == 409
        assert client.post(
            f"/api/tasks/{task_id}/approve", json={"plan_hash": created["plan_hash"]}
        ).status_code == 200
        altered = engine.store.get(task_id)
        altered.steps[0].arguments["content"] = "替换内容"
        engine.store.save(altered)
        assert client.post(f"/api/tasks/{task_id}/run").status_code == 409
        assert not (tmp_path / "workspace" / "note.md").exists()
    finally:
        app.dependency_overrides.clear()
