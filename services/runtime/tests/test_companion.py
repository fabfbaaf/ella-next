from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ella_runtime.api import app, get_companion_store
from ella_runtime.modules.companion.store import CompanionStore
from tests.api_client import authorized_client


def test_hunger_decays_and_feeding_restores_it(tmp_path):
    store = CompanionStore(tmp_path / "companion.sqlite3")
    later = datetime.now(UTC) + timedelta(hours=20)
    assert store.status(now=later)["hunger"] == 0
    fed = store.feed(now=later)
    assert fed["hunger"] == 30
    assert fed["hungry"] is False


def test_reminder_waits_for_quiet_hours_and_fires_only_once(tmp_path):
    store = CompanionStore(tmp_path / "companion.sqlite3")
    local = ZoneInfo("Asia/Shanghai")
    tomorrow = (datetime.now(local) + timedelta(days=1)).date()
    quiet = datetime.combine(tomorrow, time(23, 0), tzinfo=local)
    morning = quiet + timedelta(hours=9, minutes=1)
    reminder = store.add_reminder("喝水", quiet - timedelta(minutes=10))
    assert store.poll_events(now=quiet) == []
    assert store.list_notifications() == []
    first = store.poll_events(now=morning)
    assert [event["text"] for event in first if event["type"] == "reminder"] == ["喝水"]
    assert not any(event["type"] == "reminder" for event in store.poll_events(now=morning))
    assert store.list_reminders()[0]["id"] == reminder["id"]
    assert store.list_reminders()[0]["delivered_at"] is not None
    assert store.list_notifications()[0]["read_at"] is None


def test_companion_api_feeds_sets_quiet_hours_and_deletes_reminder(tmp_path):
    store = CompanionStore(tmp_path / "companion.sqlite3")
    app.dependency_overrides[get_companion_store] = lambda: store
    try:
        client = authorized_client(app)
        assert client.get("/api/companion/status").status_code == 200
        assert client.post("/api/companion/feed").json()["hunger"] <= 100
        assert (
            client.patch(
                "/api/companion/quiet-hours", json={"start": "23:00", "end": "07:00"}
            ).json()["quiet_start"]
            == "23:00"
        )
        assert (
            client.patch(
                "/api/companion/quiet-hours", json={"start": "invalid", "end": "07:00"}
            ).status_code
            == 422
        )
        added = client.post(
            "/api/reminders",
            json={
                "title": "测试提醒",
                "due_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
        assert added.status_code == 201
        identity = added.json()["id"]
        assert client.get("/api/reminders").json()[0]["id"] == identity
        assert client.delete(f"/api/reminders/{identity}").status_code == 204
    finally:
        app.dependency_overrides.clear()


def test_progress_commentary_is_deduplicated_and_rate_limited(tmp_path):
    store = CompanionStore(tmp_path / "companion.sqlite3")
    store.set_quiet_hours("00:00", "00:00")
    now = datetime.now(UTC)
    store.record_progress("task", "one", "complete", "任务搞定啦", now=now)
    first = store.poll_events(now=now)
    assert [event["text"] for event in first if event["type"] == "activity"] == [
        "任务搞定啦"
    ]
    store.record_progress("task", "one", "complete", "重复文案", now=now)
    store.record_progress("game", "minecraft", "move-1", "第一步", now=now)
    store.record_progress("game", "minecraft", "move-2", "第二步", now=now)
    assert not any(event["type"] == "activity" for event in store.poll_events(now=now))
    later = store.poll_events(now=now + timedelta(seconds=31))
    assert [event["text"] for event in later if event["type"] == "activity"] == ["第二步"]


def test_missed_reminder_stays_pending_then_is_recoverable_in_inbox(tmp_path):
    path = tmp_path / "companion.sqlite3"
    store = CompanionStore(path)
    store.set_quiet_hours("00:00", "00:00")
    now = datetime.now(UTC)
    reminder = store.add_reminder("该休息了", now - timedelta(hours=1))
    assert store.list_reminders()[0]["delivered_at"] is None
    first = store.poll_events(now=now)
    assert [event["id"] for event in first if event["type"] == "reminder"] == [reminder["id"]]
    second_window = CompanionStore(path)
    assert not any(event["type"] == "reminder" for event in second_window.poll_events(now=now))
    inbox = second_window.list_notifications()
    assert len(inbox) == 1
    assert inbox[0]["id"] == reminder["id"]
    assert inbox[0]["read_at"] is None
    assert second_window.mark_notification_read(reminder["id"], now=now)
    assert CompanionStore(path).list_notifications()[0]["read_at"] is not None
    assert store.list_reminders()[0]["read_at"] is not None
    assert store.delete_reminder(reminder["id"])
    assert store.list_notifications() == []


def test_notification_api_reads_history_without_polling_or_competing(tmp_path):
    store = CompanionStore(tmp_path / "companion.sqlite3")
    store.set_quiet_hours("00:00", "00:00")
    app.dependency_overrides[get_companion_store] = lambda: store
    try:
        client = authorized_client(app)
        created = client.post("/api/reminders", json={
            "title": "喝水", "due_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        }).json()
        assert client.get("/api/companion/notifications").json() == []
        assert client.get("/api/reminders").json()[0]["delivered_at"] is None
        events = client.post("/api/companion/poll").json()
        assert any(event["id"] == created["id"] for event in events)
        assert client.post("/api/companion/poll").json() == []
        inbox = client.get("/api/companion/notifications").json()
        assert inbox[0]["id"] == created["id"]
        assert inbox[0]["read_at"] is None
        assert client.post(f"/api/companion/notifications/{created['id']}/read").status_code == 204
        assert client.get("/api/companion/notifications").json()[0]["read_at"] is not None
    finally:
        app.dependency_overrides.clear()


def test_old_unread_notifications_remain_in_history(tmp_path):
    store = CompanionStore(tmp_path / "companion.sqlite3")
    store.set_quiet_hours("00:00", "00:00")
    start = datetime.now(UTC)
    for index in range(55):
        moment = start + timedelta(seconds=index * 31)
        store.record_progress("task", str(index), "done", f"进展 {index}", now=moment)
        store.poll_events(now=moment)
    all_items = store.list_notifications()
    assert len(all_items) == 55
    assert all_items[-1]["text"] == "进展 0"
    for item in all_items[:50]:
        assert store.mark_notification_read(item["id"])
    unread = [item for item in store.list_notifications() if item["read_at"] is None]
    assert [item["text"] for item in unread] == [f"进展 {index}" for index in range(4, -1, -1)]
