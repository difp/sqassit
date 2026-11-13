"""Тесты редких веток слоя SQLite и миграций."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import closing

import pytest

from app import database as db


def test_tournament_outbox_survives_database_restart(tmp_path):
    async def scenario():
        path = str(tmp_path / 'outbox.db')
        await db.connect(path)
        assert await db.save_training_tournament(7, '{}', 0, True)
        await db.close()
        await db.connect(path)
        try:
            jobs = await db.list_tournament_notification_jobs()
            assert [(row['training_id'], row['revision']) for row in jobs] == [(7, 1)]
        finally:
            await db.close()
    asyncio.run(scenario())


def test_training_outbox_survives_restart_and_rolls_back_with_booking(tmp_path):
    async def scenario():
        path = str(tmp_path / 'training-outbox.db')
        await db.connect(path)
        await db._execute("CREATE TRIGGER fail_training_job BEFORE INSERT ON training_notification_jobs BEGIN SELECT RAISE(ABORT, 'queue failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match='queue failure'):
            await db.book_training(5, 555, 2, requires_approval=True)
        assert await db.get_booking(5, 555) is None
        await db._execute('DROP TRIGGER fail_training_job')
        assert await db.book_training(5, 555, 2, requires_approval=True) == 'pending'
        await db.close()
        await db.connect(path)
        try:
            jobs = await db.list_training_notification_jobs()
            assert [(row['kind'], row['training_id'], row['user_id']) for row in jobs] == [('request', 5, 555)]
        finally:
            await db.close()
    asyncio.run(scenario())


def test_training_outbox_migrates_legacy_booking_schema(tmp_path):
    path = tmp_path / 'legacy-training.db'
    with closing(sqlite3.connect(path)) as con, con:
        con.executescript(db.SCHEMA)
        for name in ('enqueue_training_request_insert', 'enqueue_training_request_update',
                     'enqueue_training_vacancy_update', 'enqueue_training_vacancy_delete'):
            con.execute(f'DROP TRIGGER {name}')
        con.execute('DROP TABLE training_notification_jobs')
        con.execute('ALTER TABLE bookings DROP COLUMN needs_approval')
        con.execute("INSERT INTO bookings(training_id,user_id,status,position,created_at) VALUES (5,555,'booked',0,1)")
    async def scenario():
        await db.connect(str(path))
        try:
            assert (await db.get_booking(5, 555))['status'] == 'booked'
            await db.cancel_booking(5, 555)
            jobs = await db.list_training_notification_jobs()
            assert [(row['kind'], row['user_id']) for row in jobs] == [('vacancy', 555)]
        finally:
            await db.close()
    asyncio.run(scenario())


def test_messages_client_id_migrates_legacy_schema(tmp_path):
    path = tmp_path / "legacy-messages.db"
    with closing(sqlite3.connect(path)) as con, con:
        con.executescript(db.SCHEMA)
        con.execute("ALTER TABLE messages DROP COLUMN client_id")
        con.execute(
            "INSERT INTO messages (from_user, to_user, text, created_at) "
            "VALUES (5, 123, 'старое', 1)"
        )

    async def scenario():
        await db.connect(str(path))
        try:
            first = await db.add_message_once(5, 123, "новое", "key-1")
            second = await db.add_message_once(5, 123, "новое", "key-1")
            assert first[1] is True and second[1] is False
            assert first[0] == second[0]
            page, has_more = await db.list_user_thread(5)
            assert [row["text"] for row in page] == ["старое", "новое"]
            assert has_more is False
        finally:
            await db.close()

    asyncio.run(scenario())


def test_add_message_once_recovers_from_insert_race(tmp_path, monkeypatch):
    path = tmp_path / "messages-race.db"

    async def scenario():
        await db.connect(str(path))
        try:
            first_id, created = await db.add_message_once(5, 123, "текст", "dup")
            assert created is True

            calls = {"count": 0}

            async def racing_add(*args, **kwargs):
                calls["count"] += 1
                raise sqlite3.IntegrityError("UNIQUE constraint failed")

            monkeypatch.setattr(db, "add_message", racing_add)
            second_id, created = await db.add_message_once(5, 123, "текст", "dup")
            assert (second_id, created) == (first_id, False)
            assert calls["count"] == 1

            # Неизвестный client_id: ошибка UNIQUE не прячется.
            with pytest.raises(sqlite3.IntegrityError):
                await db.add_message_once(5, 123, "текст", "unknown")
        finally:
            await db.close()

    asyncio.run(scenario())


def test_add_message_once_conflict_scopes_and_rollback(tmp_path):
    path = tmp_path / "messages-conflict.db"

    async def scenario():
        await db.connect(str(path))
        try:
            first_id, created = await db.add_message_once(5, 123, "первый", "key")
            assert created is True

            # Тот же ключ с другим текстом — ошибка, а не «тихий» успех.
            with pytest.raises(db.MessageConflict):
                await db.add_message_once(5, 123, "другой", "key")
            # После IntegrityError соединение не остаётся в транзакции.
            assert db._db().in_transaction is False

            retry = await db.add_message_once(5, 123, "первый", "key")
            assert retry == (first_id, False)

            # Ключ уникален в рамках диалога: другому получателю — можно.
            other_id, other_created = await db.add_message_once(5, 124, "другой", "key")
            assert other_created is True and other_id != first_id

            await db.mark_chat_read(123, 5, last_read_id=first_id - 1)
            assert await db.unread_by_peer(123) == {5: 1}
            await db.mark_chat_read(123, 5)
            assert await db.unread_by_peer(123) == {}
            # Отметка не откатывается назад.
            await db.mark_chat_read(123, 5, last_read_id=0)
            assert await db.unread_by_peer(123) == {}
        finally:
            await db.close()

    asyncio.run(scenario())



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
        con.execute(
            "INSERT INTO matches (title, start_at, created_by, created_at) VALUES (?, ?, ?, ?)",
            ('Существующий матч', int(time.time()) + 86400, 42, 1),
        )
        con.execute("ALTER TABLE matches DROP COLUMN published")
        con.execute("ALTER TABLE matches DROP COLUMN notify_all")
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
            matches = await db.list_upcoming_matches(int(time.time()), int(time.time()) + 172800)
            assert len(matches) == 1 and matches[0]["title"] == 'Существующий матч'
            assert matches[0]["published"] == 1 and matches[0]["notify_all"] == 0
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


def test_queue_priority_and_transaction_rollback(tmp_path, monkeypatch):
    async def scenario():
        await db.connect(str(tmp_path / "rollback.db"))
        try:
            tid = await db.create_training(
                title="Группа", training_type="Группа", start_at=int(time.time()) + 86400,
                capacity=1, duration_min=60, reminder_offset_min=60,
                reminder_enabled=False, created_by=1, level="",
            )
            assert await db.book_training(tid, 10, 1) == "booked"
            assert await db.book_training(tid, 11, 1, requires_approval=True) == "waitlist"
            # Заявка, сохранившаяся в старой заполненной группе.
            await db._execute("INSERT INTO bookings (training_id,user_id,status,created_at) VALUES (?,?,'pending',?)", (tid, 12, int(time.time())))
            # Одобрение переполненной заявки не должно превышать вместимость.
            assert await db.approve_booking(tid, 12) == "waitlist"
            row = await db.get_booking(tid, 12)
            assert row["position"] == 2 and not row["needs_approval"]
            # Отмена ожидающего не должна передвинуть очередь, пока мест нет.
            assert await db.cancel_booking(tid, 12) == ""
            assert (await db.get_booking(tid, 11))["status"] == "waitlist"

            async def failure(*args, **kwargs):
                raise RuntimeError("simulated failure")

            # При ошибке после обновления записи изменения откатываются целиком.
            with monkeypatch.context() as patch:
                patch.setattr(db, "_advance_training_waitlist", failure)
                with pytest.raises(RuntimeError, match="simulated"):
                    await db.cancel_booking(tid, 10)
                assert (await db.get_booking(tid, 10))["status"] == "booked"

            await db._execute("UPDATE bookings SET status='pending',position=0 WHERE training_id=? AND user_id=12", (tid,))
            with monkeypatch.context() as patch:
                patch.setattr(db, "_advance_training_waitlist", failure)
                with pytest.raises(RuntimeError, match="simulated"):
                    await db.reject_booking(tid, 12)
                assert (await db.get_booking(tid, 12))["status"] == "pending"

            with monkeypatch.context() as patch:
                patch.setattr(db, "_queue_pending_if_full", failure)
                with pytest.raises(RuntimeError, match="simulated"):
                    await db.reconcile_full_training_waitlists()
                assert (await db.get_booking(tid, 12))["status"] == "pending"

            await db.cancel_booking(tid, 10)
            # Место зарезервировано для 11, пока тренер принимает решение.
            with monkeypatch.context() as patch:
                patch.setattr(db, "_queue_pending_if_full", failure)
                with pytest.raises(RuntimeError, match="simulated"):
                    await db.approve_booking(tid, 11)
                assert (await db.get_booking(tid, 11))["status"] == "pending"
            await db.reject_booking(tid, 11)
            with monkeypatch.context() as patch:
                patch.setattr(db, "_queue_pending_if_full", failure)
                with pytest.raises(RuntimeError, match="simulated"):
                    await db.book_training(tid, 20, 1)
                assert await db.get_booking(tid, 20) is None
        finally:
            await db.close()

    asyncio.run(scenario())
