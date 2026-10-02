from ella_runtime.modules.memory.capture import MemoryCapture
from ella_runtime.modules.memory.contracts import MemoryKind, MemorySource
from ella_runtime.modules.memory.store import MemoryStore


def test_explicit_and_clear_preference_memories_have_sources(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    capture = MemoryCapture(store)
    assert capture.capture("请记住：我的猫叫团子", source_ref="chat:one")
    assert capture.capture("我喜欢星露谷物语", source_ref="chat:one")
    assert not capture.capture("我喜欢星露谷物语", source_ref="chat:one")
    assert not capture.capture("你喜欢星露谷吗？", source_ref="chat:one")
    assert not capture.capture("不要记住我的地址", source_ref="chat:one")
    records = store.list()
    assert len(records) == 2
    assert {record.kind for record in records} == {MemoryKind.FACT, MemoryKind.PREFERENCE}
    assert all(record.source_type == MemorySource.CONVERSATION for record in records)
    assert all(record.source_ref == "chat:one" for record in records)
    assert any(record.confidence == 0.75 for record in records)
