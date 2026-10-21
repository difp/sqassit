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

from app import bot as botmod
from app import database as db
from app.config import settings
from app.identity import animal_identity


@pytest.fixture(autouse=True)
def clear_pending_reply():
    botmod._pending_reply.clear()
    yield
    botmod._pending_reply.clear()


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
    assert botmod._web_app_url() == "https://example.test/app?x=1&v=9"


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
    assert markup.inline_keyboard[0][0].web_app.url.endswith("?v=9")
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
    assert markup.inline_keyboard[0][0].web_app.url.endswith("?v=9")


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
