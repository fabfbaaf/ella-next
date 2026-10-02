import asyncio

import pytest

from ella_runtime.modules.agent.contracts import (
    StepState,
    TaskState,
    ToolEvidence,
    ToolPreconditionError,
)
from ella_runtime.modules.agent.engine import AgentEngine, AgentError
from ella_runtime.modules.agent.store import TaskStore
from ella_runtime.modules.applications import coding
from ella_runtime.modules.applications.coding import ReadProjectFileTool, ReplaceProjectTextTool
from tests.test_agent import Planner


class PreflightTool:
    name = 'checked.write'
    description = 'Validate then write'

    def __init__(self):
        self.calls = []

    async def execute(self, arguments, *, action_id):
        if arguments.get('invalid'):
            raise ToolPreconditionError('文件名参数无效')
        self.calls.append(action_id)
        return ToolEvidence(verified=True, details={'saved': arguments})

    async def reconcile(self, arguments, *, action_id):
        return None


def test_precondition_failure_is_known_and_generic_failure_remains_uncertain(tmp_path):
    safe = PreflightTool()

    class WrittenThenError(PreflightTool):
        async def execute(self, arguments, *, action_id):
            self.calls.append(action_id)
            raise ValueError('simulated failure after write')

    async def run():
        for index, tool in enumerate([safe, WrittenThenError()]):
            engine = AgentEngine(Planner([{'tool': tool.name, 'arguments': {'invalid': True}}]),
                                 TaskStore(tmp_path / f'{index}.sqlite3'), [tool])
            task = await engine.plan('保存内容')
            engine.approve(task.id, task.plan_hash)
            result = await engine.run(task.id)
            if index == 0:
                assert result.state == TaskState.FAILED
                assert result.steps[0].state == StepState.FAILED
                assert result.steps[0].evidence['side_effects_started'] is False
                assert '文件名参数无效' in result.error and '尚未执行' in result.error
                assert not tool.calls
            else:
                assert result.state == TaskState.NEEDS_RECONCILIATION
                assert result.steps[0].state == StepState.NEEDS_RECONCILIATION
                assert 'simulated failure after write' in result.error
                assert len(tool.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize('arguments', [
    {'path': '../outside.py'}, {'path': 'missing.py'}, {'path': 'non_utf8.py'},
])
def test_coding_read_failures_are_known_before_action(tmp_path, arguments):
    (tmp_path / 'non_utf8.py').write_bytes(b'\xff\xfe\x00')
    tool = ReadProjectFileTool(tmp_path)
    engine = AgentEngine(Planner([{'tool': tool.name, 'arguments': arguments}]),
                         TaskStore(tmp_path / 'tasks.sqlite3'), [tool])

    async def run():
        task = await engine.plan('读取文件')
        engine.approve(task.id, task.plan_hash)
        result = await engine.run(task.id)
        assert result.state == TaskState.FAILED
        assert result.steps[0].state == StepState.FAILED
        assert '尚未执行' in result.error

    asyncio.run(run())


def test_deleted_text_can_be_reconciled_after_lost_response_and_restart(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    source = project / 'app.py'
    source.write_bytes(b'keep\r\nREMOVE\r\nend\r\n')
    receipts = tmp_path / 'receipts.sqlite3'

    class InterruptedEdit(ReplaceProjectTextTool):
        async def execute(self, arguments, *, action_id):
            await super().execute(arguments, action_id=action_id)
            raise OSError('response lost after edit')

    tool = InterruptedEdit(project, receipt_path=receipts)
    arguments = {'path': 'app.py', 'old': 'REMOVE\r\n', 'new': ''}
    engine = AgentEngine(Planner([{'tool': tool.name, 'arguments': arguments}]),
                         TaskStore(tmp_path / 'tasks.sqlite3'), [tool])

    async def run():
        task = await engine.plan('删除指定文本')
        engine.approve(task.id, task.plan_hash)
        uncertain = await engine.run(task.id)
        assert uncertain.state == TaskState.NEEDS_RECONCILIATION
        assert source.read_bytes() == b'keep\r\nend\r\n'
        restarted = AgentEngine(object(), TaskStore(engine.store.path), [
            ReplaceProjectTextTool(project, receipt_path=receipts),
        ])
        checked = await restarted.reconcile(task.id)
        assert checked.state == TaskState.READY
        assert checked.steps[0].evidence['replayed'] is False
        assert checked.steps[0].evidence['after_sha256'] == checked.steps[0].evidence['read_back_sha256']
        assert (await restarted.run(task.id)).state == TaskState.COMPLETE
        assert source.read_bytes() == b'keep\r\nend\r\n'

    asyncio.run(run())


def test_edit_reconciliation_rejects_full_file_drift_and_wrong_receipt(tmp_path):
    source = tmp_path / 'app.py'
    source.write_text('head\nOLD\ntail\n', encoding='utf-8')
    tool = ReplaceProjectTextTool(tmp_path, receipt_path=tmp_path / 'receipt.sqlite3')
    arguments = {'path': 'app.py', 'old': 'OLD', 'new': 'NEW'}

    async def run():
        await tool.execute(arguments, action_id='edit')
        assert (await tool.reconcile(arguments, action_id='edit')).verified
        assert not (await tool.reconcile(arguments, action_id='other')).verified
        changed = {**arguments, 'new': 'NEW\n'}
        assert not (await tool.reconcile(changed, action_id='edit')).verified
        source.write_text('head\nNEW\ntail\nUNRELATED CHANGE\n', encoding='utf-8')
        assert not (await tool.reconcile(arguments, action_id='edit')).verified

    asyncio.run(run())


def test_edit_without_receipt_cannot_infer_success_or_replay(tmp_path):
    source = tmp_path / 'app.py'
    source.write_text('NEW\n', encoding='utf-8')
    tool = ReplaceProjectTextTool(tmp_path, receipt_path=tmp_path / 'receipt.sqlite3')
    arguments = {'path': 'app.py', 'old': 'OLD', 'new': 'NEW'}
    evidence = asyncio.run(tool.reconcile(arguments, action_id='unknown'))
    assert not evidence.verified
    assert evidence.details['replayed'] is False
    assert not tool.receipt_path.exists()


def test_edit_crash_before_replace_preserves_original_and_never_replays(tmp_path, monkeypatch):
    source = tmp_path / 'app.py'
    source.write_text('OLD\n', encoding='utf-8')
    tool = ReplaceProjectTextTool(tmp_path, receipt_path=tmp_path / 'receipt.sqlite3')
    arguments = {'path': 'app.py', 'old': 'OLD', 'new': 'NEW'}

    def failed_replace(*args):
        raise OSError('crash before replace')

    monkeypatch.setattr(coding.os, 'replace', failed_replace)
    with pytest.raises(OSError, match='crash'):
        asyncio.run(tool.execute(arguments, action_id='edit'))
    assert source.read_text(encoding='utf-8') == 'OLD\n'
    assert not (asyncio.run(tool.reconcile(arguments, action_id='edit'))).verified
    with pytest.raises(RuntimeError, match='不能再次执行'):
        asyncio.run(tool.execute(arguments, action_id='edit'))
    assert source.read_text(encoding='utf-8') == 'OLD\n'
    assert not list(tmp_path.glob('*.tmp'))


def test_failed_plan_can_be_revised_without_replaying_completed_work(tmp_path):
    tool = PreflightTool()
    plan = [
        {'tool': tool.name, 'arguments': {'value': 'first'}, 'reason': '第一步'},
        {'tool': tool.name, 'arguments': {'invalid': True}, 'reason': '第二步'},
    ]
    engine = AgentEngine(Planner(plan), TaskStore(tmp_path / 'tasks.sqlite3'), [tool])

    async def run():
        task = await engine.plan('保存两份内容')
        original_hash = task.plan_hash
        engine.approve(task.id, original_hash)
        failed = await engine.run(task.id)
        assert failed.state == TaskState.FAILED
        completed_before = failed.steps[0].model_dump()
        revised = engine.revise(task.id, failed.plan_hash, [
            plan[0], {'tool': tool.name, 'arguments': {'value': 'second'}, 'reason': '修正参数'},
        ])
        assert revised.state == TaskState.WAITING_APPROVAL
        assert revised.approved_plan_hash is None and revised.error is None
        assert revised.steps[0].model_dump() == completed_before
        assert revised.steps[1].id != failed.steps[1].id
        assert revised.steps[1].state == StepState.PENDING
        assert revised.steps[1].evidence == {}
        with pytest.raises(AgentError):
            engine.approve(task.id, original_hash)
        with pytest.raises(AgentError):
            await engine.run(task.id)
        restarted = AgentEngine(object(), TaskStore(engine.store.path), [tool])
        restarted.approve(task.id, revised.plan_hash)
        complete = await restarted.run(task.id)
        assert complete.state == TaskState.COMPLETE
        assert tool.calls == [completed_before['id'], revised.steps[1].id]

    asyncio.run(run())


@pytest.mark.parametrize('change', ['completed', 'reason', 'drop', 'stale'])
def test_revision_rejects_completed_edits_and_stale_plan(tmp_path, change):
    tool = PreflightTool()
    plan = [{'tool': tool.name, 'arguments': {'value': 'first'}, 'reason': '第一步'},
            {'tool': tool.name, 'arguments': {'invalid': True}, 'reason': '第二步'}]
    engine = AgentEngine(Planner(plan), TaskStore(tmp_path / 'tasks.sqlite3'), [tool])

    async def run():
        task = await engine.plan('操作')
        engine.approve(task.id, task.plan_hash)
        failed = await engine.run(task.id)
        revised = [{'tool': item['tool'], 'arguments': dict(item['arguments']), 'reason': item['reason']} for item in plan]
        plan_hash = failed.plan_hash
        if change == 'completed':
            revised[0]['arguments']['value'] = 'altered'
        elif change == 'reason':
            revised[0]['reason'] = 'altered'
        elif change == 'drop':
            revised = revised[1:]
        else:
            plan_hash = '0' * 64
        with pytest.raises(AgentError):
            engine.revise(task.id, plan_hash, revised)
        assert engine.store.get(task.id).model_dump() == failed.model_dump()

    asyncio.run(run())


@pytest.mark.parametrize('state', [TaskState.RUNNING, TaskState.NEEDS_RECONCILIATION, TaskState.COMPLETE])
def test_revision_cannot_replay_unknown_running_or_completed_task(tmp_path, state):
    tool = PreflightTool()
    plan = [{'tool': tool.name, 'arguments': {}}]
    engine = AgentEngine(Planner(plan), TaskStore(tmp_path / 'tasks.sqlite3'), [tool])
    task = asyncio.run(engine.plan('操作'))
    task.state = state
    engine.store.save(task)
    with pytest.raises(AgentError):
        engine.revise(task.id, task.plan_hash, plan)


def test_store_rejects_revision_after_an_execution_claim(tmp_path):
    tool = PreflightTool()
    engine = AgentEngine(Planner([{'tool': tool.name, 'arguments': {}}]),
                         TaskStore(tmp_path / 'tasks.sqlite3'), [tool])
    task = asyncio.run(engine.plan('操作'))
    engine.approve(task.id, task.plan_hash)
    old_snapshot = engine.store.get(task.id)
    engine.store.claim_ready(task.id)
    old_snapshot.state = TaskState.WAITING_APPROVAL
    old_snapshot.approved_plan_hash = None
    with pytest.raises(ValueError, match='不能修改计划'):
        engine.store.replace_plan(old_snapshot, old_snapshot.plan_hash)
    assert engine.store.get(task.id).state == TaskState.RUNNING


def test_reconcile_read_error_keeps_unknown_reason_without_replay(tmp_path):
    class UncertainTool(PreflightTool):
        async def execute(self, arguments, *, action_id):
            raise OSError('lost response')

        async def reconcile(self, arguments, *, action_id):
            raise OSError('file inaccessible')

    tool = UncertainTool()
    engine = AgentEngine(Planner([{'tool': tool.name, 'arguments': {}}]),
                         TaskStore(tmp_path / 'tasks.sqlite3'), [tool])

    async def run():
        task = await engine.plan('操作')
        engine.approve(task.id, task.plan_hash)
        await engine.run(task.id)
        checked = await engine.reconcile(task.id)
        assert checked.state == TaskState.NEEDS_RECONCILIATION
        assert 'file inaccessible' in checked.error
        assert not tool.calls

    asyncio.run(run())


def test_running_task_check_is_not_limited_by_recent_history(tmp_path):
    from datetime import UTC, datetime, timedelta

    from ella_runtime.modules.agent.contracts import AgentTask, TaskStep

    store = TaskStore(tmp_path / "busy.sqlite3")
    now = datetime.now(UTC)
    active = AgentTask(id="active-old", goal="长任务", created_at=now, updated_at=now,
                       state=TaskState.RUNNING, steps=[TaskStep(id="step", tool="test")])
    store.save(active)
    for index in range(101):
        store.save(active.model_copy(update={"id": f"newer-{index}",
                    "updated_at": now + timedelta(seconds=index + 1), "state": TaskState.COMPLETE}))
    assert not any(task.state == TaskState.RUNNING for task in store.list())
    assert store.has_running()
    active.state = TaskState.COMPLETE
    store.save(active)
    assert not store.has_running()
