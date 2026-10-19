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

logger = logging.getLogger(__name__)

router = Router()

# Тренер, ожидающий ответа игроку: {coach_id: player_id}
_pending_reply: dict[int, int] = {}


class PendingReplyFilter(BaseFilter):
    """Пропускает только сообщения тренера, который сейчас пишет ответ игроку."""

    async def __call__(self, message: Message) -> bool:
        return message.from_user is not None and message.from_user.id in _pending_reply


def _web_app_url() -> str:
    """Публичный HTTPS-адрес Web App (для кнопки в боте)."""
    base = settings.webhook_base_url.rstrip("/")
    return base or "https://t.me"


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


@router.message(CommandStart())
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
        await _cache_avatar(message.bot, user.id)

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🏸 Открыть тренировки", web_app=WebAppInfo(url=_web_app_url()))]
            ]
        )
        await message.answer(
            f"Привет, {user.first_name}!\n\n"
            "Здесь можно записаться на сквош-тренировки: смотри расписание на 7 дней, "
            "записывайся в один тап и следи за свободными местами.\n\n"
            "Нажми кнопку ниже, чтобы открыть приложение.",
            reply_markup=keyboard,
        )
    except Exception:
        # Один сбойный апдейт не должен ронять бота.
        logger.exception("Ошибка в обработчике /start")


@router.message(Command("reply"))
async def cmd_reply(message: Message) -> None:
    """Ответ тренера игроку: /reply <telegram_id> <текст> (только для тренера)."""
    try:
        user = message.from_user
        if user is None or not (
            user.id == settings.coach_telegram_id or await db.is_coach_id(user.id)
        ):
            return  # не тренер — игнорируем
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 3 or not parts[1].lstrip("-").isdigit():
            await message.answer("Формат: <code>/reply &lt;id игрока&gt; текст</code>")
            return
        player_id = int(parts[1])
        body = html.escape(parts[2].strip())
        await message.bot.send_message(player_id, f"💬 <b>Сообщение от тренера:</b>\n{body}")
        await message.answer("Отправлено ✅")
    except Exception:
        logger.exception("Ошибка ответа игроку")
        with contextlib.suppress(Exception):
            await message.answer("Не удалось отправить: игрок ещё не начинал чат с ботом.")


@router.callback_query(F.data.startswith("reply:"))
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
        target = int(callback.data.split(":", 1)[1])
        _pending_reply[callback.from_user.id] = target
        await callback.answer()
        await callback.message.answer(
            "✍️ Напишите ответ игроку и отправьте одним сообщением.\nОтмена: /cancel"
        )
    except Exception:
        logger.exception("Ошибка обработки кнопки ответа")


@router.message(F.text, PendingReplyFilter())
async def coach_reply_text(message: Message) -> None:
    """Текст тренера в режиме ответа — доставляем игроку."""
    try:
        user = message.from_user
        target = _pending_reply.pop(user.id, None) if user else None
        if target is None:
            return
        text = (message.text or "").strip()
        if text == "/cancel":
            await message.answer("Отменено")
            return
        await message.bot.send_message(
            target, f"💬 <b>Сообщение от тренера:</b>\n{html.escape(text)}"
        )
        await message.answer("✅ Отправлено")
    except Exception:
        logger.exception("Ошибка отправки ответа игроку")
        with contextlib.suppress(Exception):
            await message.answer("Не удалось отправить: игрок ещё не начинал чат с ботом.")


@router.message(Command("inbox"))
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
        blocks = []
        for r in rows:
            name = html.escape(r["sender_name"] or "Игрок")
            blocks.append(
                f"✉️ <b>{name}</b> (<code>{r['from_user']}</code>)\n"
                f"{html.escape(r['text'])}\n"
                f"↩ <code>/reply {r['from_user']} текст</code>"
            )
        await message.answer("📥 <b>Последние сообщения</b>\n\n" + "\n\n".join(blocks))
    except Exception:
        logger.exception("Ошибка чтения входящих")


@router.callback_query(F.data.startswith("apply:"))
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
        applicant = int(applicant_raw)

        if action == "ok":
            await db.add_coach(applicant, uid)
            await db.decide_application(applicant, "approved", uid)
            await callback.message.answer("✅ Заявка одобрена")
            await callback.answer("Одобрено")
        else:
            await db.decide_application(applicant, "rejected", uid)
            await callback.message.answer("❌ Заявка отклонена")
            await callback.answer("Отклонено")

        # Уведомляем заявителя (best-effort).
        with contextlib.suppress(Exception):
            text = (
                "🎉 Ваша заявка одобрена! Откройте приложение — появится вкладка «Тренер»."
                if action == "ok" else "Ваша заявка на тренера отклонена."
            )
            await callback.message.bot.send_message(applicant, text)
    except Exception:
        logger.exception("Ошибка обработки заявки")


@router.message(F.text)
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
    return Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp


async def send_training_reminder(bot: Bot, training) -> None:
    """Рассылает напоминание игрокам и тренеру по конкретной тренировке."""
    start_local = datetime.fromtimestamp(training["start_at"], tz=settings.timezone)
    text = (
        "⏰ <b>Напоминание о тренировке</b>\n\n"
        f"🏸 {training['training_type']}\n"
        f"📅 {start_local.strftime('%d.%m.%Y в %H:%M')}\n"
        f"Всего мест: {training['capacity']}"
    )
    recipients = await db.notification_targets(training["id"])
    for row in recipients:
        try:
            await bot.send_message(row["telegram_id"], text)
        except Exception:
            logger.warning("Не доставлено напоминание %s", row["telegram_id"], exc_info=True)


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
                await send_training_reminder(bot, training)
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
