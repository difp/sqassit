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
