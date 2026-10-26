"""Уведомления о записи: тексты, таймеры и планировщик."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from app import database as db
from app import main as mainmod


def _training(**overrides):
    now = int(time.time())
    data = {
        "id": 1,
        "training_type": "Группа",
        "level": "Уверенный любитель (С)",
        "start_at": now + 3 * 3600,
        "duration_min": 60,
        "capacity": 6,
        "created_by": 123,
        "deleted_at": None,
    }
    data.update(overrides)
    return data


def _person(user_id=555, status="booked", first_name="Иван"):
    return {
        "telegram_id": user_id,
        "first_name": first_name,
        "status": status,
        "display_mode": "name",
        "avatar_file_id": None,
    }


class FakeBot:
    def __init__(self):
        self.sent = []
        self.deleted = []
        self.edits = []
        self.fail_send = False
        self.delete_error = ""
        self.edit_error = ""
        self._next_id = 100

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail_send:
            raise RuntimeError("timeout")
        self._next_id += 1
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, **kwargs):
        if self.edit_error:
            raise RuntimeError(self.edit_error)
        self.edits.append(kwargs)

    async def delete_message(self, chat_id, message_id):
        if self.delete_error:
            raise RuntimeError(self.delete_error)
        self.deleted.append((chat_id, message_id))


@pytest.fixture()
def bot(monkeypatch):
    fake = FakeBot()
    monkeypatch.setattr(mainmod.app.state, "bot", fake, raising=False)

    async def allowed(*args):
        return True

    monkeypatch.setattr(mainmod, "_notifications_allowed", allowed)
    return fake


def test_send_new_event_message_success_and_failure(bot, monkeypatch):
    saved = []

    async def save(*args, **kwargs):
        saved.append((args, kwargs))

    monkeypatch.setattr(db, "save_event_notification", save)

    message_id = asyncio.run(
        mainmod._send_new_event_message(
            "training_booked", 1, 555, "text",
            delete_at=123, phase="final", sent_at=100, final_at=None,
        )
    )
    assert message_id == 101
    assert saved and saved[0][1]["delete_at"] == 123
    assert saved[0][1]["phase"] == "final"

    bot.fail_send = True
    assert asyncio.run(
        mainmod._send_new_event_message(
            "training_booked", 1, 555, "text",
            delete_at=123, phase="final", sent_at=100,
        )
    ) == 0
    assert len(saved) == 1

    # Ветка «состав пуст».
    text = mainmod._training_booked_message_text(
        _training(), [], "ilka", "final"
    )
    assert "Пока никто не записан" in text
    assert "<i>" in text


def test_send_final_enrollment_branches(bot, monkeypatch):
    training = _training()
    row = {
        "id": 10,
        "kind": "training_booked",
        "entity_id": 1,
        "chat_id": 555,
        "user_id": 555,
        "message_id": 77,
        "phase": "preliminary",
        "final_at": 1,
    }
    saved = []
    deleted_rows = []

    async def save(*args, **kwargs):
        saved.append((args, kwargs))

    async def delete_row(row_id):
        deleted_rows.append(row_id)

    monkeypatch.setattr(db, "save_event_notification", save)
    monkeypatch.setattr(db, "delete_event_notification_by_id", delete_row)

    assert asyncio.run(
        mainmod._send_final_enrollment(
            training, row, [_person()], "ilka", 1000
        )
    ) is True
    assert bot.sent and bot.deleted == [(555, 77)]
    assert saved and saved[0][1]["phase"] == "final"

    async def denied(*args):
        return False

    monkeypatch.setattr(mainmod, "_notifications_allowed", denied)
    assert asyncio.run(
        mainmod._send_final_enrollment(
            training, row, [_person()], "ilka", 1000
        )
    ) is False
    assert deleted_rows == [10]

    async def allowed(*args):
        return True

    monkeypatch.setattr(mainmod, "_notifications_allowed", allowed)
    bot.fail_send = True
    assert asyncio.run(
        mainmod._send_final_enrollment(
            training, row, [_person()], "ilka", 1000
        )
    ) is False

    # Ошибка удаления старого сообщения не мешает финальной отправке.
    bot.fail_send = False
    bot.delete_error = "message cannot be deleted"
    assert asyncio.run(
        mainmod._send_final_enrollment(
            training, row, [_person()], "ilka", 1000
        )
    ) is True


def test_ensure_training_status_message_transitions(bot, monkeypatch):
    training = _training()
    now = int(time.time())
    rows = []
    updated = []
    sent = []
    final_calls = []
    deleted = []

    async def list_for_player(*args):
        return rows

    async def update_text(*args):
        updated.append(args)

    async def send_new(*args, **kwargs):
        sent.append((args, kwargs))
        return 1

    async def delete_row(row):
        deleted.append(row["id"])

    async def final(*args):
        final_calls.append(args)
        return True

    monkeypatch.setattr(db, "list_event_notifications_for_player", list_for_player)
    monkeypatch.setattr(db, "update_event_notification_text", update_text)
    monkeypatch.setattr(mainmod, "_send_new_event_message", send_new)
    monkeypatch.setattr(mainmod, "_delete_notification_row", delete_row)
    monkeypatch.setattr(mainmod, "_send_final_enrollment", final)

    # Нет записи, занятие близко — сразу финальное.
    asyncio.run(
        mainmod._ensure_training_status_message(
            training, 555, "booked", [_person()], "ilka", now
        )
    )
    assert sent and sent[-1][1]["phase"] == "final"

    # Нет записи, занятие далеко — предварительное.
    far = _training(start_at=now + 10 * 86400)
    asyncio.run(
        mainmod._ensure_training_status_message(
            far, 555, "booked", [_person()], "ilka", now
        )
    )
    assert sent[-1][1]["phase"] == "preliminary"
    assert isinstance(sent[-1][1]["final_at"], int)

    # Существующее финальное — только обновление текста.
    rows = [{
        "kind": "training_booked", "phase": "final", "final_at": None,
        "message_id": 5,
    }]
    asyncio.run(
        mainmod._ensure_training_status_message(
            training, 555, "booked", [_person()], "ilka", now
        )
    )
    assert updated

    # Предварительное пора заменить финальным.
    rows = [{
        "kind": "training_booked", "phase": "preliminary",
        "final_at": now - 1, "message_id": 5,
    }]
    asyncio.run(
        mainmod._ensure_training_status_message(
            training, 555, "booked", [_person()], "ilka", now
        )
    )
    assert final_calls

    # Предварительное уже удалено, финальное ещё не пора — ничего.
    rows = [{
        "kind": "training_booked", "phase": "preliminary",
        "final_at": now + 100, "message_id": 0,
    }]
    before = len(sent)
    asyncio.run(
        mainmod._ensure_training_status_message(
            training, 555, "booked", [_person()], "ilka", now
        )
    )
    assert len(sent) == before

    # Уведомления выключены — новое сообщение не отправляем.
    rows = []
    async def denied(*args):
        return False

    monkeypatch.setattr(mainmod, "_notifications_allowed", denied)
    before = len(sent)
    asyncio.run(
        mainmod._ensure_training_status_message(
            training, 555, "booked", [_person()], "ilka", now
        )
    )
    assert len(sent) == before
    asyncio.run(
        mainmod._ensure_training_status_message(
            training, 555, "waitlist", [_person()], "ilka", now
        )
    )
    asyncio.run(
        mainmod._ensure_training_status_message(
            training, 555, "rejected", [_person()], "ilka", now
        )
    )
    assert len(sent) == before

    # Занятие уже началось — статусные сообщения только чистим.
    rows = [{"id": 11, "kind": "training_booked", "message_id": 11}]
    deleted.clear()
    started = _training(start_at=now - 1)
    asyncio.run(
        mainmod._ensure_training_status_message(
            started, 555, "booked", [_person()], "ilka", now
        )
    )
    assert deleted == [11]


def test_ensure_status_waitlist_and_rejected(bot, monkeypatch):
    training = _training()
    now = int(time.time())
    rows = []
    sent = []
    deleted = []

    async def list_for_player(*args):
        return rows

    async def send_new(*args, **kwargs):
        sent.append((args, kwargs))
        return 1

    async def delete_row(row):
        deleted.append(row["id"])

    monkeypatch.setattr(db, "list_event_notifications_for_player", list_for_player)
    monkeypatch.setattr(mainmod, "_send_new_event_message", send_new)
    monkeypatch.setattr(mainmod, "_delete_notification_row", delete_row)

    # Waiting list: удаляем старые типы и отправляем новое.
    rows = [{"id": 1, "kind": "training_booked", "message_id": 1},
            {"id": 2, "kind": "training_rejected", "message_id": 2}]
    asyncio.run(
        mainmod._ensure_training_status_message(
            training, 555, "waitlist", [_person()], "ilka", now
        )
    )
    assert deleted == [1, 2]
    assert sent and sent[-1][0][0] == "training_waitlist"

    # Уже есть сообщение — повторно не шлём.
    rows = [{"id": 3, "kind": "training_waitlist", "message_id": 3}]
    before = len(sent)
    asyncio.run(
        mainmod._ensure_training_status_message(
            training, 555, "waitlist", [_person()], "ilka", now
        )
    )
    assert len(sent) == before

    # Сообщение было удалено — отправляем снова.
    rows = [{"id": 4, "kind": "training_waitlist", "message_id": 0}]
    asyncio.run(
        mainmod._ensure_training_status_message(
            training, 555, "waitlist", [_person()], "ilka", now
        )
    )
    assert len(sent) == before + 1

    # Rejected: удаляем booked/waitlist и отправляем отказ.
    deleted.clear()
    rows = [{"id": 5, "kind": "training_booked", "message_id": 5},
            {"id": 6, "kind": "training_waitlist", "message_id": 6}]
    asyncio.run(
        mainmod._ensure_training_status_message(
            training, 555, "rejected", [_person()], "ilka", now
        )
    )
    assert deleted == [5, 6]
    assert sent[-1][0][0] == "training_rejected"

    # Уже есть отказ — не дублируем.
    rows = [{"id": 7, "kind": "training_rejected", "message_id": 7}]
    before = len(sent)
    asyncio.run(
        mainmod._ensure_training_status_message(
            training, 555, "rejected", [_person()], "ilka", now
        )
    )
    assert len(sent) == before

    # Pending/cancelled/deleted очищают все статусные сообщения.
    deleted.clear()
    rows = [{"id": 8, "kind": "training_booked", "message_id": 8},
            {"id": 9, "kind": "training_waitlist", "message_id": 9},
            {"id": 10, "kind": "training_rejected", "message_id": 10}]
    asyncio.run(
        mainmod._ensure_training_status_message(
            training, 555, "cancelled", [_person()], "ilka", now
        )
    )
    assert deleted == [8, 9, 10]


def test_refresh_training_messages(bot, monkeypatch):
    training = _training()
    updated = []
    full = [
        _person(555, "booked"),
        _person(556, "cancelled"),
    ]

    async def get_full(_id):
        return full

    async def rows(_id):
        return [
            {"kind": "training", "user_id": 555, "message_id": 1},
            {"kind": "training", "user_id": 999, "message_id": 2},
            {"kind": "training_booked", "user_id": 555,
             "phase": "preliminary", "message_id": 3},
            {"kind": "training_booked", "user_id": 555,
             "phase": "preliminary", "message_id": 0},
            {"kind": "training_booked", "user_id": 556,
             "phase": "final", "message_id": 4},
            {"kind": "match", "user_id": 555, "message_id": 5},
        ]

    async def edit(row, text):
        updated.append((row["kind"], text))

    monkeypatch.setattr(db, "get_participants_full", get_full)
    monkeypatch.setattr(db, "list_event_notifications_for_training", rows)
    monkeypatch.setattr(mainmod, "_edit_notification_row", edit)

    asyncio.run(
        mainmod._refresh_training_messages(
            training, [_person(555, "booked")], "ilka"
        )
    )
    assert [item[0] for item in updated] == ["training", "training_booked"]


def test_delete_notification_row(bot, monkeypatch):
    deleted = []

    async def delete_row(row_id):
        deleted.append(row_id)

    monkeypatch.setattr(db, "delete_event_notification_by_id", delete_row)
    asyncio.run(
        mainmod._delete_notification_row(
            {"id": 1, "chat_id": 555, "message_id": 7}
        )
    )
    assert bot.deleted == [(555, 7)]
    assert deleted == [1]

    bot.delete_error = "message to delete not found"
    asyncio.run(
        mainmod._delete_notification_row(
            {"id": 2, "chat_id": 555, "message_id": 8}
        )
    )
    assert deleted == [1, 2]

    asyncio.run(
        mainmod._delete_notification_row(
            {"id": 3, "chat_id": 555, "message_id": 0}
        )
    )
    assert deleted == [1, 2, 3]


def test_edit_notification_row(bot, monkeypatch):
    updated = []

    async def update(*args):
        updated.append(args)

    monkeypatch.setattr(db, "update_event_notification_text", update)
    row = {
        "kind": "training",
        "entity_id": 1,
        "chat_id": 555,
        "user_id": 555,
        "message_id": 7,
    }
    asyncio.run(mainmod._edit_notification_row(row, "new"))
    assert bot.edits and bot.edits[-1]["text"] == "new"
    assert updated

    bot.edit_error = "message is not modified"
    asyncio.run(mainmod._edit_notification_row(row, "same"))
    assert len(updated) == 2

    bot.edit_error = "message not found"
    asyncio.run(mainmod._edit_notification_row(row, "lost"))
    assert len(updated) == 3

    no_message = {**row, "message_id": 0}
    asyncio.run(mainmod._edit_notification_row(no_message, "db only"))
    assert len(updated) == 4


def test_process_due_deletions(bot, monkeypatch):
    now = int(time.time())
    cleared = []
    deleted = []

    async def due(_now):
        return [
            {"id": 1, "kind": "training_booked", "phase": "preliminary",
             "final_at": now + 100, "chat_id": 555, "message_id": 1},
            {"id": 2, "kind": "training_booked", "phase": "preliminary",
             "final_at": now - 100, "chat_id": 555, "message_id": 2},
            {"id": 3, "kind": "training_booked", "phase": "final",
             "final_at": None, "chat_id": 555, "message_id": 3},
            {"id": 4, "kind": "training_waitlist", "phase": "status",
             "final_at": None, "chat_id": 555, "message_id": 0},
            {"id": 5, "kind": "training_rejected", "phase": "status",
             "final_at": None, "chat_id": 555, "message_id": 5},
        ]

    async def clear(row_id):
        cleared.append(row_id)

    async def delete_row(row_id):
        deleted.append(row_id)

    monkeypatch.setattr(db, "list_due_event_notifications", due)
    monkeypatch.setattr(db, "clear_event_notification_message", clear)
    monkeypatch.setattr(db, "delete_event_notification_by_id", delete_row)
    bot.delete_error = "timeout"
    asyncio.run(mainmod._process_due_deletions(now))
    # Транзиентная ошибка: сообщения не удаляем, но запись без message_id чистим.
    assert cleared == [] and deleted == [4]

    deleted.clear()
    bot.delete_error = "message not found"
    asyncio.run(mainmod._process_due_deletions(now))
    assert cleared == [1]
    assert deleted == [3, 4, 5]


def test_process_due_final_enrollments(bot, monkeypatch):
    now = int(time.time())
    deleted = []
    calls = []

    async def due(_now):
        return [
            {"id": 1, "entity_id": 1},
            {"id": 2, "entity_id": 2},
            {"id": 3, "entity_id": 3},
        ]

    async def get_training(tid):
        if tid == 1:
            return None
        if tid == 2:
            return {"id": 2, "deleted_at": 1}
        return _training(id=3)

    async def participants(_id):
        return [_person()]

    async def coach(_id):
        return "ilka"

    async def delete_row(row_id):
        deleted.append(row_id)

    async def final(*args):
        calls.append(args)
        return True

    monkeypatch.setattr(db, "list_due_final_enrollments", due)
    monkeypatch.setattr(db, "get_training", get_training)
    monkeypatch.setattr(db, "get_participants", participants)
    monkeypatch.setattr(db, "delete_event_notification_by_id", delete_row)
    monkeypatch.setattr(mainmod, "_training_coach_name", coach)
    monkeypatch.setattr(mainmod, "_send_final_enrollment", final)

    asyncio.run(mainmod._process_due_final_enrollments(now))
    assert deleted == [1, 2]
    assert calls and calls[0][1]["entity_id"] == 3


def test_cleanup_stale_preliminary(monkeypatch):
    deleted = []

    async def stale(_now):
        return [{"id": 1}, {"id": 2}]

    async def delete_row(row):
        deleted.append(row["id"])

    monkeypatch.setattr(db, "list_stale_preliminary_enrollments", stale)
    monkeypatch.setattr(mainmod, "_delete_notification_row", delete_row)
    asyncio.run(mainmod._cleanup_stale_preliminary(int(time.time())))
    assert deleted == [1, 2]


def test_transient_delete_error():
    assert mainmod._is_transient_delete_error(RuntimeError("timeout"))
    assert mainmod._is_transient_delete_error(RuntimeError("too many requests"))
    assert not mainmod._is_transient_delete_error(
        RuntimeError("message to delete not found")
    )


def test_notifications_loop_cancellation_and_error(monkeypatch):
    async def noop(now):
        return None

    monkeypatch.setattr(mainmod, "_process_due_final_enrollments", noop)
    monkeypatch.setattr(mainmod, "_cleanup_stale_preliminary", noop)

    async def error_scenario():
        entered = asyncio.Event()

        async def boom(now):
            entered.set()
            raise RuntimeError("boom")

        monkeypatch.setattr(mainmod, "_process_due_deletions", boom)
        task = asyncio.create_task(mainmod._training_notifications_loop(0))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(error_scenario())

    async def cancel_scenario():
        entered = asyncio.Event()

        async def slow(now):
            entered.set()
            await asyncio.sleep(10)

        monkeypatch.setattr(mainmod, "_process_due_deletions", slow)
        task = asyncio.create_task(mainmod._training_notifications_loop(0))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_scenario())


def test_notify_training_restored(monkeypatch):
    training = _training()
    ensured = []
    refreshed = []
    notified = []

    async def get_training(_id):
        return training

    async def participants(_id):
        return [_person(555, "booked"), _person(556, "waitlist")]

    async def coach(_id):
        return "ilka"

    async def notify(*args):
        notified.append(args)

    async def ensure(*args):
        ensured.append(args)

    async def refresh(*args):
        refreshed.append(args)

    monkeypatch.setattr(db, "get_training", get_training)
    monkeypatch.setattr(db, "get_participants", participants)
    monkeypatch.setattr(mainmod, "_training_coach_name", coach)
    monkeypatch.setattr(mainmod, "_notify_training_event", notify)
    monkeypatch.setattr(mainmod, "_ensure_training_status_message", ensure)
    monkeypatch.setattr(mainmod, "_refresh_training_messages", refresh)

    asyncio.run(mainmod._notify_training_restored(training))
    assert notified
    assert len(ensured) == 1
    assert ensured[0][1] == 555
    assert refreshed


def test_clear_event_notification_message_db(tmp_path):
    async def scenario():
        await db.connect(str(tmp_path / "notifications.db"))
        try:
            await db.save_event_notification(
                "training_booked", 1, 555, 555, 10, "text",
                delete_at=123, phase="preliminary",
                sent_at=100, final_at=456,
            )
            row = await db.get_event_notification(
                "training_booked", 1, 555, 555
            )
            assert row is not None
            await db.clear_event_notification_message(int(row["id"]))
            row = await db.get_event_notification(
                "training_booked", 1, 555, 555
            )
            assert row is not None
            assert row["message_id"] == 0
            assert row["delete_at"] is None
            assert row["phase"] == "preliminary"
            assert row["final_at"] == 456
        finally:
            await db.close()

    asyncio.run(scenario())


def test_event_notification_migration_adds_columns(tmp_path):
    import sqlite3

    path = tmp_path / "legacy-notifications.db"
    con = sqlite3.connect(path)
    try:
        con.executescript(db.SCHEMA)
        con.execute("DROP INDEX IF EXISTS idx_event_notifications_delete")
        for column in ("delete_at", "phase", "sent_at", "final_at"):
            con.execute(
                f"ALTER TABLE event_notifications DROP COLUMN {column}"
            )
        con.commit()
    finally:
        con.close()

    async def scenario():
        await db.connect(str(path))
        try:
            async with db._db().execute(
                "PRAGMA table_info(event_notifications)"
            ) as cur:
                columns = {row[1] for row in await cur.fetchall()}
            assert {"delete_at", "phase", "sent_at", "final_at"} <= columns
        finally:
            await db.close()

    asyncio.run(scenario())
