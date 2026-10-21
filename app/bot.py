"""
Telegram-бот на aiogram 3.x: точка входа для пользователей + планировщик напоминаний.

Отделяет «телеграмную» логику от HTTP/Web App (main.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import time
from datetime import datetime, timedelta
from typing import cast

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from . import database as db
from .config import BOOKING_CANCEL_LOCK_MINUTES, settings
from .identity import animal_identity

logger = logging.getLogger(__name__)

# Тренер, ожидающий ответа игроку: {coach_id: (player_id, expires_at)}
_pending_reply: dict[int, tuple[int, float]] = {}
_PENDING_REPLY_TTL_SECONDS = 10 * 60


class PendingReplyFilter(BaseFilter):
    """Пропускает только сообщения тренера, который сейчас пишет ответ игроку."""

    async def __call__(self, message: Message) -> bool:
        if message.from_user is None:
            return False
        entry = _pending_reply.get(message.from_user.id)
        if entry is None:
            return False
        _, expires_at = entry
        if expires_at < time.time():
            _pending_reply.pop(message.from_user.id, None)
            return False
        return True


WEB_APP_VERSION = "17"


def _web_app_url() -> str:
    """Публичный HTTPS-адрес Web App (для кнопки в боте)."""
    base = settings.webhook_base_url.rstrip("/")
    if not base:
        return "https://t.me"
    # Версия в query сбрасывает старый HTML из кэша Telegram WebView после
    # заметных обновлений интерфейса. Меняйте при крупных релизах.
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}v={WEB_APP_VERSION}"


def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🏸 Открыть тренировки",
                web_app=WebAppInfo(url=_web_app_url()),
            )],
            [
                InlineKeyboardButton(text="🗓 Выбрать слот", callback_data="slots:list"),
                InlineKeyboardButton(text="👥 Кто записался", callback_data="attendees:list"),
            ],
        ]
    )


def _slot_label(training) -> str:
    start = datetime.fromtimestamp(training["start_at"], tz=settings.timezone)
    summary = (training["training_type"] or "Тренировка")[:18]
    return f"{start.strftime('%d.%m %H:%M')} · {summary} · {training['booked_count']}/{training['capacity']}"


def _callback_message(callback: CallbackQuery) -> Message | None:
    message = callback.message
    return cast(Message, message) if message is not None else None


def _player_name(row) -> str:
    if row["display_mode"] == "animal":
        return animal_identity(row["telegram_id"])["name"]
    return row["first_name"] or "Игрок"


async def _slot_details(training_id: int, user_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    training = await db.get_training(training_id)
    if training is None or training["deleted_at"]:
        return None
    participants = await db.get_participants(training_id)
    booked = [p for p in participants if p["status"] == "booked"]
    waitlist = [p for p in participants if p["status"] == "waitlist"]
    booking = await db.get_booking(training_id, user_id)
    start = datetime.fromtimestamp(training["start_at"], tz=settings.timezone)
    free = max(0, training["capacity"] - len(booked))
    status = ""
    if booking is not None and booking["status"] == "booked":
        status = "\n✅ Вы записаны"
    elif booking is not None and booking["status"] == "waitlist":
        status = "\n⏳ Вы в листе ожидания"
    text = (
        f"🏸 <b>{html.escape(training['training_type'] or 'Тренировка')}</b>\n"
        f"📅 {start.strftime('%d.%m.%Y в %H:%M')}\n"
        f"👥 Свободно: {free} из {training['capacity']}\n"
        f"⌛ В очереди: {len(waitlist)}"
        f"{status}"
    )
    buttons = []
    if booking is not None and booking["status"] in {"booked", "waitlist"}:
        buttons.append([InlineKeyboardButton(
            text="🗑 Отменить запись", callback_data=f"slots:cancel:{training_id}"
        )])
    elif training["start_at"] > int(time.time()):
        buttons.append([InlineKeyboardButton(
            text="✅ Записаться" if free else "⏳ В лист ожидания",
            callback_data=f"slots:book:{training_id}",
        )])
    buttons.append([InlineKeyboardButton(
        text="👥 Кто записался", callback_data=f"attendees:show:{training_id}"
    )])
    buttons.append([InlineKeyboardButton(text="⬅️ К слотам", callback_data="slots:list")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def _send_slots(message: Message, user_id: int) -> None:
    now = int(time.time())
    until = int((datetime.now(settings.timezone) + timedelta(days=7)).timestamp())
    rows = await db.list_upcoming_trainings(now, until)
    if not rows:
        await message.answer("На ближайшие 7 дней тренировок нет.")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=_slot_label(row), callback_data=f"slots:show:{row['id']}"
        )]
        for row in rows[:20]
    ])
    await message.answer("Выберите слот:", reply_markup=keyboard)


async def cmd_slots(message: Message) -> None:
    try:
        if message.from_user is None:
            return
        await _send_slots(message, message.from_user.id)
    except Exception:
        logger.exception("Ошибка выбора слота")


async def cb_slots(callback: CallbackQuery) -> None:
    await callback.answer()
    message = _callback_message(callback)
    if message is None or callback.from_user is None:
        return
    try:
        await _send_slots(message, callback.from_user.id)
    except Exception:
        logger.exception("Ошибка списка слотов")


async def cb_slot_show(callback: CallbackQuery) -> None:
    try:
        if callback.from_user is None:
            return
        training_id = int((callback.data or "").split(":")[-1])
        details = await _slot_details(training_id, callback.from_user.id)
        await callback.answer()
        message = _callback_message(callback)
        if message is None:
            return
        if details is None:
            await message.answer("Тренировка не найдена.")
            return
        text, keyboard = details
        await message.answer(text, reply_markup=keyboard)
    except Exception:
        logger.exception("Ошибка показа слота")


async def cb_slot_book(callback: CallbackQuery) -> None:
    try:
        if callback.from_user is None:
            return
        training_id = int((callback.data or "").split(":")[-1])
        training = await db.get_training(training_id)
        if training is None or training["deleted_at"]:
            await callback.answer("Тренировка не найдена", show_alert=True)
            return
        if training["start_at"] <= int(time.time()):
            await callback.answer("Тренировка уже началась", show_alert=True)
            return
        status = await db.book_training(
            training_id, callback.from_user.id, training["capacity"]
        )
        details = await _slot_details(training_id, callback.from_user.id)
        await callback.answer(
            "Вы записаны" if status == "booked" else "Вы в листе ожидания"
        )
        message = _callback_message(callback)
        if message is not None and details is not None:
            text, keyboard = details
            await message.edit_text(text, reply_markup=keyboard)
    except Exception:
        logger.exception("Ошибка записи через слот")


async def cb_slot_cancel(callback: CallbackQuery) -> None:
    try:
        if callback.from_user is None:
            return
        training_id = int((callback.data or "").split(":")[-1])
        training = await db.get_training(training_id)
        if training is None or training["deleted_at"]:
            await callback.answer("Тренировка не найдена", show_alert=True)
            return
        seconds_left = training["start_at"] - int(time.time())
        if seconds_left < BOOKING_CANCEL_LOCK_MINUTES * 60:
            await callback.answer(
                "Отмена недоступна: до старта меньше 4.5 часов",
                show_alert=True,
            )
            return
        await db.cancel_booking(training_id, callback.from_user.id)
        details = await _slot_details(training_id, callback.from_user.id)
        await callback.answer("Запись отменена")
        message = _callback_message(callback)
        if message is not None and details is not None:
            text, keyboard = details
            await message.edit_text(text, reply_markup=keyboard)
    except Exception:
        logger.exception("Ошибка отмены через слот")


async def cb_attendees(callback: CallbackQuery) -> None:
    await callback.answer()
    message = _callback_message(callback)
    if message is None or callback.from_user is None:
        return
    try:
        now = int(time.time())
        user_id = callback.from_user.id
        if await db.is_coach_id(user_id):
            rows = await db.list_active_trainings(now, user_id)
        else:
            until = int((datetime.now(settings.timezone) + timedelta(days=7)).timestamp())
            rows = await db.list_upcoming_trainings(now, until)
        if not rows:
            await message.answer("Ближайших тренировок нет.")
            return
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=_slot_label(row), callback_data=f"attendees:show:{row['id']}"
            )]
            for row in rows[:20]
        ])
        await message.answer("Кто записался — выберите занятие:", reply_markup=keyboard)
    except Exception:
        logger.exception("Ошибка списка участников")


async def cb_attendees_show(callback: CallbackQuery) -> None:
    try:
        training_id = int((callback.data or "").split(":")[-1])
        training = await db.get_training(training_id)
        await callback.answer()
        message = _callback_message(callback)
        if message is None:
            return
        if training is None or training["deleted_at"]:
            await message.answer("Тренировка не найдена.")
            return
        participants = await db.get_participants(training_id)
        booked = [p for p in participants if p["status"] == "booked"]
        waitlist = [p for p in participants if p["status"] == "waitlist"]
        lines = [f"👥 <b>Кто записался</b> · {html.escape(training['training_type'] or 'Тренировка')}"]
        lines.append("Записаны: " + (", ".join(
            html.escape(_player_name(p)) for p in booked[:40]
        ) if booked else "пока никого"))
        if waitlist:
            lines.append("В очереди: " + ", ".join(
                html.escape(_player_name(p)) for p in waitlist[:20]
            ))
        await message.answer("\n".join(lines))
    except Exception:
        logger.exception("Ошибка показа участников")


async def _cache_avatar(bot: Bot, user_id: int) -> None:
    """Кэширует file_id аватарки (файл НЕ скачивается и НЕ пишется на диск)."""
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count and photos.photos:
            # Берём самый крупный размер из первого фото.
            file_id = photos.photos[0][-1].file_id
            await db.set_user_avatar(user_id, file_id)
    except Exception:  # аватарка — не критичная операция
        logger.warning("Не удалось получить аватарку для %s", user_id, exc_info=True)


async def cmd_start(message: Message) -> None:
    """CRM: при первом старте создаём карточку игрока и открываем Web App."""
    try:
        user = message.from_user
        if user is None:
            return
        is_coach = (user.id == settings.coach_telegram_id) or await db.is_coach_id(user.id)
        await db.upsert_user(
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name,
            is_coach=is_coach,
        )
        bot = message.bot
        if bot is not None:
            await _cache_avatar(bot, user.id)

        keyboard = _main_keyboard()
        await message.answer(
            f"Привет, {html.escape(user.first_name or 'друг')}!\n\n"
            "Здесь можно записаться на сквош-тренировки: смотри расписание на 7 дней, "
            "записывайся в один тап и следи за свободными местами.\n\n"
            "Нажми кнопку ниже, чтобы открыть приложение.",
            reply_markup=keyboard,
        )
    except Exception:
        # Один сбойный апдейт не должен ронять бота.
        logger.exception("Ошибка в обработчике /start")


async def cb_reply(callback: CallbackQuery) -> None:
    """Кнопка «Ответить» под сообщением игрока: тренер пишет ответ следующим сообщением."""
    try:
        is_coach = (
            callback.from_user.id == settings.coach_telegram_id
            or await db.is_coach_id(callback.from_user.id)
        )
        if not is_coach:
            await callback.answer("Недоступно", show_alert=True)
            return
        data = callback.data or ""
        target = int(data.split(":", 1)[1])
        _pending_reply[callback.from_user.id] = (
            target,
            time.time() + _PENDING_REPLY_TTL_SECONDS,
        )
        await callback.answer()
        if callback.message is None:
            return
        await callback.message.answer(
            "✍️ Напишите ответ игроку и отправьте одним сообщением.\nОтмена: /cancel"
        )
    except Exception:
        logger.exception("Ошибка обработки кнопки ответа")


async def coach_reply_text(message: Message) -> None:
    """Текст тренера в режиме ответа — доставляем игроку."""
    try:
        user = message.from_user
        entry = _pending_reply.pop(user.id, None) if user else None
        if entry is None:
            return
        target, expires_at = entry
        if expires_at < time.time():
            await message.answer("Время ответа истекло. Нажмите «Ответить» ещё раз.")
            return
        text = (message.text or "").strip()
        if text == "/cancel":
            await message.answer("Отменено")
            return
        if message.bot is None:
            return
        await message.bot.send_message(
            target, f"💬 <b>Сообщение от тренера:</b>\n{html.escape(text)}"
        )
        await message.answer("✅ Отправлено")
    except Exception:
        logger.exception("Ошибка отправки ответа игроку")
        with contextlib.suppress(Exception):
            await message.answer("Не удалось отправить: игрок ещё не начинал чат с ботом.")


async def cmd_inbox(message: Message) -> None:
    """Тренер: последние личные сообщения от игроков."""
    try:
        user = message.from_user
        if user is None or not (
            user.id == settings.coach_telegram_id or await db.is_coach_id(user.id)
        ):
            return
        rows = await db.list_inbox(user.id, 15)
        if not rows:
            await message.answer("📭 Входящих сообщений нет.")
            return
        for r in rows:
            if r["display_mode"] == "animal":
                sender_name = animal_identity(r["from_user"])["name"]
            else:
                sender_name = r["sender_name"] or "Игрок"
            name = html.escape(sender_name)
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✍️ Ответить",
                            callback_data=f"reply:{r['from_user']}",
                        )
                    ]
                ]
            )
            await message.answer(
                f"✉️ <b>{name}</b>\n{html.escape(r['text'])}",
                reply_markup=keyboard,
            )
    except Exception:
        logger.exception("Ошибка чтения входящих")


async def cb_apply(callback: CallbackQuery) -> None:
    """Одобрение/отклонение заявки в тренеры (владелец или менеджер)."""
    try:
        uid = callback.from_user.id
        if not (uid == settings.coach_telegram_id or await db.is_manager_id(uid)):
            await callback.answer("Недоступно", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 3:
            await callback.answer()
            return
        _, action, applicant_raw = parts
        if action not in {"ok", "no"}:
            await callback.answer()
            return
        applicant = int(applicant_raw)

        application = await db.get_application(applicant)
        if application is None or application["status"] != "pending":
            await callback.answer("Заявка уже рассмотрена", show_alert=True)
            return

        chat = callback.message
        if chat is None:
            await callback.answer("Сообщение недоступно", show_alert=True)
            return

        if action == "ok":
            await db.add_coach(applicant, uid)
            await db.decide_application(applicant, "approved", uid)
            await chat.answer("✅ Заявка одобрена")
            await callback.answer("Одобрено")
        else:
            await db.decide_application(applicant, "rejected", uid)
            await chat.answer("❌ Заявка отклонена")
            await callback.answer("Отклонено")

        # Уведомляем заявителя (best-effort).
        with contextlib.suppress(Exception):
            text = (
                "🎉 Ваша заявка одобрена! Откройте приложение — появится вкладка «Тренер»."
                if action == "ok" else "Ваша заявка на тренера отклонена."
            )
            if chat.bot is not None:
                await chat.bot.send_message(applicant, text)
    except Exception:
        logger.exception("Ошибка обработки заявки")


async def fallback(message: Message) -> None:
    """Любой текст — мягко направляем в Web App."""
    try:
        await message.answer(
            "Открой приложение кнопкой ниже 👇", reply_markup=_main_keyboard()
        )
    except Exception:
        logger.exception("Ошибка в fallback-обработчике")


def create_bot() -> Bot:
    session = (
        AiohttpSession(proxy=settings.bot_proxy) if settings.bot_proxy else None
    )
    return Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher() -> Dispatcher:
    """
    Собирает роутер заново для каждого жизненного цикла приложения.

    aiogram запрещает подключать один и тот же Router к нескольким Dispatcher,
    а тесты (и потенциальный перезапуск) создают приложение несколько раз.
    """
    dp = Dispatcher()
    router = Router()

    # Порядок важен: команды/кнопки должны проверяться раньше fallback.
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_slots, Command("slots"))
    router.callback_query.register(cb_reply, F.data.startswith("reply:"))
    router.message.register(cmd_inbox, Command("inbox"))
    router.message.register(coach_reply_text, F.text, PendingReplyFilter())
    router.callback_query.register(cb_apply, F.data.startswith("apply:"))
    router.callback_query.register(cb_slots, F.data == "slots:list")
    router.callback_query.register(cb_slot_show, F.data.startswith("slots:show:"))
    router.callback_query.register(cb_slot_book, F.data.startswith("slots:book:"))
    router.callback_query.register(cb_slot_cancel, F.data.startswith("slots:cancel:"))
    router.callback_query.register(cb_attendees, F.data == "attendees:list")
    router.callback_query.register(cb_attendees_show, F.data.startswith("attendees:show:"))
    router.message.register(fallback, F.text)

    dp.include_router(router)
    return dp


def format_training_reminder(training) -> str:
    """Формирует текст напоминания; все пользовательские строки экранируются."""
    start_local = datetime.fromtimestamp(training["start_at"], tz=settings.timezone)
    summary = html.escape(training["training_type"] or "Сквош-тренировка")
    return (
        "⏰ <b>Напоминание о тренировке</b>\n\n"
        f"🏸 {summary}\n"
        f"📅 {start_local.strftime('%d.%m.%Y в %H:%M')}\n"
        f"Всего мест: {training['capacity']}"
    )


_TELEGRAM_TEXT_LIMIT = 4000


def _chunk_telegram_messages(
    blocks: list[str],
    *,
    header: str = "",
    limit: int = _TELEGRAM_TEXT_LIMIT,
) -> list[str]:
    """Склеивает блоки в сообщения, не превышающие лимит Telegram."""
    chunks: list[str] = []
    current = header
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if current and len(candidate) > limit:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def send_training_reminder(bot: Bot, training) -> tuple[int, int]:
    """
    Рассылает напоминание записавшимся игрокам.

    Возвращает (доставлено, всего получателей), чтобы планировщик не помечал
    напоминание отправленным, если Telegram полностью недоступен.
    """
    text = format_training_reminder(training)
    recipients = await db.notification_targets(training["id"])
    delivered = 0
    for row in recipients:
        try:
            await bot.send_message(row["telegram_id"], text)
            delivered += 1
        except Exception:
            logger.warning("Не доставлено напоминание %s", row["telegram_id"], exc_info=True)
    return delivered, len(recipients)


async def reminder_loop(bot: Bot, interval_seconds: int = 60) -> None:
    """
    Фоновый планировщик: раз в минуту проверяет, кому пора напомнить.
    Любые исключения глушатся, чтобы цикл не умирал.
    """
    while True:
        try:
            now = int(time.time())
            due = await db.trainings_due_for_reminder(now)
            for training in due:
                delivered, total = await send_training_reminder(bot, training)
                # Если получателей нет — помечаем, чтобы не проверять вечно.
                # Если не доставлено ни одного — повторим на следующем тике.
                if total == 0 or delivered > 0:
                    await db.mark_reminder_sent(training["id"])
        except asyncio.CancelledError:
            logger.info("Планировщик напоминаний остановлен.")
            raise
        except Exception:
            logger.exception("Ошибка в планировщике напоминаний")
        await asyncio.sleep(interval_seconds)


@contextlib.asynccontextmanager
async def polling_context(bot: Bot, dp: Dispatcher):
    """Long-polling в фоне с авто-перезапуском при сбоях и корректной остановкой."""
    stop = asyncio.Event()

    async def runner() -> None:
        while not stop.is_set():
            try:
                await dp.start_polling(bot, handle_signals=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Long-polling прерван, перезапуск через 5 секунд")
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=5)

    task = asyncio.create_task(runner())
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        # Гасим polling без шума: сетевые ошибки Telegram при остановке не должны
        # валить lifespan приложения.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
