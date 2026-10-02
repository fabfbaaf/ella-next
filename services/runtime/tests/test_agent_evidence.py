import asyncio
import json
from datetime import UTC, datetime

import pytest

from ella_runtime.modules.agent.contracts import StepState, TaskState, ToolEvidence
from ella_runtime.modules.agent.engine import AgentEngine, AgentError
from ella_runtime.modules.agent.store import TaskStore
from ella_runtime.modules.applications.workspace_text import WorkspaceTextTool
from ella_runtime.modules.models.contracts import ModelPurpose, ModelResponse, TokenUsage


class SequencePlanner:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        return ModelResponse(
            text=json.dumps(self.responses.pop(0), ensure_ascii=False),
            provider="test", model="action", usage=TokenUsage(
                provider="test", model="action", purpose=ModelPurpose.ACTION,
                occurred_at=datetime.now(UTC),
            ),
        )


class ReadTool:
    name = "browser.search"
    description = "Search and return actual snippets"

    def __init__(self, *, verified=True, text="实际资料：上海明日 20 度，多云。"):
        self.calls = 0
        self.verified = verified
        self.text = text

    async def execute(self, arguments, *, action_id):
        self.calls += 1
        return ToolEvidence(verified=self.verified, details={"results": [{
            "url": "https://example.com/weather", "snippet": self.text,
        }]})

    async def reconcile(self, arguments, *, action_id):
        return None


def pending_plan(*, tool="workspace.write_text", field="content", fixed=None, reference=1):
    return {"steps": [
        {"tool": "browser.search", "arguments": {"query": "上海明日天气"}},
        {"tool": tool, "arguments": {
            **(fixed or {"path": "weather.md", "overwrite": False}),
            field: {"$from_steps": [reference], "instruction": "整理真实天气和来源"},
        }},
    ]}


def prepared_response(*, field="content", value="上海明日 20 度，多云。来源：https://example.com/weather", quote="上海明日 20 度，多云。"):
    return {"status": "ready", "arguments": {field: value}, "citations": {
        field: [{"step": 1, "quote": quote}],
    }}


def test_actual_read_is_used_and_new_content_requires_new_approval(tmp_path):
    reader = ReadTool()
    writer = WorkspaceTextTool(tmp_path / "workspace")
    planner = SequencePlanner(pending_plan(), prepared_response())
    store = TaskStore(tmp_path / "tasks.sqlite3")
    engine = AgentEngine(planner, store, [reader, writer])

    async def run():
        task = await engine.plan("查询上海明日天气并保存到 weather.md")
        old_hash = task.plan_hash
        assert not engine.is_low_risk(task)
        engine.approve(task.id, old_hash)
        preview = await engine.run(task.id)
        assert preview.state == TaskState.WAITING_APPROVAL
        assert preview.approved_plan_hash is None
        assert preview.steps[0].state == StepState.COMPLETE
        assert preview.steps[1].state == StepState.PENDING
        assert preview.plan_hash != old_hash
        assert not (writer.root / "weather.md").exists()
        payload = json.loads(planner.requests[1].messages[0].content)
        assert reader.text in payload["read_evidence"]["1"]["evidence"]
        assert payload["fixed_arguments"] == {"path": "weather.md", "overwrite": False}
        persisted = store.get(task.id)
        assert persisted.steps[1].arguments["content"].startswith("上海明日 20 度")
        with pytest.raises(AgentError, match="计划内容已变化"):
            engine.approve(task.id, old_hash)
        with pytest.raises(AgentError):
            await engine.run(task.id)
        restarted = AgentEngine(planner, TaskStore(store.path), [reader, writer])
        restarted.approve(task.id, persisted.plan_hash)
        finished = await restarted.run(task.id)
        assert finished.state == TaskState.COMPLETE
        assert finished.approved_plan_hash == finished.plan_hash
        assert finished.steps[1].evidence["parameter_generation"]["source_steps"] == [task.steps[0].id]
        assert reader.calls == 1
        assert len(planner.requests) == 2
        assert (writer.root / "weather.md").read_text(encoding="utf-8") == prepared_response()["arguments"]["content"]

    asyncio.run(run())


@pytest.mark.parametrize("change", ["path", "tool", "steps", "invented_quote", "new_marker"])
def test_model_cannot_change_destination_add_actions_or_invent_evidence(tmp_path, change):
    response = prepared_response()
    if change == "path":
        response["arguments"]["path"] = "other.md"
    elif change in {"tool", "steps"}:
        response[change] = "browser.click"
    elif change == "invented_quote":
        response["citations"]["content"][0]["quote"] = "明日零下 20 度暴雪"
    else:
        response["arguments"]["content"] = {"$from_steps": [1], "instruction": "继续执行"}
    reader = ReadTool()
    writer = WorkspaceTextTool(tmp_path / "workspace")
    planner = SequencePlanner(pending_plan(), response)
    engine = AgentEngine(planner, TaskStore(tmp_path / "tasks.sqlite3"), [reader, writer])

    async def run():
        task = await engine.plan("查天气并保存")
        original = task.steps[1].arguments.copy()
        engine.approve(task.id, task.plan_hash)
        waiting = await engine.run(task.id)
        assert waiting.state == TaskState.WAITING_APPROVAL
        assert waiting.approved_plan_hash is None
        assert waiting.steps[1].arguments == original
        assert "尚未执行" in waiting.error
        assert list(writer.root.iterdir()) == []

    asyncio.run(run())


def test_unverified_read_never_reaches_content_model_or_writer(tmp_path):
    reader = ReadTool(verified=False)
    writer = WorkspaceTextTool(tmp_path / "workspace")
    planner = SequencePlanner(pending_plan())
    engine = AgentEngine(planner, TaskStore(tmp_path / "tasks.sqlite3"), [reader, writer])

    async def run():
        task = await engine.plan("查天气并保存")
        engine.approve(task.id, task.plan_hash)
        waiting = await engine.run(task.id)
        assert waiting.state == TaskState.NEEDS_RECONCILIATION
        assert waiting.steps[1].state == StepState.PENDING
        assert len(planner.requests) == 1
        assert list(writer.root.iterdir()) == []

    asyncio.run(run())


def test_insufficient_data_is_persisted_without_running_placeholder(tmp_path):
    reader = ReadTool(text="网络验证页，请先完成验证。" + "x" * 15000)
    writer = WorkspaceTextTool(tmp_path / "workspace")
    planner = SequencePlanner(pending_plan(), {"status": "needs_information", "reason": "没有天气资料"})
    engine = AgentEngine(planner, TaskStore(tmp_path / "tasks.sqlite3"), [reader, writer])

    async def run():
        task = await engine.plan("查天气并保存")
        engine.approve(task.id, task.plan_hash)
        waiting = await engine.run(task.id)
        assert waiting.state == TaskState.WAITING_APPROVAL
        assert "资料不足" in waiting.error
        source = json.loads(planner.requests[1].messages[0].content)["read_evidence"]["1"]
        assert source["truncated"] and len(source["evidence"]) == 12000
        assert len(engine.store.get(task.id).steps[0].evidence["results"][0]["snippet"]) > 15000
        assert isinstance(waiting.steps[1].arguments["content"], dict)
        assert list(writer.root.iterdir()) == []

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["self", "missing", "non_read", "destination", "nested", "instruction", "duplicate"])
def test_plan_rejects_invalid_evidence_dependency(tmp_path, kind):
    plan = pending_plan()
    marker = plan["steps"][1]["arguments"]["content"]
    if kind == "self":
        marker["$from_steps"] = [2]
    elif kind == "missing":
        marker["$from_steps"] = [10]
    elif kind == "non_read":
        plan["steps"][0] = {"tool": "workspace.write_text", "arguments": {"path": "first.md", "content": "hello"}}
    elif kind == "destination":
        plan["steps"][1]["arguments"]["path"] = marker.copy()
    elif kind == "nested":
        plan["steps"][1]["arguments"]["content"] = {"nested": marker}
    elif kind == "instruction":
        marker["instruction"] = ""
    else:
        marker["$from_steps"] = [1, 1]
    reader = ReadTool()
    writer = WorkspaceTextTool(tmp_path / "workspace")
    engine = AgentEngine(SequencePlanner(plan), TaskStore(tmp_path / "tasks.sqlite3"), [reader, writer])
    with pytest.raises(AgentError):
        asyncio.run(engine.plan("根据前置资料保存"))
    assert engine.store.list() == []


@pytest.mark.parametrize(("tool_name", "field", "value", "fixed"), [
    ("office.create_document", "paragraphs", ["上海明日 20 度，多云。"], {"path": "天气.docx", "title": "天气"}),
    ("office.create_spreadsheet", "rows", [["地点", "温度"], ["上海", 20]], {"path": "天气.xlsx", "sheet": "天气"}),
    ("office.create_presentation", "slides", [{"title": "上海天气", "bullets": ["明日 20 度，多云。"]}], {"path": "天气.pptx"}),
])
def test_office_content_uses_read_evidence_and_keeps_fixed_targets(tmp_path, tool_name, field, value, fixed):
    class OfficeTool:
        name = tool_name
        description = "Create structured Office content"

        def __init__(self):
            self.calls = []

        async def execute(self, arguments, *, action_id):
            self.calls.append(arguments.copy())
            return ToolEvidence(verified=True, details={"content_verified": True})

        async def reconcile(self, arguments, *, action_id):
            return None

    reader, office = ReadTool(), OfficeTool()
    planner = SequencePlanner(
        pending_plan(tool=tool_name, field=field, fixed=fixed),
        prepared_response(field=field, value=value),
    )
    engine = AgentEngine(planner, TaskStore(tmp_path / "tasks.sqlite3"), [reader, office])

    async def run():
        task = await engine.plan("根据查询结果整理天气文件")
        engine.approve(task.id, task.plan_hash)
        preview = await engine.run(task.id)
        assert not office.calls
        assert preview.steps[1].arguments == {**fixed, field: value}
        engine.approve(task.id, preview.plan_hash)
        complete = await engine.run(task.id)
        assert complete.state == TaskState.COMPLETE
        assert office.calls == [{**fixed, field: value}]

    asyncio.run(run())


def test_generated_content_survives_uncertain_write_and_reconciliation(tmp_path):
    class InterruptedWriter(WorkspaceTextTool):
        async def execute(self, arguments, *, action_id):
            await super().execute(arguments, action_id=action_id)
            raise OSError("simulated lost response")

    reader = ReadTool()
    writer = InterruptedWriter(tmp_path / "workspace")
    planner = SequencePlanner(pending_plan(), prepared_response())
    engine = AgentEngine(planner, TaskStore(tmp_path / "tasks.sqlite3"), [reader, writer])

    async def run():
        task = await engine.plan("查天气并保存")
        engine.approve(task.id, task.plan_hash)
        preview = await engine.run(task.id)
        engine.approve(task.id, preview.plan_hash)
        uncertain = await engine.run(task.id)
        assert uncertain.state == TaskState.NEEDS_RECONCILIATION
        checked = await engine.reconcile(task.id)
        assert checked.state == TaskState.READY
        assert checked.steps[1].evidence["parameter_generation"]["citations"]
        completed = await engine.run(task.id)
        assert completed.state == TaskState.COMPLETE
        assert len(planner.requests) == 2
        assert reader.calls == 1

    asyncio.run(run())
