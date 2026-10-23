"""Тесты обработчиков бота без сети Telegram.

aiogram-объекты заменяются минимальными заглушками: проверяем логику,
текст сообщений и права доступа, а не транспорт.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiogram.types import InlineKeyboardMarkup

from app import bot as botmod
from app import database as db
from app.config import settings
from app.identity import animal_identity


@pytest.fixture(autouse=True)
def clear_pending_reply():
    botmod._pending_reply.clear()
    yield
    botmod._pending_reply.clear()


@pytest.fixture(autouse=True)
def notifications_on(monkeypatch):
    """Юнит-тесты бота не поднимают БД: по умолчанию уведомления разрешены."""
    async def enabled(telegram_id, category):
        return True

    monkeypatch.setattr(db, "notification_enabled", enabled)


def _message(user_id: int, text: str = "", first_name: str = "Игрок", username: str | None = None):
    answers: list[tuple[str, object]] = []
    sent: list[tuple[int, str, object]] = []

    async def answer(message, reply_markup=None):
        answers.append((message, reply_markup))

    async def send_message(chat_id, message, **kwargs):
        sent.append((chat_id, message, kwargs.get("reply_markup")))

    message = SimpleNamespace(
        from_user=SimpleNamespace(
            id=user_id, first_name=first_name, username=username
        ),
        text=text,
        bot=SimpleNamespace(send_message=send_message),
        answer=answer,
    )
    return message, answers, sent


def _callback(user_id: int, data: str):
    answers: list[tuple[object, object]] = []
    chat_answers: list[str] = []
    sent: list[tuple[int, str, object]] = []

    async def answer(message=None, show_alert=None):
        answers.append((message, show_alert))

    async def chat_answer(message, **kwargs):
        chat_answers.append(message)

    async def send_message(chat_id, message, **kwargs):
        sent.append((chat_id, message, kwargs.get("reply_markup")))

    message = SimpleNamespace(
        answer=chat_answer,
        bot=SimpleNamespace(send_message=send_message),
    )
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        data=data,
        answer=answer,
        message=message,
    ), answers, chat_answers, sent


def test_web_app_url_keeps_existing_query(monkeypatch):
    monkeypatch.setattr(
        botmod,
        "settings",
        replace(settings, webhook_base_url="https://example.test/app?x=1"),
    )
    assert botmod._web_app_url() == "https://example.test/app?x=1&v=32"


def test_web_app_url_falls_back_to_telegram(monkeypatch):
    monkeypatch.setattr(botmod, "settings", replace(settings, webhook_base_url=""))
    assert botmod._web_app_url() == "https://t.me"


def test_pending_reply_filter_removes_expired(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(botmod.time, "time", lambda: now[0])
    botmod._pending_reply[123] = (555, 1100.0)
    message = SimpleNamespace(from_user=SimpleNamespace(id=123))

    assert asyncio.run(botmod.PendingReplyFilter()(message)) is True
    now[0] = 1200.0
    assert asyncio.run(botmod.PendingReplyFilter()(message)) is False
    assert 123 not in botmod._pending_reply


def test_cmd_start_escapes_name_and_hides_id(monkeypatch):
    calls = {}

    async def fake_is_coach_id(telegram_id):
        return False

    async def fake_upsert_user(**kwargs):
        calls["user"] = kwargs

    monkeypatch.setattr(db, "is_coach_id", fake_is_coach_id)
    monkeypatch.setattr(db, "upsert_user", fake_upsert_user)
    monkeypatch.setattr(
        botmod,
        "settings",
        replace(settings, webhook_base_url="https://example.test"),
    )

    message, answers, _ = _message(
        1001, first_name="<Иван>", username="ivan"
    )
    message.bot = None
    asyncio.run(botmod.cmd_start(message))

    text, markup = answers[0]
    assert "&lt;Иван&gt;" in text
    assert "<Иван>" not in text
    assert "1001" not in text
    assert markup.inline_keyboard[0][0].web_app.url.endswith("?v=32")
    assert calls["user"]["telegram_id"] == 1001


def test_cb_reply_rejects_non_coach(monkeypatch):
    async def fake_is_coach_id(telegram_id):
        return False

    monkeypatch.setattr(db, "is_coach_id", fake_is_coach_id)
    callback, answers, chat_answers, _ = _callback(555, "reply:777")
    asyncio.run(botmod.cb_reply(callback))

    assert answers == [("Недоступно", True)]
    assert chat_answers == []
    assert not botmod._pending_reply


def test_cb_reply_arms_pending_reply_for_coach(monkeypatch):
    async def fake_is_coach_id(telegram_id):
        return True

    monkeypatch.setattr(db, "is_coach_id", fake_is_coach_id)
    callback, answers, chat_answers, _ = _callback(123, "reply:555")
    asyncio.run(botmod.cb_reply(callback))

    assert answers == [(None, None)]
    assert chat_answers and "Напишите ответ" in chat_answers[0]
    target, expires_at = botmod._pending_reply[123]
    assert target == 555
    assert expires_at > time.time()


def test_coach_reply_text_sends_escaped_message():
    botmod._pending_reply[123] = (555, time.time() + 600)
    message, answers, sent = _message(123, text="<b>ответ</b>")

    asyncio.run(botmod.coach_reply_text(message))

    assert sent[0][0] == 555
    assert "&lt;b&gt;ответ&lt;/b&gt;" in sent[0][1]
    assert "<b>ответ</b>" not in sent[0][1]
    assert "555" not in sent[0][1]
    assert answers == [("✅ Отправлено", None)]
    assert 123 not in botmod._pending_reply


def test_coach_reply_text_cancel_and_expired():
    botmod._pending_reply[123] = (555, time.time() + 600)
    message, answers, sent = _message(123, text="/cancel")
    asyncio.run(botmod.coach_reply_text(message))
    assert answers == [("Отменено", None)]
    assert sent == []

    botmod._pending_reply[123] = (555, time.time() - 1)
    message, answers, sent = _message(123, text="поздно")
    asyncio.run(botmod.coach_reply_text(message))
    assert "истекло" in answers[0][0]
    assert sent == []


def test_cmd_inbox_empty_and_forbidden(monkeypatch):
    async def fake_is_coach_id(telegram_id):
        return False

    async def fake_list_inbox(coach_id, limit):
        return []

    monkeypatch.setattr(db, "is_coach_id", fake_is_coach_id)
    monkeypatch.setattr(db, "list_inbox", fake_list_inbox)

    message, answers, _ = _message(555)
    asyncio.run(botmod.cmd_inbox(message))
    assert answers == []  # обычный игрок не получает inbox

    message, answers, _ = _message(123)
    asyncio.run(botmod.cmd_inbox(message))
    assert answers and "Входящих сообщений нет" in answers[0][0]


def test_cmd_inbox_renders_reply_button_for_animal(monkeypatch):
    async def fake_is_coach_id(telegram_id):
        return True

    async def fake_list_inbox(coach_id, limit):
        return [{
            "from_user": 555,
            "sender_name": "Иван",
            "display_mode": "animal",
            "text": "Привет <b>",
        }]

    monkeypatch.setattr(db, "is_coach_id", fake_is_coach_id)
    monkeypatch.setattr(db, "list_inbox", fake_list_inbox)

    message, answers, _ = _message(123)
    asyncio.run(botmod.cmd_inbox(message))

    text, markup = answers[0]
    assert animal_identity(555)["name"] in text
    assert "Иван" not in text
    assert "&lt;b&gt;" in text
    assert markup.inline_keyboard[0][0].callback_data == "reply:555"


def test_cb_apply_rejects_non_manager(monkeypatch):
    async def fake_is_manager_id(telegram_id):
        return False

    async def fake_get_application(telegram_id):
        raise AssertionError("заявка не должна читаться")

    monkeypatch.setattr(db, "is_manager_id", fake_is_manager_id)
    monkeypatch.setattr(db, "get_application", fake_get_application)
    callback, answers, _, _ = _callback(555, "apply:ok:888")
    asyncio.run(botmod.cb_apply(callback))

    assert answers == [("Недоступно", True)]


def test_cb_apply_approves_and_notifies(monkeypatch):
    async def fake_is_manager_id(telegram_id):
        return True

    async def fake_get_application(telegram_id):
        return {"telegram_id": telegram_id, "status": "pending"}

    async def fake_add_coach(telegram_id, added_by):
        return True

    async def fake_decide_application(telegram_id, status, decided_by):
        assert status == "approved"
        assert decided_by == 123

    monkeypatch.setattr(db, "is_manager_id", fake_is_manager_id)
    monkeypatch.setattr(db, "get_application", fake_get_application)
    monkeypatch.setattr(db, "add_coach", fake_add_coach)
    monkeypatch.setattr(db, "decide_application", fake_decide_application)

    callback, answers, chat_answers, sent = _callback(123, "apply:ok:888")
    asyncio.run(botmod.cb_apply(callback))

    assert answers == [("Одобрено", None)]
    assert "одобрена" in chat_answers[0]
    assert sent[0][0] == 888
    assert "одобрена" in sent[0][1]


def test_cb_apply_rejects_already_decided(monkeypatch):
    async def fake_is_manager_id(telegram_id):
        return True

    async def fake_get_application(telegram_id):
        return {"telegram_id": telegram_id, "status": "approved"}

    monkeypatch.setattr(db, "is_manager_id", fake_is_manager_id)
    monkeypatch.setattr(db, "get_application", fake_get_application)
    callback, answers, chat_answers, _ = _callback(123, "apply:no:888")
    asyncio.run(botmod.cb_apply(callback))

    assert answers == [("Заявка уже рассмотрена", True)]
    assert chat_answers == []


def test_cb_apply_ignores_malformed_data(monkeypatch):
    async def fake_is_manager_id(telegram_id):
        return True

    monkeypatch.setattr(db, "is_manager_id", fake_is_manager_id)
    callback, answers, _, _ = _callback(123, "apply:ok")
    asyncio.run(botmod.cb_apply(callback))
    assert answers == [(None, None)]

    callback, answers, _, _ = _callback(123, "apply:foo:888")
    asyncio.run(botmod.cb_apply(callback))
    assert answers == [(None, None)]


def test_fallback_opens_web_app(monkeypatch):
    monkeypatch.setattr(
        botmod,
        "settings",
        replace(settings, webhook_base_url="https://example.test"),
    )
    message, answers, _ = _message(555)
    asyncio.run(botmod.fallback(message))
    text, markup = answers[0]
    assert "Открой приложение" in text
    assert markup.inline_keyboard[0][0].web_app.url.endswith("?v=32")


def test_cache_avatar_uses_largest_photo(monkeypatch):
    stored = {}

    async def fake_set_avatar(user_id, file_id):
        stored[user_id] = file_id

    class FakeBot:
        async def get_user_profile_photos(self, user_id, limit=1):
            return SimpleNamespace(
                total_count=1,
                photos=[[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="big")]],
            )

    monkeypatch.setattr(db, "set_user_avatar", fake_set_avatar)
    asyncio.run(botmod._cache_avatar(FakeBot(), 555))
    assert stored == {555: "big"}


def test_send_training_reminder_counts_only_delivered(monkeypatch):
    async def fake_targets(training_id):
        return [{"telegram_id": 1}, {"telegram_id": 2}]

    class FakeBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, telegram_id, text):
            if telegram_id == 2:
                raise RuntimeError("telegram down")
            self.sent.append(telegram_id)

    monkeypatch.setattr(db, "notification_targets", fake_targets)
    bot = FakeBot()
    training = {
        "id": 1,
        "start_at": int(time.time()) + 3600,
        "training_type": "Тренировка",
        "capacity": 4,
    }
    delivered, total = asyncio.run(botmod.send_training_reminder(bot, training))
    assert delivered == 1
    assert total == 2


def test_pending_reply_filter_missing_user_and_missing_entry():
    assert asyncio.run(botmod.PendingReplyFilter()(SimpleNamespace(from_user=None))) is False
    message = SimpleNamespace(from_user=SimpleNamespace(id=123))
    assert asyncio.run(botmod.PendingReplyFilter()(message)) is False


def test_cmd_start_missing_user_and_avatar_path(monkeypatch):
    message, answers, _ = _message(123)
    message.from_user = None
    asyncio.run(botmod.cmd_start(message))
    assert answers == []

    cached = []

    async def fake_upsert(**kwargs):
        return None

    async def fake_cache(bot, user_id):
        cached.append(user_id)

    async def fake_is_coach_id(_id):
        return False

    monkeypatch.setattr(db, "upsert_user", fake_upsert)
    monkeypatch.setattr(db, "is_coach_id", fake_is_coach_id)
    monkeypatch.setattr(botmod, "_cache_avatar", fake_cache)
    message, answers, _ = _message(1002, first_name="Имя")
    message.bot = SimpleNamespace()
    asyncio.run(botmod.cmd_start(message))
    assert cached == [1002]
    assert answers and "Имя" in answers[0][0]


def test_cmd_start_handles_exception(monkeypatch):
    async def fake_upsert(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "upsert_user", fake_upsert)
    message, answers, _ = _message(1003)
    asyncio.run(botmod.cmd_start(message))
    assert answers == []


def test_cb_reply_without_message_and_malformed_data(monkeypatch):
    async def fake_is_coach_id(telegram_id):
        return True

    monkeypatch.setattr(db, "is_coach_id", fake_is_coach_id)
    callback, answers, _, _ = _callback(123, "reply:555")
    callback.message = None
    asyncio.run(botmod.cb_reply(callback))
    assert answers == [(None, None)]
    assert 123 in botmod._pending_reply

    botmod._pending_reply.clear()
    callback, answers, _, _ = _callback(123, "reply:")
    asyncio.run(botmod.cb_reply(callback))
    assert answers == []


def test_coach_reply_text_without_pending_bot_or_delivery():
    message, answers, sent = _message(123, text="просто текст")
    asyncio.run(botmod.coach_reply_text(message))
    assert answers == [] and sent == []

    botmod._pending_reply[123] = (555, time.time() + 600)
    message, answers, sent = _message(123, text="ответ")
    message.bot = None
    asyncio.run(botmod.coach_reply_text(message))
    assert answers == [] and sent == []

    class FailingBot:
        async def send_message(self, *args, **kwargs):
            raise RuntimeError("telegram down")

    botmod._pending_reply[123] = (555, time.time() + 600)
    message, answers, _ = _message(123, text="ответ")
    message.bot = FailingBot()
    asyncio.run(botmod.coach_reply_text(message))
    assert "Не удалось отправить" in answers[-1][0]


def test_cmd_inbox_handles_db_error(monkeypatch):
    async def fake_is_coach_id(telegram_id):
        return True

    async def fake_list_inbox(coach_id, limit):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "is_coach_id", fake_is_coach_id)
    monkeypatch.setattr(db, "list_inbox", fake_list_inbox)
    message, answers, _ = _message(123)
    asyncio.run(botmod.cmd_inbox(message))
    assert answers == []


def test_cb_apply_chat_missing_reject_and_error(monkeypatch):
    async def fake_is_manager_id(telegram_id):
        return True

    async def fake_get_application(telegram_id):
        return {"telegram_id": telegram_id, "status": "pending"}

    async def fake_decide(telegram_id, status, decided_by):
        return None

    monkeypatch.setattr(db, "is_manager_id", fake_is_manager_id)
    monkeypatch.setattr(db, "get_application", fake_get_application)
    monkeypatch.setattr(db, "decide_application", fake_decide)

    callback, answers, _, _ = _callback(123, "apply:no:888")
    callback.message = None
    asyncio.run(botmod.cb_apply(callback))
    assert answers == [("Сообщение недоступно", True)]

    callback, answers, chat_answers, sent = _callback(123, "apply:no:888")
    asyncio.run(botmod.cb_apply(callback))
    assert answers == [("Отклонено", None)]
    assert "отклонена" in chat_answers[0]
    assert sent[0][0] == 888

    callback, answers, _, _ = _callback(123, "apply:ok:not-an-id")
    asyncio.run(botmod.cb_apply(callback))
    assert answers == []


def test_fallback_handles_exception():
    message, answers, _ = _message(555)

    async def failing_answer(*args, **kwargs):
        raise RuntimeError("telegram down")

    message.answer = failing_answer
    asyncio.run(botmod.fallback(message))
    assert answers == []


def test_reminder_loop_marks_due_and_cancels(monkeypatch):
    training = {"id": 7}
    marked = []

    async def fake_due(now):
        return [training]

    async def fake_send(bot, row):
        return 1, 1

    async def fake_mark(training_id):
        marked.append(training_id)

    async def fake_sleep(seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(db, "trainings_due_for_reminder", fake_due)
    monkeypatch.setattr(botmod, "send_training_reminder", fake_send)
    monkeypatch.setattr(db, "mark_reminder_sent", fake_mark)
    monkeypatch.setattr(botmod.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(botmod.reminder_loop(None))
    assert marked == [7]


def test_reminder_loop_survives_error(monkeypatch):
    async def fake_due(now):
        raise RuntimeError("db down")

    async def fake_sleep(seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(db, "trainings_due_for_reminder", fake_due)
    monkeypatch.setattr(botmod.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(botmod.reminder_loop(None))


def test_polling_context_starts_and_stops(monkeypatch):
    started = asyncio.Event()

    class FakeDP:
        async def start_polling(self, bot, handle_signals=False):
            started.set()
            await asyncio.Event().wait()

    async def scenario():
        async with botmod.polling_context(None, FakeDP()):
            await started.wait()

    asyncio.run(scenario())


def test_polling_context_restarts_after_error(monkeypatch):
    calls = [0]

    class FakeDP:
        async def start_polling(self, bot, handle_signals=False):
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError("network")
            await asyncio.Event().wait()

    async def fake_wait_for(awaitable, timeout):
        awaitable.close()

    monkeypatch.setattr(botmod.asyncio, "wait_for", fake_wait_for)
    async def scenario():
        async with botmod.polling_context(None, FakeDP()):
            while calls[0] < 2:
                await asyncio.sleep(0)

    asyncio.run(scenario())
    assert calls[0] >= 2


def test_reminder_loop_cancelled_while_checking(monkeypatch):
    async def fake_due(now):
        raise asyncio.CancelledError

    monkeypatch.setattr(db, "trainings_due_for_reminder", fake_due)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(botmod.reminder_loop(None))


def _callback_with_message(user_id, data):
    callback, answers, chat_answers, _ = _callback(user_id, data)
    edits = []

    async def answer(text, reply_markup=None):
        chat_answers.append(text)

    async def edit_text(text, reply_markup=None):
        edits.append((text, reply_markup))

    callback.message = SimpleNamespace(answer=answer, edit_text=edit_text)
    return callback, answers, chat_answers, edits


def _training(start_at=None, capacity=2):
    return {
        "id": 1,
        "training_type": "Тренировка",
        "start_at": start_at if start_at is not None else int(time.time()) + 3600,
        "capacity": capacity,
        "booked_count": 1,
        "deleted_at": None,
    }


def test_bot_main_keyboard_slot_label_and_names(monkeypatch):
    monkeypatch.setattr(
        botmod,
        "settings",
        replace(settings, webhook_base_url="https://example.test"),
    )
    keyboard = botmod._main_keyboard()
    assert keyboard.inline_keyboard[0][0].web_app.url.endswith("?v=32")
    assert keyboard.inline_keyboard[1][0].callback_data == "slots:list"
    assert keyboard.inline_keyboard[1][1].callback_data == "attendees:list"
    label = botmod._slot_label(_training())
    assert "Тренировка" in label and "1/2" in label
    assert botmod._player_name({
        "display_mode": "animal", "telegram_id": 555, "first_name": "Иван"
    }) == animal_identity(555)["name"]
    assert botmod._player_name({
        "display_mode": "name", "telegram_id": 555, "first_name": ""
    }) == "Игрок"
    assert botmod._callback_message(SimpleNamespace(message=None)) is None


def test_slot_details_variants(monkeypatch):
    training = _training(capacity=1)
    participants = [
        {"status": "booked", "telegram_id": 555, "first_name": "Иван", "display_mode": "name"},
        {"status": "waitlist", "telegram_id": 556, "first_name": "Мария", "display_mode": "name"},
    ]

    async def get_training(_id):
        return training

    async def get_participants(_id):
        return participants

    async def no_booking(_id, _uid):
        return None

    monkeypatch.setattr(db, "get_training", get_training)
    monkeypatch.setattr(db, "get_participants", get_participants)
    monkeypatch.setattr(db, "get_booking", no_booking)

    details = asyncio.run(botmod._slot_details(1, 555))
    assert details and "Свободно: 0" in details[0]
    assert details[1].inline_keyboard[0][0].callback_data == "slots:book:1"

    async def booked(_id, _uid):
        return {"status": "booked"}

    monkeypatch.setattr(db, "get_booking", booked)
    details = asyncio.run(botmod._slot_details(1, 555))
    assert details and "Вы записаны" in details[0]
    assert details[1].inline_keyboard[0][0].callback_data == "slots:cancel:1"

    async def waitlist(_id, _uid):
        return {"status": "waitlist"}

    monkeypatch.setattr(db, "get_booking", waitlist)
    details = asyncio.run(botmod._slot_details(1, 555))
    assert details and "листе ожидания" in details[0]

    past = _training(start_at=0)
    async def past_training(_id):
        return past

    async def no_participants(_id):
        return []

    monkeypatch.setattr(db, "get_training", past_training)
    monkeypatch.setattr(db, "get_participants", no_participants)
    monkeypatch.setattr(db, "get_booking", no_booking)
    details = asyncio.run(botmod._slot_details(1, 555))
    assert details and details[1].inline_keyboard[0][0].callback_data.startswith("attendees:")

    async def missing(_id):
        return None

    monkeypatch.setattr(db, "get_training", missing)
    assert asyncio.run(botmod._slot_details(1, 555)) is None


def test_send_slots_and_cmd_slots(monkeypatch):
    async def no_rows(*args, **kwargs):
        return []

    async def rows(*args, **kwargs):
        return [_training()]

    monkeypatch.setattr(db, "list_upcoming_trainings", no_rows)
    message, answers, _ = _message(555)
    asyncio.run(botmod._send_slots(message, 555))
    assert "тренировок нет" in answers[0][0]

    monkeypatch.setattr(db, "list_upcoming_trainings", rows)
    message, answers, _ = _message(555)
    asyncio.run(botmod._send_slots(message, 555))
    assert answers[0][1].inline_keyboard[0][0].callback_data == "slots:show:1"

    message, answers, _ = _message(555)
    message.from_user = None
    asyncio.run(botmod.cmd_slots(message))
    assert answers == []

    async def broken(*args, **kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(botmod, "_send_slots", broken)
    message, answers, _ = _message(555)
    asyncio.run(botmod.cmd_slots(message))
    assert answers == []


def test_cb_slots_and_show_edges(monkeypatch):
    async def rows(*args, **kwargs):
        return [_training()]

    monkeypatch.setattr(db, "list_upcoming_trainings", rows)
    callback, answers, chat_answers, _ = _callback_with_message(555, "slots:list")
    asyncio.run(botmod.cb_slots(callback))
    assert answers == [(None, None)]
    assert chat_answers and "Выберите слот" in chat_answers[0]

    callback, _, _, _ = _callback_with_message(555, "slots:list")
    callback.message = None
    asyncio.run(botmod.cb_slots(callback))

    async def broken(*args, **kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(botmod, "_send_slots", broken)
    callback, _, _, _ = _callback_with_message(555, "slots:list")
    asyncio.run(botmod.cb_slots(callback))

    async def details(_id, _uid):
        return "text", InlineKeyboardMarkup(inline_keyboard=[])

    monkeypatch.setattr(botmod, "_slot_details", details)
    callback, answers, chat_answers, _ = _callback_with_message(555, "slots:show:1")
    asyncio.run(botmod.cb_slot_show(callback))
    assert chat_answers == ["text"]

    async def none_details(_id, _uid):
        return None

    monkeypatch.setattr(botmod, "_slot_details", none_details)
    callback, answers, chat_answers, _ = _callback_with_message(555, "slots:show:1")
    asyncio.run(botmod.cb_slot_show(callback))
    assert "не найдена" in chat_answers[0]

    callback, _, _, _ = _callback_with_message(555, "slots:show:bad")
    asyncio.run(botmod.cb_slot_show(callback))

    callback, _, _, _ = _callback_with_message(555, "slots:show:1")
    callback.message = None
    asyncio.run(botmod.cb_slot_show(callback))


def test_cb_slot_book_edges(monkeypatch):
    async def no_training(_id):
        return None

    monkeypatch.setattr(db, "get_training", no_training)
    callback, answers, _, _ = _callback_with_message(555, "slots:book:1")
    asyncio.run(botmod.cb_slot_book(callback))
    assert answers == [("Тренировка не найдена", True)]

    async def past(_id):
        return _training(start_at=0)

    monkeypatch.setattr(db, "get_training", past)
    callback, answers, _, _ = _callback_with_message(555, "slots:book:1")
    asyncio.run(botmod.cb_slot_book(callback))
    assert answers == [("Тренировка уже началась", True)]

    async def training(_id):
        return _training()

    async def book(*args, **kwargs):
        return "booked"

    async def details(_id, _uid):
        return "text", InlineKeyboardMarkup(inline_keyboard=[])

    monkeypatch.setattr(db, "get_training", training)
    monkeypatch.setattr(db, "book_training", book)
    monkeypatch.setattr(botmod, "_slot_details", details)
    callback, answers, _, edits = _callback_with_message(555, "slots:book:1")
    asyncio.run(botmod.cb_slot_book(callback))
    assert answers == [("Вы записаны", None)]
    assert edits and edits[0][0] == "text"

    async def waitlist(*args, **kwargs):
        return "waitlist"

    monkeypatch.setattr(db, "book_training", waitlist)
    callback, answers, _, _ = _callback_with_message(555, "slots:book:1")
    callback.message = None
    asyncio.run(botmod.cb_slot_book(callback))
    assert answers == [("Вы в листе ожидания", None)]

    callback, _, _, _ = _callback_with_message(555, "slots:book:bad")
    asyncio.run(botmod.cb_slot_book(callback))


def test_cb_slot_cancel_edges(monkeypatch):
    async def no_training(_id):
        return None

    monkeypatch.setattr(db, "get_training", no_training)
    callback, answers, _, _ = _callback_with_message(555, "slots:cancel:1")
    asyncio.run(botmod.cb_slot_cancel(callback))
    assert answers == [("Тренировка не найдена", True)]

    async def soon(_id):
        return _training(start_at=int(time.time()) + 60)

    monkeypatch.setattr(db, "get_training", soon)
    callback, answers, _, _ = _callback_with_message(555, "slots:cancel:1")
    asyncio.run(botmod.cb_slot_cancel(callback))
    assert "4.5" in answers[0][0]

    async def training(_id):
        return _training(start_at=int(time.time()) + 6 * 3600)

    async def cancel(*args, **kwargs):
        return ""

    async def details(_id, _uid):
        return "text", InlineKeyboardMarkup(inline_keyboard=[])

    monkeypatch.setattr(db, "get_training", training)
    monkeypatch.setattr(db, "cancel_booking", cancel)
    monkeypatch.setattr(botmod, "_slot_details", details)
    callback, answers, _, edits = _callback_with_message(555, "slots:cancel:1")
    asyncio.run(botmod.cb_slot_cancel(callback))
    assert answers == [("Запись отменена", None)]
    assert edits

    callback, _, _, _ = _callback_with_message(555, "slots:cancel:bad")
    asyncio.run(botmod.cb_slot_cancel(callback))


def test_cb_attendees_edges(monkeypatch):
    async def no_rows(*args, **kwargs):
        return []

    async def is_coach(_id):
        return False

    monkeypatch.setattr(db, "is_coach_id", is_coach)
    monkeypatch.setattr(db, "list_upcoming_trainings", no_rows)
    callback, _answers, chat_answers, _ = _callback_with_message(555, "attendees:list")
    asyncio.run(botmod.cb_attendees(callback))
    assert "тренировок нет" in chat_answers[0]

    async def coach(_id):
        return True

    async def active(*args, **kwargs):
        return [_training()]

    monkeypatch.setattr(db, "is_coach_id", coach)
    monkeypatch.setattr(db, "list_active_trainings", active)
    callback, _, chat_answers, _ = _callback_with_message(123, "attendees:list")
    asyncio.run(botmod.cb_attendees(callback))
    assert "выберите занятие" in chat_answers[0]

    callback, _, _, _ = _callback_with_message(555, "attendees:list")
    callback.message = None
    asyncio.run(botmod.cb_attendees(callback))

    async def broken(*args, **kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(db, "is_coach_id", broken)
    callback, _, _, _ = _callback_with_message(555, "attendees:list")
    asyncio.run(botmod.cb_attendees(callback))


def test_cb_attendees_show_edges(monkeypatch):
    async def no_training(_id):
        return None

    monkeypatch.setattr(db, "get_training", no_training)
    callback, _answers, chat_answers, _ = _callback_with_message(555, "attendees:show:1")
    asyncio.run(botmod.cb_attendees_show(callback))
    assert "не найдена" in chat_answers[0]

    async def training(_id):
        return _training()

    async def participants(_id):
        return [
            {"status": "booked", "telegram_id": 555, "first_name": "Иван", "display_mode": "name"},
            {"status": "waitlist", "telegram_id": 556, "first_name": "Мария", "display_mode": "animal"},
        ]

    monkeypatch.setattr(db, "get_training", training)
    monkeypatch.setattr(db, "get_participants", participants)

    async def not_coach(_id):
        return False

    monkeypatch.setattr(db, "is_coach_id", not_coach)
    callback, _, chat_answers, _ = _callback_with_message(555, "attendees:show:1")
    asyncio.run(botmod.cb_attendees_show(callback))
    assert "Иван" in chat_answers[0] and "Мария" not in chat_answers[0]

    callback, _, _, _ = _callback_with_message(555, "attendees:show:bad")
    asyncio.run(botmod.cb_attendees_show(callback))


def test_slot_callbacks_without_user_or_message(monkeypatch):
    async def no_training(_id):
        return None

    monkeypatch.setattr(botmod.db, "get_training", no_training)
    for handler, data in (
        (botmod.cb_slot_show, "slots:show:1"),
        (botmod.cb_slot_book, "slots:book:1"),
        (botmod.cb_slot_cancel, "slots:cancel:1"),
    ):
        callback, _, _, _ = _callback_with_message(555, data)
        callback.from_user = None
        asyncio.run(handler(callback))

    callback, _, _, _ = _callback_with_message(555, "attendees:show:1")
    callback.message = None
    asyncio.run(botmod.cb_attendees_show(callback))



def test_coach_reply_respects_message_notifications(monkeypatch):
    async def disabled(telegram_id, category):
        assert category == "messages"
        return False

    monkeypatch.setattr(db, "notification_enabled", disabled)
    botmod._pending_reply[123] = (555, time.time() + 600)
    message, answers, sent = _message(123, text="ответ")

    asyncio.run(botmod.coach_reply_text(message))

    assert sent == []
    assert answers == [("У игрока отключены уведомления о сообщениях.", None)]


def test_ensure_rules_message_creates_edits_and_pins(monkeypatch):
    state = {"record": None, "saved": [], "sent": 0, "edited": 0, "pinned": 0}

    async def fake_get(kind, entity_id, chat_id, user_id):
        assert (kind, entity_id, chat_id, user_id) == ("rules", 0, 123, 0)
        return state["record"]

    async def fake_save(kind, entity_id, chat_id, user_id, message_id, text):
        state["saved"].append((kind, entity_id, chat_id, user_id, message_id, text))
        state["record"] = {"message_id": message_id, "text": text}

    class RulesBot:
        async def send_message(self, chat_id, text):
            state["sent"] += 1
            return SimpleNamespace(message_id=77)

        async def edit_message_text(self, **kwargs):
            state["edited"] += 1

        async def pin_chat_message(self, **kwargs):
            state["pinned"] += 1

    monkeypatch.setattr(db, "get_event_notification", fake_get)
    monkeypatch.setattr(db, "save_event_notification", fake_save)
    bot = RulesBot()
    asyncio.run(botmod.ensure_rules_message(bot, 123))
    assert state["sent"] == 1
    assert state["edited"] == 0
    assert state["pinned"] == 1
    assert state["saved"][0][4] == 77

    # Повторный вызов обновляет существующее сообщение и снова закрепляет его.
    state["record"] = {"message_id": 77, "text": "old"}
    asyncio.run(botmod.ensure_rules_message(bot, 123))
    assert state["sent"] == 1
    assert state["edited"] == 1
    assert state["pinned"] == 2


def test_ensure_rules_message_recovers_from_missing_message(monkeypatch):
    state = {"record": {"message_id": 55, "text": "old"}, "sent": 0, "edited": 0}

    async def fake_get(*args):
        return state["record"]

    async def fake_save(*args):
        return None

    class RulesBot:
        async def send_message(self, chat_id, text):
            state["sent"] += 1
            return SimpleNamespace(message_id=88)

        async def edit_message_text(self, **kwargs):
            state["edited"] += 1
            raise RuntimeError("message not found")

        async def pin_chat_message(self, **kwargs):
            return None

    monkeypatch.setattr(db, "get_event_notification", fake_get)
    monkeypatch.setattr(db, "save_event_notification", fake_save)
    asyncio.run(botmod.ensure_rules_message(RulesBot(), 123))
    assert state["edited"] == 1
    assert state["sent"] == 1

    # Если Telegram вернул сообщение без message_id — молча выходим.
    class NoIdBot:
        async def send_message(self, chat_id, text):
            return SimpleNamespace(message_id=0)

    state["record"] = None
    asyncio.run(botmod.ensure_rules_message(NoIdBot(), 123))

    async def broken_get(*args):
        raise RuntimeError("db")

    monkeypatch.setattr(db, "get_event_notification", broken_get)
    asyncio.run(botmod.ensure_rules_message(RulesBot(), 123))


def test_ensure_rules_for_all_iterates_users(monkeypatch):
    sent = []

    async def fake_users():
        return [{"telegram_id": 1}, {"telegram_id": 2}]

    async def fake_rule(bot, chat_id):
        sent.append(chat_id)

    monkeypatch.setattr(db, "list_all_users", fake_users)
    monkeypatch.setattr(botmod, "ensure_rules_message", fake_rule)
    asyncio.run(botmod.ensure_rules_for_all(SimpleNamespace()))

    assert sent == [1, 2]


def test_ensure_rules_message_not_modified_and_pin_failure(monkeypatch):
    state = {"sent": 0, "pinned": 0}

    async def fake_get(*args):
        return {"message_id": 55, "text": "old"}

    async def fake_save(*args):
        return None

    class RulesBot:
        async def send_message(self, chat_id, text):
            state["sent"] += 1
            return SimpleNamespace(message_id=99)

        async def edit_message_text(self, **kwargs):
            raise RuntimeError("message is not modified")

        async def pin_chat_message(self, **kwargs):
            state["pinned"] += 1
            raise RuntimeError("cannot pin")

    monkeypatch.setattr(db, "get_event_notification", fake_get)
    monkeypatch.setattr(db, "save_event_notification", fake_save)
    asyncio.run(botmod.ensure_rules_message(RulesBot(), 123))
    assert state["sent"] == 0
    assert state["pinned"] == 1


def test_ensure_rules_for_all_handles_db_error(monkeypatch):
    async def broken_users():
        raise RuntimeError("db")

    monkeypatch.setattr(db, "list_all_users", broken_users)
    asyncio.run(botmod.ensure_rules_for_all(SimpleNamespace()))


def test_phone_contact_and_command(monkeypatch):
    saved = []

    async def fake_set_phone(user_id, phone):
        saved.append((user_id, phone))

    monkeypatch.setattr(db, "set_user_phone", fake_set_phone)
    message, answers, _ = _message(555, text="/phone")
    asyncio.run(botmod.cmd_phone(message))
    assert "телефон" in answers[0][0].lower()
    assert answers[0][1] is not None

    async def fake_send(chat_id, text, **kwargs):
        return None

    message, answers, _ = _message(555)
    message.contact = SimpleNamespace(user_id=555, phone_number="+7 999 123-45-67")
    message.answer = lambda text, **kwargs: _append_answer(answers, text, kwargs)
    asyncio.run(botmod.handle_contact(message))
    assert saved == [(555, "+79991234567")]
    assert answers


async def _append_answer(answers, text, kwargs=None):
    answers.append((text, kwargs.get("reply_markup") if kwargs else None))


def test_phone_contact_wrong_user_invalid_and_error(monkeypatch):
    async def fake_set_phone(user_id, phone):
        raise AssertionError("not called")

    monkeypatch.setattr(db, "set_user_phone", fake_set_phone)
    message, answers, _ = _message(555)
    message.contact = SimpleNamespace(user_id=556, phone_number="+79991234567")

    async def answer(text, **kwargs):
        answers.append((text, kwargs.get("reply_markup")))

    message.answer = answer
    asyncio.run(botmod.handle_contact(message))
    assert "своим" in answers[-1][0]

    message, answers, _ = _message(555)
    message.contact = SimpleNamespace(user_id=555, phone_number="123")
    message.answer = answer
    asyncio.run(botmod.handle_contact(message))
    assert "распознать" in answers[-1][0]

    message, answers, _ = _message(555)
    message.contact = None
    message.answer = answer
    asyncio.run(botmod.handle_contact(message))

    class BrokenAnswer:
        async def answer(self, *args, **kwargs):
            raise RuntimeError("telegram")

    broken = SimpleNamespace(
        from_user=SimpleNamespace(id=555),
        contact=SimpleNamespace(user_id=555, phone_number="+79991234567"),
        answer=BrokenAnswer().answer,
    )

    async def ok_set_phone(user_id, phone):
        return None

    monkeypatch.setattr(db, "set_user_phone", ok_set_phone)
    asyncio.run(botmod.handle_contact(broken))


def test_cmd_phone_handles_error():
    class BrokenMessage:
        async def answer(self, *args, **kwargs):
            raise RuntimeError("telegram")

    asyncio.run(botmod.cmd_phone(BrokenMessage()))


def test_cmd_start_prompts_for_phone(monkeypatch):
    async def fake_is_coach_id(_id):
        return False

    async def fake_upsert(**kwargs):
        return None

    async def fake_get_user(_id):
        return {"phone": ""}

    async def fake_cache(*args):
        return None

    monkeypatch.setattr(db, "is_coach_id", fake_is_coach_id)
    monkeypatch.setattr(db, "upsert_user", fake_upsert)
    monkeypatch.setattr(db, "get_user", fake_get_user)
    monkeypatch.setattr(botmod, "_cache_avatar", fake_cache)
    monkeypatch.setattr(
        botmod, "settings", replace(settings, webhook_base_url="https://example.test")
    )
    message, answers, _ = _message(555, first_name="Иван")
    message.bot = SimpleNamespace()
    asyncio.run(botmod.cmd_start(message))
    assert any("Телефон нужен" in text for text, _ in answers)


def test_attendees_coach_sees_username_and_phone(monkeypatch):
    async def training(_id):
        return _training()

    async def participants(_id):
        return [
            {
                "status": "booked",
                "telegram_id": 555,
                "first_name": "Иван",
                "username": "ivan",
                "phone": "+79991234567",
                "display_mode": "name",
            }
        ]

    async def is_coach(_id):
        return True

    monkeypatch.setattr(db, "get_training", training)
    monkeypatch.setattr(db, "get_participants", participants)
    monkeypatch.setattr(db, "is_coach_id", is_coach)
    callback, _, chat_answers, _ = _callback_with_message(123, "attendees:show:1")
    asyncio.run(botmod.cb_attendees_show(callback))
    assert "@ivan" in chat_answers[0]
    assert "+79991234567" in chat_answers[0]
