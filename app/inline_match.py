"""Публикация матчей в чатах через Telegram inline mode."""
from __future__ import annotations

import asyncio
import html
import logging
import time
import weakref
from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultUnion,
    InputTextMessageContent,
    Message,
    WebAppInfo,
)
from fastapi import HTTPException

from . import database as db
from .config import settings
from .identity import animal_identity

logger = logging.getLogger(__name__)
_refresh_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()


def keyboard(match_id: int, label: str = "Записаться") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f"match_join:{match_id}")
    ]])


def card(match, players) -> str:
    booked = [p for p in players if p['status'] == 'booked']
    local = datetime.fromtimestamp(match['start_at'], settings.timezone)
    text = f"<b>{html.escape((match['title'] or 'Матч')[:100])}</b>\n{local:%d.%m · %H:%M}"
    if match['location']:
        text += f"\n{html.escape(match['location'][:120])}"
    text += f"\n\nУчастники · {len(booked)} из {match['max_players']}"
    shown = 0
    for player in booked[:50]:
        name = animal_identity(player['telegram_id'])['name'] if player['display_mode'] == 'animal' else player['first_name'] or 'Игрок'
        line = '\n' + html.escape(name[:60])
        if len(text) + len(line) > 3500:
            break
        text += line
        shown += 1
    if len(booked) > shown:
        text += f"\n… и ещё {len(booked) - shown}"
    if match['deleted_at']:
        text += '\n\nМатч отменён'
    elif match['start_at'] <= int(time.time()):
        text += '\n\nМатч начался'
    elif len(booked) >= match['max_players']:
        text += '\n\nМест нет'
    return text


async def refresh(bot, match_id: int) -> None:
    lock = _refresh_locks.setdefault(match_id, asyncio.Lock())
    async with lock:
        match = await db.get_match(match_id)
        if match is None:
            return
        refs = await db.list_match_inline_messages(match_id)
        if not refs:
            return
        players = await db.get_match_players(match_id)
        text = card(match, players)
        label = (
            'Матч отменён' if match['deleted_at'] else
            'Матч начался' if match['start_at'] <= int(time.time()) else
            'Мест нет' if match['players_count'] >= match['max_players'] else
            'Записаться'
        )
        for row in refs:
            try:
                await bot.edit_message_text(text=text, inline_message_id=row['inline_message_id'], reply_markup=keyboard(match_id, label))
            except TelegramBadRequest as exc:
                if 'message is not modified' not in str(exc).lower():
                    logger.info('Не удалось обновить карточку матча %s: %s', match_id, exc)
            except Exception:
                logger.exception('Не удалось обновить карточку матча %s', match_id)


async def command(message: Message) -> None:
    if not message.bot or not message.from_user or message.chat.type != 'private':
        return
    url = settings.webhook_base_url.rstrip('/') + '/match/create'
    await message.answer('Создайте матч, затем выберите чат для публикации.', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Создать матч', web_app=WebAppInfo(url=url))
    ]]))


async def query_handler(query: InlineQuery) -> None:
    matches = await db.list_upcoming_matches(int(time.time()), int(time.time()) + 366 * 86400)
    results: list[InlineQueryResultUnion] = []
    for match in matches:
        if match['created_by'] != query.from_user.id:
            continue
        players = await db.get_match_players(match['id'])
        results.append(InlineQueryResultArticle(
            id=f"match:{match['id']}", title=match['title'],
            description=datetime.fromtimestamp(match['start_at'], settings.timezone).strftime('%d.%m · %H:%M'),
            input_message_content=InputTextMessageContent(message_text=card(match, players), parse_mode='HTML'),
            reply_markup=keyboard(match['id']),
        ))
        if len(results) == 50:
            break
    await query.answer(results, cache_time=0, is_personal=True)


async def chosen_handler(result: ChosenInlineResult, bot) -> None:
    if not result.inline_message_id or not result.result_id.startswith('match:'):
        return
    try:
        match_id = int(result.result_id.split(':', 1)[1])
    except ValueError:
        return
    match = await db.get_match(match_id)
    if match is None or match['created_by'] != result.from_user.id:
        return
    await db.save_match_inline_message(match_id, result.inline_message_id)
    await refresh(bot, match_id)


async def request_join(callback: CallbackQuery, bot) -> None:
    try:
        match_id = int((callback.data or '').split(':', 1)[1])
    except (ValueError, IndexError):
        await callback.answer('Матч не найден', show_alert=True)
        return
    match = await db.get_match(match_id)
    if match is None or match['deleted_at'] or match['start_at'] <= int(time.time()):
        await callback.answer('Матч недоступен', show_alert=True)
        return
    if match['players_count'] >= match['max_players']:
        await callback.answer('Мест нет', show_alert=True)
        return
    if callback.inline_message_id:
        await db.save_match_inline_message(match_id, callback.inline_message_id)
    from .bot import send_temporary_notification
    try:
        await send_temporary_notification(bot, callback.from_user.id, f"Записаться на «{html.escape(match['title'])}»?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='Подтвердить', callback_data=f'match_confirm:{match_id}'),
            InlineKeyboardButton(text='Отмена', callback_data='match_cancel'),
        ]]))
    except (TelegramForbiddenError, TelegramBadRequest):
        await callback.answer('Сначала откройте бота и нажмите «Старт», затем вернитесь к карточке.', show_alert=True)
        return
    except Exception:
        logger.exception('Не удалось запросить подтверждение записи')
        await callback.answer('Не удалось отправить подтверждение. Попробуйте позже.', show_alert=True)
        return
    await callback.answer('Подтвердите запись в личном сообщении бота.', show_alert=True)


async def confirm_join(callback: CallbackQuery, bot) -> None:
    if not isinstance(callback.message, Message) or callback.message.chat.type != 'private':
        await callback.answer('Подтвердите запись в личном чате с ботом.', show_alert=True)
        return
    try:
        match_id = int((callback.data or '').split(':', 1)[1])
    except (ValueError, IndexError):
        await callback.answer('Матч не найден', show_alert=True)
        return
    # Используем тот же путь, проверки и лимиты, что и приложение.
    from . import main
    user = await db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer('Сначала нажмите «Старт» в боте.', show_alert=True)
        return
    try:
        result = await main.api_join_match(match_id, dict(user))
    except HTTPException:
        await callback.answer('Не удалось записаться. Попробуйте позже.', show_alert=True)
        return
    if not isinstance(result, dict):
        await callback.answer('Не удалось записаться: проверьте места, дату и профиль.', show_alert=True)
        return
    status = result.get('status')
    if status not in {'booked', 'pending'}:
        await callback.answer('Запись недоступна. Проверьте свой статус в приложении.', show_alert=True)
        return
    await callback.message.edit_text('Заявка отправлена организатору.' if status == 'pending' else 'Вы записаны на матч.')
    await callback.answer()
    await refresh(bot, match_id)


async def cancel_join(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message) and callback.message.chat.type == 'private':
        await callback.message.edit_text('Запись отменена.')
    await callback.answer()


def register(router: Router) -> None:
    router.message.register(command, Command('match'))
    router.inline_query.register(query_handler)
    router.chosen_inline_result.register(chosen_handler)
    router.callback_query.register(request_join, F.data.startswith('match_join:'))
    router.callback_query.register(confirm_join, F.data.startswith('match_confirm:'))
    router.callback_query.register(cancel_join, F.data == 'match_cancel')
