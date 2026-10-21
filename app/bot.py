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
from datetime import datetime

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


WEB_APP_VERSION = "7"


def _web_app_url() -> str:
    """Публичный HTTPS-адрес Web App (для кнопки в боте)."""
    base = settings.webhook_base_url.rstrip("/")
    if not base:
        return "https://t.me"
    # Версия в query сбрасывает старый HTML из кэша Telegram WebView после
    # заметных обновлений интерфейса. Меняйте при крупных релизах.
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}v={WEB_APP_VERSION}"


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

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🏸 Открыть тренировки", web_app=WebAppInfo(url=_web_app_url()))]
            ]
        )
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
        await message.answer("Открой приложение кнопкой ниже 👇", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(
                text="🏸 Открыть тренировки", web_app=WebAppInfo(url=_web_app_url())
            )]]
        ))
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
    router.callback_query.register(cb_reply, F.data.startswith("reply:"))
    router.message.register(coach_reply_text, F.text, PendingReplyFilter())
    router.message.register(cmd_inbox, Command("inbox"))
    router.callback_query.register(cb_apply, F.data.startswith("apply:"))
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
