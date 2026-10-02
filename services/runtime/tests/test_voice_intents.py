import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from ella_runtime.modules.companion.store import CompanionStore
from ella_runtime.modules.companion.voice_intents import VoiceIntentProcessor
from ella_runtime.modules.models.contracts import ModelPurpose, ModelResponse, TokenUsage


class IntentModel:
    def __init__(self, result):
        self.result = result
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        return ModelResponse(
            text=json.dumps(self.result), provider="test", model="action",
            usage=TokenUsage(
                provider="test", model="action", purpose=ModelPurpose.ACTION,
                occurred_at=datetime.now(UTC),
            ),
        )


class TaskPlanner:
    def __init__(self):
        self.goals = []

    async def plan(self, goal):
        self.goals.append(goal)
        return SimpleNamespace(id="planned-task")


def test_voice_request_creates_reviewable_task_but_ignores_chat(tmp_path):
    model = IntentModel({"kind": "task"})
    companion = CompanionStore(tmp_path / "companion.sqlite3")
    planner = TaskPlanner()
    processor = VoiceIntentProcessor(model, companion, planner)

    assert asyncio.run(processor.process("今天玩了星露谷，好开心")) is None
    assert not model.requests
    result = asyncio.run(processor.process("帮我打开网页查一下天气"))
    assert result == {"kind": "task", "id": "planned-task"}
    assert planner.goals == ["帮我打开网页查一下天气"]
    assert asyncio.run(processor.process("做个表格记录开销")) == {
        "kind": "task", "id": "planned-task"
    }
    assert model.requests[0].purpose == ModelPurpose.ACTION
    assert companion.poll_events(now=datetime(2026, 9, 23, 9, tzinfo=UTC))[0]["type"] == "activity"


def test_voice_reminder_requires_explicit_request_and_future_zoned_time(tmp_path):
    future = datetime.now(UTC) + timedelta(hours=2)
    model = IntentModel({"kind": "reminder", "title": "喝水", "due_at": future.isoformat()})
    companion = CompanionStore(tmp_path / "companion.sqlite3")
    processor = VoiceIntentProcessor(model, companion, TaskPlanner())

    assert asyncio.run(processor.process("帮我打开网页")) is None
    assert companion.list_reminders() == []
    result = asyncio.run(processor.process("提醒我两小时后喝水"))
    assert result["kind"] == "reminder"
    assert [item["title"] for item in companion.list_reminders()] == ["喝水"]

    model.result["due_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    assert asyncio.run(processor.process("提醒我昨天喝水")) is None
    assert len(companion.list_reminders()) == 1
