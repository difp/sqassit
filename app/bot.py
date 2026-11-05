"""
Telegram-бот на aiogram 3.x: точка входа для пользователей + планировщик напоминаний.

Отделяет «телеграмную» логику от HTTP/Web App (main.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
import time
from datetime import datetime
from typing import cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)

from . import database as db
from .config import settings
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


WEB_APP_VERSION = "54"


def _web_app_url() -> str:
    """Публичный HTTPS-адрес Web App (для кнопки в боте)."""
    base = settings.webhook_base_url.rstrip("/")
    if not base:
        return "https://t.me"
    # Версия в query сбрасывает старый HTML из кэша Telegram WebView после
    # заметных обновлений интерфейса. Меняйте при крупных релизах.
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}v={WEB_APP_VERSION}"


def _training_tournament_url(training_id: int) -> str:
    """Открывает сетку занятия в мини-приложении участника."""
    parts = urlsplit(_web_app_url())
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key != "tournament"]
    query.append(("tournament", str(training_id)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _coach_training_url(training_id: int) -> str:
    """Открывает в мини-приложении конкретную тренировку тренера."""
    parts = urlsplit(_web_app_url())
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key != "training"]
    query.append(("training", str(training_id)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/coach",
                       urlencode(query), parts.fragment))


RULES_TEXT = (
    "<b>Правила SQASSIT</b>\n\n"
    "• Записывайтесь только при уверенности, что придёте. Отменяйте запись заранее — минимум за 4,5 часа.\n\n"
    "• Уважайте других игроков: соблюдайте договорённости, не опаздывайте и избегайте грубости.\n\n"
    "• Указывайте достоверные данные при создании матчей.\n\n"
    "• Личные данные доступны тренеру для модерации.\n\n"
    "• Нашли ошибку или есть идея? Напишите через «Обратную связь».\n\n"
    "Правила могут обновляться."
)


async def send_temporary_notification(
    bot: Bot, chat_id: int, text: str, *, kind: str = "notice",
    entity_id: int | None = None, user_id: int | None = None,
    delete_at: int | None = None, **kwargs,
):
    """Отправляет уведомление и планирует удаление через сутки."""
    sent = await bot.send_message(chat_id, text, **kwargs)
    message_id = int(getattr(sent, "message_id", 0) or 0)
    if message_id:
        await db.save_event_notification(
            kind, message_id if entity_id is None else entity_id,
            chat_id, chat_id if user_id is None else user_id, message_id, text,
            delete_at=delete_at or int(time.time()) + 86400,
        )
    return sent


async def ensure_rules_message(
    bot: Bot, chat_id: int, *, create_if_missing: bool = True
) -> None:
    """
    Создаёт/обновляет одно закреплённое сообщение с правилами.

    При обновлении существующего сообщения используется edit_message_text:
    игрок не получает новое уведомление. create_if_missing=False не отправляет
    ничего, если правила этому чату ещё не отправлялись.
    """
    try:
        record = await db.get_event_notification("rules", 0, chat_id, 0)
        message_id = int(record["message_id"] or 0) if record is not None else 0
        missing_message = False
        if message_id and record is not None and record["text"] != RULES_TEXT:
            try:
                await bot.edit_message_text(
                    text=RULES_TEXT, chat_id=chat_id, message_id=message_id
                )
            except Exception as exc:
                if "message is not modified" not in str(exc).lower():
                    logger.info("Не удалось обновить правила %s", chat_id, exc_info=True)
                    if isinstance(exc, TelegramBadRequest) and "message to edit not found" in str(exc).lower():
                        message_id = 0
                        missing_message = True
                    else:
                        return
        existing_message = bool(message_id)
        if not message_id:
            if not create_if_missing and not missing_message:
                return
            sent = await bot.send_message(chat_id, RULES_TEXT)
            message_id = int(getattr(sent, "message_id", 0) or 0)
        if not message_id:
            return
        await db.save_event_notification(
            "rules", 0, chat_id, 0, message_id, RULES_TEXT
        )
        try:
            await bot.pin_chat_message(
                chat_id=chat_id,
                message_id=message_id,
                disable_notification=True,
            )
        except Exception as exc:
            logger.info("Не удалось закрепить правила %s", chat_id, exc_info=True)
            if existing_message and isinstance(exc, TelegramBadRequest) and "message to pin not found" in str(exc).lower():
                sent = await bot.send_message(chat_id, RULES_TEXT)
                new_id = int(getattr(sent, "message_id", 0) or 0)
                if not new_id:
                    return
                await db.save_event_notification("rules", 0, chat_id, 0, new_id, RULES_TEXT)
                try:
                    await bot.pin_chat_message(
                        chat_id=chat_id, message_id=new_id, disable_notification=True
                    )
                except Exception:
                    logger.info("Не удалось закрепить новые правила %s", chat_id, exc_info=True)
    except Exception:
        logger.info("Не удалось отправить правила %s", chat_id, exc_info=True)


async def ensure_rules_for_all(bot: Bot) -> None:
    """Обновляет уже закреплённые правила, не создавая новых сообщений."""
    try:
        rows = await db.list_all_users()
        for row in rows:
            await ensure_rules_message(
                bot, int(row["telegram_id"]), create_if_missing=False
            )
            await asyncio.sleep(0.05)
    except Exception:
        logger.exception("Ошибка обновления правил")


def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="Открыть расписание",
                web_app=WebAppInfo(url=_web_app_url()),
            )],
        ]
    )


def _phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text="📱 Поделиться телефоном", request_contact=True)
        ]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _normalize_contact_phone(value: str) -> str:
    cleaned = re.sub(r"[^\d+]", "", (value or "").strip())
    plus = cleaned.startswith("+")
    digits = cleaned[1:] if plus else cleaned
    if not digits.isdigit() or not 10 <= len(digits) <= 15:
        return ""
    return ("+" if plus else "") + digits


async def cmd_phone(message: Message) -> None:
    """Просит телефон отдельной reply-кнопкой Telegram."""
    try:
        await message.answer(
            "📱 Поделитесь телефоном, чтобы тренер мог связаться с вами. "
            "Можно также ввести номер вручную в настройках профиля.",
            reply_markup=_phone_keyboard(),
        )
    except Exception:
        logger.exception("Ошибка запроса телефона")


async def handle_contact(message: Message) -> None:
    """Сохраняет только собственный контакт отправителя (не чужой)."""
    try:
        if message.from_user is None or message.contact is None:
            return
        contact = message.contact
        if contact.user_id != message.from_user.id:
            await message.answer("Поделитесь, пожалуйста, своим контактом.")
            return
        phone = _normalize_contact_phone(contact.phone_number)
        if not phone:
            await message.answer(
                "Не удалось распознать номер. Введите телефон в настройках профиля."
            )
            return
        await db.set_user_phone(message.from_user.id, phone)
        await message.answer(
            "✅ Телефон сохранён. Его увидит только тренер.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await message.answer("Меню:", reply_markup=_main_keyboard())
    except Exception:
        logger.exception("Ошибка сохранения телефона")


async def cmd_slots(message: Message) -> None:
    """Старую команду направляем в актуальное расписание приложения."""
    await message.answer(
        "Расписание и запись доступны в приложении.", reply_markup=_main_keyboard()
    )


async def cb_legacy_schedule(callback: CallbackQuery) -> None:
    """Обезвреживает кнопки в ранее отправленных сообщениях бота."""
    await callback.answer("Откройте расписание в приложении.", show_alert=True)
    message = callback.message
    if message is not None:
        try:
            await cast(Message, message).edit_text(
                "Расписание и запись доступны в приложении.",
                reply_markup=_main_keyboard(),
            )
        except Exception:
            logger.info("Не удалось обновить старое сообщение расписания", exc_info=True)


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
        if bot is not None:
            await ensure_rules_message(bot, user.id)
        await message.answer(
            f"Привет, {html.escape(user.first_name or 'друг')}!\n\n"
            "Здесь можно записаться на сквош-тренировки: смотри расписание на 7 дней, "
            "записывайся в один тап и следи за свободными местами.\n\n"
            "Нажми кнопку ниже, чтобы открыть приложение.",
            reply_markup=keyboard,
        )
        profile = await db.get_user(user.id)
        if profile is not None and not profile["phone"]:
            await message.answer(
                "📱 Телефон нужен тренеру для связи и модерации. "
                "Поделитесь контактом кнопкой ниже или введите номер в профиле.",
                reply_markup=_phone_keyboard(),
            )
        if (message.text or '').split(maxsplit=1)[1:] == ['match']:
            from .inline_match import command
            await command(message)
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
        if not await db.notification_enabled(target, "messages"):
            await message.answer("У игрока отключены уведомления о сообщениях.")
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

        # Кнопки из старых уведомлений тоже могут завершить заявку.
        with contextlib.suppress(Exception):
            await db.expire_event_notifications("coach_application", applicant)

        # Уведомляем заявителя (best-effort).
        with contextlib.suppress(Exception):
            text = (
                "🎉 Ваша заявка одобрена! Откройте приложение — появится вкладка «Тренер»."
                if action == "ok" else "Ваша заявка на тренера отклонена."
            )
            if chat.bot is not None:
                await send_temporary_notification(chat.bot, applicant, text)
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
    from . import inline_match
    inline_match.register(router)

    # Порядок важен: команды/кнопки должны проверяться раньше fallback.
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_slots, Command("slots"))
    router.message.register(cmd_phone, Command("phone"))
    router.message.register(handle_contact, F.contact)
    router.callback_query.register(cb_apply, F.data.startswith("apply:"))
    router.callback_query.register(cb_legacy_schedule, F.data.startswith("slots:"))
    router.callback_query.register(cb_legacy_schedule, F.data.startswith("attendees:"))
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
            await send_temporary_notification(
                bot, row["telegram_id"], text,
                kind="training_reminder", entity_id=training["id"],
                delete_at=min(
                    int(training["start_at"]) + int(training.get("duration_min", 60)) * 60 + 14400
                    if isinstance(training, dict) else
                    int(training["start_at"]) + int(training["duration_min"]) * 60 + 14400,
                    int(time.time()) + 47 * 3600,
                ),
            )
            await db.mark_reminder_delivered(training["id"], row["telegram_id"])
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
                # Недоставленные сообщения повторим на следующем тике.
                if delivered == total:
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
