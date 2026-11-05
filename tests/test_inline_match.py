import asyncio
import time
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import EditMessageText, SendMessage
from fastapi import HTTPException

from app import inline_match as mod
from app import main


def run(coro):
    return asyncio.run(coro)


def test_card_limits_names_and_reports_finished():
    match = {'title': 'Матч', 'start_at': int(time.time()) + 1000, 'location': '', 'max_players': 50, 'deleted_at': None}
    players = [{'status': 'booked', 'display_mode': 'name', 'first_name': f'Игрок {i}', 'telegram_id': i} for i in range(51)]
    assert 'и ещё 1' in mod.card(match, players)
    assert 'Мест нет' in mod.card(match, players)
    for player in players:
        player['first_name'] = '&' * 60
    assert len(mod.card(match, players)) < 3800
    match['deleted_at'] = 1
    assert 'Матч отменён' in mod.card(match, players)
    match['deleted_at'] = None
    match['start_at'] = 1
    assert 'Матч начался' in mod.card(match, players)


def test_command_and_inline_query_limits(monkeypatch):
    answers = []
    async def answer(*args, **kwargs): answers.append((args, kwargs))
    message = SimpleNamespace(bot=None, from_user=None, chat=SimpleNamespace(type='private'), answer=answer)
    run(mod.command(message))
    assert not answers
    monkeypatch.setattr(mod, 'settings', SimpleNamespace(webhook_base_url='https://example.com/', timezone=mod.settings.timezone))
    message.bot = object()
    message.from_user = object()
    run(mod.command(message))
    assert answers[-1][1]['reply_markup'].inline_keyboard[0][0].web_app.url == 'https://example.com/match/create'
    match = {'id': 1, 'created_by': 2, 'title': 'Матч', 'start_at': int(time.time()) + 3600, 'location': '', 'max_players': 4, 'deleted_at': None}
    async def matches(*args): return [{**match, 'created_by': 3}] + [match] * 60
    async def players(*args): return []
    async def drafts(*args): return []
    monkeypatch.setattr(mod.db, 'list_owned_match_drafts', drafts)
    monkeypatch.setattr(mod.db, 'list_upcoming_matches', matches)
    monkeypatch.setattr(mod.db, 'get_match_players', players)
    query = SimpleNamespace(from_user=SimpleNamespace(id=2), answer=answer)
    run(mod.query_handler(query))
    assert len(answers[-1][0][0]) == 50


def test_chosen_and_refresh_failures(monkeypatch):
    async def get(*args): return None
    monkeypatch.setattr(mod.db, 'get_match', get)
    bot = SimpleNamespace()
    run(mod.refresh(bot, 1))
    for item in [SimpleNamespace(inline_message_id='', result_id='match:1'), SimpleNamespace(inline_message_id='x', result_id='other'), SimpleNamespace(inline_message_id='x', result_id='match:bad'), SimpleNamespace(inline_message_id='x', result_id='match:1', from_user=SimpleNamespace(id=1))]:
        run(mod.chosen_handler(item, bot))
    match = {'id': 1, 'created_by': 2, 'title': 'Матч', 'start_at': int(time.time()) + 3600, 'location': '', 'max_players': 4, 'players_count': 0, 'deleted_at': None}
    async def get_match(*args): return match
    async def refs(*args): return [{'inline_message_id': 'one'}, {'inline_message_id': 'two'}, {'inline_message_id': 'three'}]
    async def players(*args): return []
    async def edit(**kwargs):
        value = kwargs['inline_message_id']
        if value == 'one': raise TelegramBadRequest(method=EditMessageText(inline_message_id=value, text='x'), message='message is not modified')
        if value == 'two': raise TelegramBadRequest(method=EditMessageText(inline_message_id=value, text='x'), message='other error')
        raise RuntimeError('network')
    monkeypatch.setattr(mod.db, 'get_match', get_match)
    monkeypatch.setattr(mod.db, 'list_match_inline_messages', refs)
    monkeypatch.setattr(mod.db, 'get_match_players', players)
    run(mod.refresh(SimpleNamespace(edit_message_text=edit), 1))
    match['created_by'] = 3
    run(mod.chosen_handler(SimpleNamespace(inline_message_id='x', result_id='match:1', from_user=SimpleNamespace(id=2)), bot))


def test_join_failures_and_cancel(monkeypatch):
    answers, edits = [], []
    async def answer(*args, **kwargs): answers.append(args)
    async def edit_text(value): edits.append(value)
    monkeypatch.setattr(mod, 'Message', SimpleNamespace)
    callback = SimpleNamespace(data='bad', from_user=SimpleNamespace(id=1), inline_message_id=None, message=SimpleNamespace(chat=SimpleNamespace(type='private'), edit_text=edit_text), answer=answer)
    bot = SimpleNamespace()
    run(mod.request_join(callback, bot))
    async def get(*args): return None
    monkeypatch.setattr(mod.db, 'get_match', get)
    callback.data = 'match_join:1'
    run(mod.request_join(callback, bot))
    match = {'id': 1, 'title': 'Матч', 'start_at': int(time.time()) + 3600, 'deleted_at': None, 'players_count': 0, 'max_players': 2}
    async def get_match(*args): return match
    async def forbidden(*args, **kwargs): raise TelegramForbiddenError(method=SendMessage(chat_id=1, text='x'), message='blocked')
    monkeypatch.setattr(mod.db, 'get_match', get_match)
    run(mod.request_join(callback, SimpleNamespace(send_message=forbidden)))
    assert 'Старт' in answers[-1][0]
    async def network_error(*args, **kwargs): raise RuntimeError('network')
    run(mod.request_join(callback, SimpleNamespace(send_message=network_error)))
    assert 'Попробуйте позже' in answers[-1][0]
    callback.message.chat.type = 'group'
    run(mod.confirm_join(callback, bot))
    callback.message.chat.type = 'private'
    callback.data = 'bad'
    run(mod.confirm_join(callback, bot))
    callback.data = 'match_confirm:1'
    monkeypatch.setattr(mod.db, 'get_user', get)
    run(mod.confirm_join(callback, bot))
    async def user(*args): return {'telegram_id': 1}
    monkeypatch.setattr(mod.db, 'get_user', user)
    async def limited(*args): raise HTTPException(status_code=429)
    monkeypatch.setattr(main, 'api_join_match', limited)
    run(mod.confirm_join(callback, bot))
    async def failed(*args): return None
    monkeypatch.setattr(main, 'api_join_match', failed)
    run(mod.confirm_join(callback, bot))
    async def rejected(*args): return {'status': 'rejected'}
    monkeypatch.setattr(main, 'api_join_match', rejected)
    run(mod.confirm_join(callback, bot))
    run(mod.cancel_join(callback))
    assert edits == ['Запись отменена.']


def test_group_command_offers_private_start():
    answers = []
    async def me(): return SimpleNamespace(username='ExampleBot')
    async def answer(*args, **kwargs): answers.append(kwargs)
    message = SimpleNamespace(bot=SimpleNamespace(me=me), from_user=object(), chat=SimpleNamespace(type='group'), answer=answer)
    run(mod.command(message))
    assert answers[0]['reply_markup'].inline_keyboard[0][0].url == 'https://t.me/ExampleBot?start=match'


def test_confirmation_reports_success_when_message_edit_fails(monkeypatch):
    answers, refreshed = [], []
    async def answer(*args, **kwargs): answers.append(args[0])
    async def edit_text(text): raise RuntimeError('Telegram edit failed')
    async def user(*args): return {'telegram_id': 1}
    async def join(*args): return {'ok': True, 'status': 'booked'}
    async def refresh(*args): refreshed.append(True)
    monkeypatch.setattr(mod, 'Message', SimpleNamespace)
    monkeypatch.setattr(mod.db, 'get_user', user)
    monkeypatch.setattr(main, 'api_join_match', join)
    monkeypatch.setattr(mod, 'refresh', refresh)
    callback = SimpleNamespace(data='match_confirm:1', from_user=SimpleNamespace(id=1), message=SimpleNamespace(chat=SimpleNamespace(type='private'), edit_text=edit_text), answer=answer)
    run(mod.confirm_join(callback, object()))
    assert answers == ['Вы записаны на матч.']
    assert refreshed == [True]


def test_refresh_handles_draft_deleted_during_recovery(monkeypatch):
    calls = []
    async def get(*args):
        calls.append(True)
        return {'id': 1, 'published': 0} if len(calls) == 1 else None
    async def refs(*args): return [{'inline_message_id': 'x'}]
    async def publish(*args): pass
    monkeypatch.setattr(mod.db, 'get_match', get)
    monkeypatch.setattr(mod.db, 'list_match_inline_messages', refs)
    monkeypatch.setattr(mod, 'publish_draft', publish)
    run(mod.refresh(object(), 1))
    assert len(calls) == 2


def test_confirmation_updates_message_even_if_callback_expired(monkeypatch):
    edits = []
    async def answer(*args, **kwargs): raise RuntimeError('callback expired')
    async def edit_text(text): edits.append(text)
    async def user(*args): return {'telegram_id': 1}
    async def join(*args): return {'ok': True, 'status': 'booked'}
    async def refresh(*args): pass
    monkeypatch.setattr(mod, 'Message', SimpleNamespace)
    monkeypatch.setattr(mod.db, 'get_user', user)
    monkeypatch.setattr(main, 'api_join_match', join)
    monkeypatch.setattr(mod, 'refresh', refresh)
    callback = SimpleNamespace(data='match_confirm:1', from_user=SimpleNamespace(id=1), message=SimpleNamespace(chat=SimpleNamespace(type='private'), edit_text=edit_text), answer=answer)
    run(mod.confirm_join(callback, object()))
    assert edits == ['Вы записаны на матч.']
