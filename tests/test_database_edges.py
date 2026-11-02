"""Тесты редких веток слоя SQLite и миграций."""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from app import database as db


def test_db_requires_initialization():
    asyncio.run(db.close())
    with pytest.raises(RuntimeError, match="не инициализирована"):
        db._db()


def test_migration_backfills_onboarding_for_existing_users(tmp_path):
    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    try:
        con.executescript(db.SCHEMA)
        con.execute(
            "INSERT INTO users (telegram_id, username, first_name, created_at) "
            "VALUES (42, 'old', 'Old', 1)"
        )
        for column in (
            "onboarding_seen",
            "notify_matches",
            "notify_trainings",
            "notify_messages",
        ):
            con.execute(f"ALTER TABLE users DROP COLUMN {column}")
        con.commit()
    finally:
        con.close()

    async def scenario() -> None:
        await db.connect(str(path))
        try:
            row = await db.get_user(42)
            assert row is not None
            assert row["onboarding_seen"] == 1
            assert row["notify_matches"] == 1
            assert row["notify_trainings"] == 1
            assert row["notify_messages"] == 1
        finally:
            await db.close()

    asyncio.run(scenario())


def test_database_helpers_and_queue_branches(tmp_path):
    async def scenario() -> None:
        await db.connect(str(tmp_path / "helpers.db"))
        try:
            await db.upsert_user(1, "u", "User", is_coach=False)
            await db.set_user_avatar(1, "file-id")
            await db.set_user_notes(1, "note")
            await db.set_notifications_enabled(1, False)
            assert (await db.find_user_by_username("@U"))["telegram_id"] == 1

            now = int(time.time())
            training_id = await db.create_training(
                title="Тест",
                training_type="Тест",
                start_at=now + 3600,
                capacity=2,
                duration_min=60,
                reminder_offset_min=60,
                reminder_enabled=True,
                created_by=1,
                level="",
            )
            await db.update_reminder_settings(
                training_id, enabled=True, offset_min=60
            )
            due = await db.trainings_due_for_reminder(now + 3599)
            assert any(row["id"] == training_id for row in due)
            await db.mark_reminder_sent(training_id)

            past_id = await db.create_training(
                title="Прошлая",
                training_type="Прошлая",
                start_at=now - 3600,
                capacity=2,
                duration_min=60,
                reminder_offset_min=60,
                reminder_enabled=False,
                created_by=1,
                level="",
            )
            history = await db.list_history_trainings(now, 1)
            assert any(row["id"] == past_id for row in history)

            assert await db.book_training(training_id, 1, 2) == "booked"
            participants = await db.get_participants(training_id)
            assert participants and participants[0]["telegram_id"] == 1

            # Отмена несуществующей записи не трогает очередь.
            assert await db.cancel_booking(training_id, 999) == ""
            assert await db.cancel_booking(training_id, 1) == ""
            assert await db.restore_booking(training_id, 1, 2) == "booked"
            assert await db.restore_booking(training_id, 1, 2) == "booked"

            # Место освободилось, но очередь уже есть: ручная запись
            # тренером встаёт в её конец, а не обгоняет ожидающих.
            queue_id = await db.create_training(
                title="Очередь",
                training_type="Очередь",
                start_at=now + 7200,
                capacity=1,
                duration_min=60,
                reminder_offset_min=60,
                reminder_enabled=False,
                created_by=1,
                level="",
            )
            assert await db.book_training(queue_id, 10, 1) == "booked"
            assert await db.book_training(queue_id, 11, 1) == "waitlist"
            assert (
                await db.book_training(
                    queue_id, 12, 2, respect_waitlist=True
                )
                == "waitlist"
            )

            await db.add_message(1, 123, "Привет")
            inbox = await db.list_inbox(123)
            assert inbox and inbox[0]["text"] == "Привет"

            await db.add_feedback(1, "bug", "Не работает кнопка", "Матч")
            feedback = await db.list_user_feedback(1)
            assert feedback and feedback[0]["kind"] == "bug"
            assert feedback[0]["page"] == "Матч"
            assert await db.notification_targets(training_id) == []
            assert await db.list_match_feed_recipients() == []
            with pytest.raises(ValueError, match="категория"):
                await db.notification_enabled(1, "unknown")

            # Общий тумблер выключен — категории тоже недоступны.
            assert await db.notification_enabled(1, "matches") is False
            await db.set_notifications_enabled(1, True)
            assert await db.notification_enabled(1, "matches") is True

            # Тематические тумблеры работают независимо и фильтруют рассылки.
            await db.set_player_profile(
                1,
                about="",
                display_mode="name",
                show_username=False,
                notify_matches=False,
                notify_trainings=False,
            )
            assert await db.notification_enabled(1, "matches") is False
            assert await db.notification_enabled(1, "trainings") is False
            assert await db.notification_targets(training_id) == []
            assert await db.list_match_feed_recipients() == []
            await db.set_player_profile(
                1,
                about="",
                display_mode="name",
                show_username=False,
                notify_matches=True,
                notify_trainings=True,
            )
            assert [row["telegram_id"] for row in await db.notification_targets(training_id)] == [1]
            assert [row["telegram_id"] for row in await db.list_match_feed_recipients()] == [1]

            await db.save_event_notification("training", 1, 123, 0, 55, "text")
            saved = await db.get_event_notification("training", 1, 123, 0)
            assert saved is not None
            assert saved["message_id"] == 55 and saved["text"] == "text"
            await db.save_event_notification("training", 1, 123, 0, 56, "new")
            saved = await db.get_event_notification("training", 1, 123, 0)
            assert saved is not None and saved["message_id"] == 56

            await db.purge_training(training_id)
            assert await db.get_training(training_id) is None
        finally:
            await db.close()

    asyncio.run(scenario())


def test_set_user_phone(tmp_path):
    async def scenario() -> None:
        await db.connect(str(tmp_path / "phone.db"))
        try:
            await db.upsert_user(1, "u", "User", is_coach=False)
            await db.set_user_phone(1, "+79991234567")
            row = await db.get_user(1)
            assert row is not None and row["phone"] == "+79991234567"
        finally:
            await db.close()

    asyncio.run(scenario())


def test_reminder_retries_only_failed_recipients(tmp_path):
    from app.bot import send_training_reminder

    class FakeBot:
        def __init__(self):
            self.sent = []
            self.fail = True

        async def send_message(self, user_id, text):
            if user_id == 2 and self.fail:
                raise RuntimeError("temporary")
            self.sent.append(user_id)

    async def scenario():
        await db.connect(str(tmp_path / "reminders.db"))
        try:
            for user_id in (1, 2):
                await db.upsert_user(user_id, None, "Player")
            tid = await db.create_training(
                title="Тест", training_type="Тест", start_at=int(time.time()) + 3600,
                duration_min=60, capacity=2, level="", reminder_enabled=True,
                reminder_offset_min=60, created_by=1,
            )
            for user_id in (1, 2):
                await db.book_training(tid, user_id, 2)
            bot = FakeBot()
            training = await db.get_training(tid)
            assert await send_training_reminder(bot, training) == (1, 2)
            bot.fail = False
            assert await send_training_reminder(bot, training) == (1, 1)
            assert bot.sent == [1, 2]
        finally:
            await db.close()

    asyncio.run(scenario())


def test_failed_updates_roll_back_as_a_unit(tmp_path):
    async def scenario():
        await db.connect(str(tmp_path / "rollback.db"))
        try:
            await db.upsert_user(1, None, "Player")
            conn = db._db()
            await conn.execute("INSERT INTO feedback (user_id, kind, text, created_at) VALUES (1, 'bug', 'text', 1)")
            await conn.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON users BEGIN SELECT RAISE(ABORT, 'test'); END")
            await conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                await db.delete_user_data(1)
            assert len(await db.list_user_feedback(1)) == 1
            await conn.execute("CREATE TRIGGER fail_reset BEFORE DELETE ON reminder_deliveries BEGIN SELECT RAISE(ABORT, 'test'); END")
            await conn.execute("INSERT INTO reminder_deliveries VALUES (1, 1)")
            await conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                await db.update_reminder_settings(1, enabled=True, offset_min=60)
        finally:
            await db.close()

    asyncio.run(scenario())


def test_notification_deadlines_and_expiration(tmp_path, monkeypatch):
    async def scenario():
        await db.connect(str(tmp_path / "notifications.db"))
        try:
            now = 1_900_000_000
            monkeypatch.setattr(db.time, "time", lambda: now)
            tid = await db.create_training(
                title="Тест", training_type="Тест", start_at=now + 3600,
                duration_min=60, capacity=2, level="", reminder_enabled=True,
                reminder_offset_min=60, created_by=1,
            )
            await db.save_event_notification("training", tid, 1, 1, 10, "one")
            record = await db.get_event_notification("training", tid, 1, 1)
            assert record["delete_at"] == now + 6 * 3600
            await db.save_event_notification("training", tid, 1, 1, 10, "updated")
            assert (await db.get_event_notification("training", tid, 1, 1))["delete_at"] == now + 6 * 3600
            await db.save_event_notification("chat", 2, 1, 1, 11, "message")
            assert (await db.get_event_notification("chat", 2, 1, 1))["delete_at"] == now + 86400
            await db.expire_event_notifications("chat", 2, 1)
            assert (await db.get_event_notification("chat", 2, 1, 1))["delete_at"] == now
            await db.save_event_notification("rules", 0, 1, 0, 12, "rules")
            assert (await db.get_event_notification("rules", 0, 1, 0))["delete_at"] is None
        finally:
            await db.close()

    asyncio.run(scenario())


def test_training_and_coach_updates_roll_back_on_failure(tmp_path):
    async def scenario():
        await db.connect(str(tmp_path / "training-updates.db"))
        try:
            await db.upsert_user(7, None, "Тренер", is_coach=True)
            await db.ensure_coach(7)
            conn = db._db()
            await conn.execute("CREATE TRIGGER fail_coach_update BEFORE UPDATE OF is_coach ON users BEGIN SELECT RAISE(ABORT, 'test'); END")
            await conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                await db.remove_coach(7)
            assert await db.is_coach_id(7)
            assert await db.update_training_details(999, "Несуществующая", 1) is False
            tid = await db.create_training(
                title="До", training_type="До", start_at=1000, capacity=2,
                duration_min=60, reminder_offset_min=60, reminder_enabled=True, created_by=7,
            )
            await conn.execute("CREATE TRIGGER fail_training_update BEFORE UPDATE ON trainings BEGIN SELECT RAISE(ABORT, 'test'); END")
            await conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                await db.update_training_details(tid, "После", 2000)
            assert (await db.get_training(tid))["training_type"] == "До"
        finally:
            await db.close()

    asyncio.run(scenario())
