"""
Точка входа приложения: FastAPI (HTTP/Web App API) + aiogram (Telegram-бот).

Один монолитный процесс обслуживает:
  * Telegram-бота (polling или webhook);
  * REST API Web App;
  * HTML-страницы (Jinja2 + собственный CSS).
"""

from __future__ import annotations

import asyncio
import hmac
import html
import logging
import re
import time
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlencode

import aiosqlite
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import bot as bot_module
from . import database as db
from .avatars import (
    AVATAR_URL_TTL_SECONDS,
    AvatarCache,
    avatar_version,
    sniff_media_type,
)
from .config import (
    BOOKING_CANCEL_LOCK_MINUTES,
    DEFAULT_REMINDER_OFFSET_MINUTES,
    DEFAULT_TRAINING_DURATION_MINUTES,
    TRAINING_LEVELS,
    settings,
)
from .identity import animal_identity
from .rate_limit import RateLimiter
from .secure_auth import (
    InitDataError,
    issue_scoped_token,
    validate_init_data,
    verify_scoped_token,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("squash")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Аватарки: диск-кэш создаётся в lifespan. Ограничение на размер одного файла,
# чтобы повреждённый/подменённый file_id не съел память.
_AVATAR_MAX_BYTES = 10 * 1024 * 1024


_bot_check_cache: tuple[float, bool] = (0.0, False)
_BOT_CHECK_TTL_SECONDS = 60
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    """Фоновая задача с гашением ошибок и отменой при остановке приложения."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _finished(done: asyncio.Task) -> None:
        _background_tasks.discard(done)
        with suppress(asyncio.CancelledError, Exception):
            done.exception()

    task.add_done_callback(_finished)

# Защита от спама/перебора. Лимиты подобраны для небольшого клуба.
_message_limiter = RateLimiter(20, 60)      # личные сообщения тренеру
_apply_limiter = RateLimiter(5, 300)        # заявки в тренеры
_booking_limiter = RateLimiter(60, 60)      # book/cancel
_match_create_limiter = RateLimiter(3, 600) # публикация матчей
_match_join_limiter = RateLimiter(30, 60)   # вход/выход из матчей
_feedback_limiter = RateLimiter(3, 600)     # баги и предложения


async def _bot_is_ready() -> bool:
    """Проверяет доступность Telegram Bot API с кэшем, чтобы не дёргать его зря."""
    global _bot_check_cache
    now = time.monotonic()
    cached_at, cached_ok = _bot_check_cache
    if cached_at and now - cached_at < _BOT_CHECK_TTL_SECONDS:
        return cached_ok
    try:
        me = await app.state.bot.get_me()
        ok = me is not None
    except Exception:
        logger.warning("Проверка Telegram Bot API не прошла", exc_info=True)
        ok = False
    _bot_check_cache = (now, ok)
    return ok


def _avatar_store() -> AvatarCache:
    """Возвращает диск-кэш аватарок (создаёт лениво для тестов/скриптов)."""
    store = getattr(app.state, "avatar_cache", None)
    if not isinstance(store, AvatarCache):
        store = AvatarCache(settings.avatar_cache_dir)
        app.state.avatar_cache = store
    return store


# --------------------------------------------------------------------------- #
# Обработчики ошибок
# --------------------------------------------------------------------------- #

def error_json(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": message})


# --------------------------------------------------------------------------- #
# Аутентификация Web App
# --------------------------------------------------------------------------- #

async def _authenticate(init_data: str) -> dict[str, Any]:
    """Проверяет подпись initData и возвращает карточку игрока из БД."""
    try:
        data = validate_init_data(init_data, settings.bot_token)
    except InitDataError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    tg_user = data["user"]
    try:
        tg_id = int(tg_user["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Некорректный telegram_id.") from exc

    is_owner = tg_id == settings.coach_telegram_id
    is_coach = is_owner or await db.is_coach_id(tg_id)
    username = tg_user.get("username")
    first_name = tg_user.get("first_name")

    # Пишем в БД только при реальном изменении профиля: иначе каждый GET
    # становится write-транзакцией (SQLite сериализует запись).
    row = await db.get_user(tg_id)
    if (
        row is None
        or row["username"] != username
        or row["first_name"] != first_name
        or bool(row["is_coach"]) != is_coach
    ):
        await db.upsert_user(
            telegram_id=tg_id,
            username=username,
            first_name=first_name,
            is_coach=is_coach,
        )
        row = await db.get_user(tg_id)
    if row is None:  # теоретически недостижимо
        raise HTTPException(status_code=500, detail="Профиль не найден.")
    user = dict(row)
    user["is_coach"] = is_coach  # источник истины — реестр тренеров
    user["is_owner"] = is_owner
    user["is_manager"] = is_owner or await db.is_manager_id(tg_id)
    return user


async def current_user(
    x_init_data: Annotated[str, Header(alias="X-Init-Data")] = "",
) -> dict[str, Any]:
    """
    FastAPI-зависимость: валидирует подпись initData и возвращает карточку игрока.
    Без корректной подписи Telegram доступ к API невозможен.
    """
    return await _authenticate(x_init_data)


def _avatar_url(
    telegram_id: int, *, real: bool = False, file_id: str | None = None
) -> str:
    """
    Возвращает URL аватарки с узким токеном и версией.

    ``real=True`` — только для ответов самому пользователю и тренерам:
    такой токен обходит режим «животное». Публичные карточки используют
    обычный токен, который уважает настройку приватности.

    ``file_id`` превращается в версию: при смене фото URL меняется сам,
    а всё остальное время остаётся одинаковым и кэшируется браузером.
    """
    kind = "avatar_real" if real else "avatar"
    token = issue_scoped_token(
        settings.bot_token,
        kind,
        telegram_id,
        telegram_id,
        ttl_seconds=AVATAR_URL_TTL_SECONDS,
    )
    version = avatar_version(settings.bot_token, telegram_id, file_id)
    query = urlencode({"token": token})
    return f"/api/avatar/{telegram_id}/{version}?{query}"


def _row_file_id(row: Any) -> str | None:
    """file_id аватарки из строки БД; в урезанных dict колонки может не быть."""
    # keys() нужен именно для aiosqlite.Row: `in row` проверяет значения.
    return row["avatar_file_id"] if "avatar_file_id" in row.keys() else None  # noqa: SIM118


async def require_coach(user: Annotated[dict, Depends(current_user)]) -> dict:
    """Зависимость для эндпоинтов, доступных только тренеру."""
    if not user.get("is_coach"):
        raise HTTPException(status_code=403, detail="Доступ только для тренера.")
    return user


async def require_owner(user: Annotated[dict, Depends(current_user)]) -> dict:
    """Зависимость для эндпоинтов управления тренерами (только владелец)."""
    if not user.get("is_owner"):
        raise HTTPException(status_code=403, detail="Доступ только для владельца.")
    return user


async def require_manager(user: Annotated[dict, Depends(current_user)]) -> dict:
    """Владелец или тренер с правами менеджера (заявки, реестр)."""
    if not user.get("is_manager"):
        raise HTTPException(status_code=403, detail="Недостаточно прав.")
    return user


# --------------------------------------------------------------------------- #
# Сериализация
# --------------------------------------------------------------------------- #

def _local_time(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=settings.timezone)


def _row_status(row: Any, default: str = "booked") -> str:
    """Статус участника с совместимостью со старыми/тестовыми строками."""
    try:
        return str(row["status"] or default)
    except (KeyError, IndexError, TypeError):
        return default


def _row_value(row: Any, key: str, default: Any = "") -> Any:
    """Поле sqlite Row/dict; отсутствующее или NULL значение даёт default."""
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def serialize_training(
    row: aiosqlite.Row, *, viewer_id: int | None = None
) -> dict[str, Any]:
    start = _local_time(row["start_at"])
    keys = set(row.keys())
    data: dict[str, Any] = {
        "id": row["id"],
        "title": row["title"],
        "type": row["training_type"],
        "start_at": row["start_at"],
        "date": start.strftime("%Y-%m-%d"),
        "time": start.strftime("%H:%M"),
        "datetime_local": start.strftime("%d.%m.%Y %H:%M"),
        "duration_min": row["duration_min"],
        "capacity": row["capacity"],
        "level": row["level"] if "level" in keys else "",
        "booked_count": row["booked_count"] if "booked_count" in keys else 0,
        "waitlist_count": row["waitlist_count"] if "waitlist_count" in keys else 0,
        "reminder_enabled": bool(row["reminder_enabled"]),
        "reminder_offset_min": row["reminder_offset_min"],
        "deleted": bool(row["deleted_at"]) if "deleted_at" in keys else False,
        "can_self_cancel": True,
        "is_booked": False,
        "is_waitlisted": False,
        "is_pending": False,
    }
    seconds_left = row["start_at"] - int(time.time())
    data["is_past"] = seconds_left <= 0
    data["seconds_left"] = seconds_left
    data["cancel_lock_minutes"] = BOOKING_CANCEL_LOCK_MINUTES
    # Правило: самостоятельная отмена разрешена только если до старта >= 4.5 ч.
    data["can_self_cancel"] = seconds_left >= BOOKING_CANCEL_LOCK_MINUTES * 60
    if viewer_id is not None:
        # Токен для скачивания .ics: обычная ссылка не может передать заголовок.
        data["ics_token"] = issue_scoped_token(
            settings.bot_token, "ics", row["id"], viewer_id, ttl_seconds=86400
        )
    return data


def participant_identity(row: aiosqlite.Row) -> dict[str, Any]:
    """
    Имя/аватар для списка участников.

    В режиме «животное» реальные имя и фото не показываются ни игрокам,
    ни тренеру: тренер тоже видит стабильное вымышленное животное.
    """
    if row["display_mode"] == "animal":
        animal = animal_identity(row["telegram_id"])
        return {
            "first_name": animal["name"],
            "avatar_url": "",
            "avatar_emoji": animal["emoji"],
            "anonymous": True,
            "profile_token": issue_scoped_token(
                settings.bot_token, "profile", 0, row["telegram_id"], ttl_seconds=86400
            ),
        }
    return {
        "first_name": row["first_name"] or "Игрок",
        "avatar_url": _avatar_url(
            row["telegram_id"], file_id=_row_file_id(row)
        ),
        "avatar_emoji": "",
        "anonymous": False,
        "profile_token": issue_scoped_token(
            settings.bot_token, "profile", 0, row["telegram_id"], ttl_seconds=86400
        ),
    }


def serialize_participant(row: aiosqlite.Row) -> dict[str, Any]:
    """Участник для «социальной сетки» — без username и telegram_id."""
    data = participant_identity(row)
    data["status"] = row["status"]
    return data


def serialize_match(
    row: aiosqlite.Row,
    participants: list[aiosqlite.Row],
    creator_row: aiosqlite.Row | None,
    viewer_id: int,
    is_coauthor: bool = False,
) -> dict[str, Any]:
    """Публичная карточка матча без Telegram ID участников."""
    start = _local_time(row["start_at"])
    booked = [p for p in participants if _row_status(p) == "booked"]
    pending = [p for p in participants if _row_status(p) == "pending"]
    participant_data = [
        {
            **participant_identity(p),
            "joined_at": p["created_at"],
            "is_organizer": p["telegram_id"] == row["created_by"],
        }
        for p in booked
    ]
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"] or "",
        "location": row["location"] or "",
        "start_at": row["start_at"],
        "date": start.strftime("%Y-%m-%d"),
        "time": start.strftime("%H:%M"),
        "datetime_local": start.strftime("%d.%m.%Y %H:%M"),
        "duration_min": row["duration_min"],
        "max_players": row["max_players"],
        "players_count": int(row["players_count"] or 0),
        "level": row["level"] or "",
        "is_joined": any(p["telegram_id"] == viewer_id for p in booked),
        "is_pending": any(p["telegram_id"] == viewer_id for p in pending),
        "is_creator": row["created_by"] == viewer_id,
        "is_coauthor": is_coauthor,
        "can_manage": row["created_by"] == viewer_id or is_coauthor,
        "pending_count": len(pending),
        "pending_requests": (
            [
                {
                    **participant_identity(p),
                    "telegram_id": p["telegram_id"],
                    "joined_at": p["created_at"],
                }
                for p in pending
            ]
            if row["created_by"] == viewer_id or is_coauthor
            else []
        ),
        "creator": participant_identity(creator_row) if creator_row else None,
        "participants": participant_data,
    }


def serialize_coach(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    """Публичный профиль тренера для карточки тренировки (без username)."""
    if row is None:
        return None
    return {
        "telegram_id": row["telegram_id"],
        "first_name": row["first_name"] or "Тренер",
        "avatar_url": _avatar_url(
            row["telegram_id"], real=True, file_id=_row_file_id(row)
        ),
        "bio": row["bio"] or "",
    }


def sanitize_bio(value: str) -> str:
    """Обрезает текст профиля (био тренера или «о себе» игрока)."""
    return (value or "").strip()[:450]


def normalize_phone(value: str) -> str:
    """Оставляет цифры и ведущий плюс; проверяет длину номера."""
    raw = (value or "").strip()
    cleaned = re.sub(r"[^\d+]", "", raw)
    plus = cleaned.startswith("+")
    digits = cleaned[1:] if plus else cleaned
    if not digits.isdigit() or not 10 <= len(digits) <= 15:
        raise ValueError("Введите телефон из 10–15 цифр.")
    return ("+" if plus else "") + digits


def _phone_required_for_new_events() -> bool:
    moment = settings.phone_required_from
    return moment is not None and datetime.now(tz=settings.timezone) >= moment


def _escape_ics(value: str) -> str:
    """Экранирование спецсимволов по RFC 5545."""
    return (
        (value or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold_ics_line(line: str, limit: int = 75) -> str:
    """Складывает длинную строку iCalendar по RFC 5545 (75 октетов)."""
    if len(line.encode("utf-8")) <= limit:
        return line
    parts: list[str] = []
    current: list[str] = []
    current_len = 0
    for char in line:
        char_len = len(char.encode("utf-8"))
        max_len = limit if not parts else limit - 1  # continuation starts with space
        if current and current_len + char_len > max_len:
            parts.append("".join(current))
            current = [char]
            current_len = char_len
        else:
            current.append(char)
            current_len += char_len
    if current:
        parts.append("".join(current))
    return "\r\n ".join(parts)


def build_ics(training: aiosqlite.Row) -> str:
    """Формирует iCalendar-файл (RFC 5545) для добавления в календарь."""
    start = datetime.fromtimestamp(training["start_at"], tz=UTC)
    end = start + timedelta(minutes=training["duration_min"])
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    fmt = lambda d: d.strftime("%Y%m%dT%H%M%SZ")

    summary = _escape_ics(training["training_type"] or "Сквош-тренировка")
    keys = set(training.keys())
    booked_count = training["booked_count"] if "booked_count" in keys else 0
    description = _escape_ics(
        f"Сквош-тренировка · {training['duration_min']} мин · записано "
        f"{booked_count} из {training['capacity']}"
    )

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//SquashBot//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:squash-training-{training['id']}@squashbot",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{fmt(start)}",
        f"DTEND:{fmt(end)}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{description}",
        # Напоминание за 30 минут средствами календаря.
        "BEGIN:VALARM",
        "TRIGGER:-PT30M",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{summary}",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "".join(f"{_fold_ics_line(line)}\r\n" for line in lines)


# --------------------------------------------------------------------------- #
# Модели запросов
# --------------------------------------------------------------------------- #

class TrainingIn(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    time: str = Field(pattern=r"^\d{2}:\d{2}$")
    training_type: str = Field(default="Тренировка", min_length=1, max_length=100)
    capacity: int = Field(default=4, ge=1, le=100)
    duration_min: int = Field(default=DEFAULT_TRAINING_DURATION_MINUTES, ge=15, le=600)
    level: str = Field(default="", max_length=60)
    reminder_enabled: bool = True
    reminder_offset_min: int = Field(default=DEFAULT_REMINDER_OFFSET_MINUTES, ge=0, le=2880)
    force: bool = False  # подтверждение при возможном дубле


class TrainingBatchIn(BaseModel):
    period_start: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    period_end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    weekdays: list[int] = Field(min_length=1, max_length=7)
    time: str = Field(pattern=r"^\d{2}:\d{2}$")
    training_type: str = Field(default="Тренировка", min_length=1, max_length=100)
    capacity: int = Field(default=4, ge=1, le=100)
    duration_min: int = Field(default=DEFAULT_TRAINING_DURATION_MINUTES, ge=15, le=600)
    level: str = Field(default="", max_length=60)
    reminder_enabled: bool = True
    reminder_offset_min: int = Field(default=DEFAULT_REMINDER_OFFSET_MINUTES, ge=0, le=2880)


class ReminderIn(BaseModel):
    enabled: bool
    offset_min: int = Field(default=DEFAULT_REMINDER_OFFSET_MINUTES, ge=0, le=2880)


class NotesIn(BaseModel):
    notes: str = Field(default="", max_length=4000)


class NotificationsIn(BaseModel):
    enabled: bool | None = None
    notify_matches: bool | None = None
    notify_trainings: bool | None = None
    notify_messages: bool | None = None


class FeedbackIn(BaseModel):
    kind: Literal["bug", "idea"]
    text: str = Field(max_length=1500)
    page: str = Field(default="", max_length=40)


class TrainerProfileIn(BaseModel):
    bio: str = Field(default="", max_length=450)


class PlayerProfileIn(BaseModel):
    about: str = Field(default="", max_length=450)
    display_mode: Literal["name", "animal"] = "name"
    show_username: bool = False
    phone: str | None = Field(default=None, max_length=32)
    notifications_enabled: bool | None = None
    notify_matches: bool | None = None
    notify_trainings: bool | None = None
    notify_messages: bool | None = None


class CoachIn(BaseModel):
    telegram_id: int | None = None
    username: str | None = Field(default=None, max_length=64)


class CoauthorIn(BaseModel):
    telegram_id: int | None = None
    username: str | None = Field(default=None, max_length=64)


class ManagerIn(BaseModel):
    enabled: bool


class ApplyIn(BaseModel):
    message: str = Field(default="", max_length=1000)


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    training_id: int | None = None


class MatchIn(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    time: str = Field(pattern=r"^\d{2}:\d{2}$")
    title: str = Field(default="Матч", min_length=1, max_length=100)
    description: str = Field(default="", max_length=450)
    location: str = Field(default="", max_length=120)
    duration_min: int = Field(default=60, ge=15, le=600)
    max_players: int = Field(default=4, ge=2, le=20)
    level: str = Field(default="", max_length=60)


async def _notifications_allowed(telegram_id: int, category: str) -> bool:
    """Учитывает общий тумблер и категорию уведомлений."""
    return await db.notification_enabled(telegram_id, category)


async def _can_manage_training(training: aiosqlite.Row, user_id: int) -> bool:
    return training["created_by"] == user_id or await db.is_event_coauthor(
        "training", training["id"], user_id
    )


async def _can_manage_match(match: aiosqlite.Row, user_id: int) -> bool:
    return match["created_by"] == user_id or await db.is_event_coauthor(
        "match", match["id"], user_id
    )


async def _resolve_user_id(payload: CoachIn | CoauthorIn) -> int | None:
    if payload.telegram_id is not None:
        return payload.telegram_id
    if payload.username:
        found = await db.find_user_by_username(payload.username)
        return int(found["telegram_id"]) if found is not None else None
    return None


_TRAINING_BATCH_MAX_DAYS = 92
_TRAINING_BATCH_MAX_COUNT = 100


def _parse_local_epoch(date_str: str, time_str: str) -> int:
    """Локальные дата+время (в часовом поясе тренера) -> Unix UTC."""
    try:
        local = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Некорректные дата/время.") from exc
    return int(local.replace(tzinfo=settings.timezone).timestamp())


# --------------------------------------------------------------------------- #
# Жизненный цикл приложения
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Готовим директорию данных.
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)

    # 2. Инициализируем БД.
    await db.connect(str(settings.db_path))

    # 3. Гарантируем владельца в реестре тренеров и его карточку в CRM.
    await db.ensure_coach(settings.coach_telegram_id)
    await db.upsert_user(
        telegram_id=settings.coach_telegram_id,
        username=None,
        first_name="Тренер",
        is_coach=True,
    )

    # 4. Поднимаем бота.
    tg_bot = bot_module.create_bot()
    dp = bot_module.create_dispatcher()
    app.state.bot = tg_bot
    app.state.dp = dp
    app.state.avatar_cache = AvatarCache(settings.avatar_cache_dir)
    await app.state.avatar_cache.load_disk_usage()

    tasks: list[asyncio.Task] = []
    polling_ctx = None

    if settings.bot_mode == "webhook":
        url = f"{settings.webhook_base_url.rstrip('/')}/telegram/webhook"
        await tg_bot.set_webhook(
            url, secret_token=settings.webhook_secret or None, drop_pending_updates=True
        )
        logger.info("Webhook установлен: %s", url)
    else:
        polling_ctx = bot_module.polling_context(tg_bot, dp)
        await polling_ctx.__aenter__()
        logger.info("Запущен long-polling.")

    # 5. Закреплённые правила для существующих пользователей.
    _spawn(bot_module.ensure_rules_for_all(tg_bot))

    # 6. Фоновые планировщики напоминаний и уведомлений.
    tasks.append(asyncio.create_task(bot_module.reminder_loop(tg_bot)))
    tasks.append(asyncio.create_task(_training_notifications_loop()))

    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        background = list(_background_tasks)
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        _background_tasks.clear()
        if polling_ctx is not None:
            await polling_ctx.__aexit__(None, None, None)
        await tg_bot.session.close()
        await db.close()


app = FastAPI(
    title="Squash Training Web App",
    lifespan=lifespan,
    docs_url="/docs" if settings.enable_api_docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.enable_api_docs else None,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Базовые security-заголовки; CSP разрешает только Telegram и свой origin."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors https://telegram.org https://web.telegram.org "
        "https://*.telegram.org https://*.t.me",
    )
    if request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "public, max-age=604800")
    elif request.url.path in {"/", "/coach"}:
        # HTML обновляется чаще статики; не даём WebView закэшировать старую
        # версию страницы со ссылкой на внешний telegram.org.
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


# --------------------------------------------------------------------------- #
# HTML-страницы
# --------------------------------------------------------------------------- #

@app.get("/", response_class=HTMLResponse)
async def page_player(request: Request):
    return templates.TemplateResponse(
        request, "index.html", {"allow_dev_init_data": settings.allow_dev_init_data}
    )


@app.get("/coach", response_class=HTMLResponse)
async def page_coach(request: Request):
    return templates.TemplateResponse(
        request, "coach.html", {"allow_dev_init_data": settings.allow_dev_init_data}
    )


@app.get("/match", response_class=HTMLResponse)
async def page_match(request: Request):
    return templates.TemplateResponse(
        request, "match.html", {"allow_dev_init_data": settings.allow_dev_init_data}
    )


@app.get("/healthz")
async def healthz():
    """Liveness: процесс жив и отвечает на HTTP."""
    return {"ok": True}


@app.get("/readyz")
async def readyz():
    """Readiness: живы БД и Telegram Bot API (валидный BOT_TOKEN)."""
    if not await db.ping():
        raise HTTPException(status_code=503, detail="База данных недоступна.")
    if not await _bot_is_ready():
        raise HTTPException(status_code=503, detail="Telegram Bot API недоступен.")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# API: профиль и настройки
# --------------------------------------------------------------------------- #

@app.get("/api/me")
async def api_me(user: Annotated[dict, Depends(current_user)]):
    return {
        "ok": True,
        "user": {
            "telegram_id": user["telegram_id"],
            "first_name": user["first_name"],
            "is_coach": bool(user["is_coach"]),
            "is_owner": bool(user.get("is_owner")),
            "is_manager": bool(user.get("is_manager")),
            "notifications_enabled": bool(user["notifications_enabled"]),
            "phone": user["phone"] or "",
            "notify_matches": bool(user["notify_matches"]),
            "notify_trainings": bool(user["notify_trainings"]),
            "notify_messages": bool(user["notify_messages"]),
            "avatar_url": _avatar_url(
                user["telegram_id"], real=True, file_id=_row_file_id(user)
            ),
            "about": user["about"] or "",
            "display_mode": user["display_mode"] or "name",
            "show_username": bool(user["show_username"]),
            "onboarding_seen": bool(user["onboarding_seen"]),
            "animal": animal_identity(user["telegram_id"]),
        },
    }


@app.get("/api/players/{token}")
async def api_player_profile(
    token: str, user: Annotated[dict, Depends(current_user)]
):
    """Публичный профиль игрока по узкому токену (без Telegram ID)."""
    target_id = verify_scoped_token(settings.bot_token, token, "profile", 0)
    if target_id is None:
        raise HTTPException(status_code=401, detail="Ссылка на профиль недействительна.")
    row = await db.get_user(target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Профиль не найден.")
    identity = participant_identity(row)
    return {
        "ok": True,
        "profile": {
            **identity,
            "about": row["about"] or "",
            "username": row["username"] if row["show_username"] else None,
            "username_visible": bool(row["show_username"]),
        },
    }


@app.post("/api/me/onboarding")
async def api_onboarding_seen(user: Annotated[dict, Depends(current_user)]):
    """Отмечает памятку первого запуска как просмотренную."""
    await db.set_onboarding_seen(user["telegram_id"])
    return {"ok": True}


@app.put("/api/me/profile")
async def api_set_player_profile(
    payload: PlayerProfileIn,
    user: Annotated[dict, Depends(current_user)],
):
    """Сохраняет «о себе» и настройки показа в списке участников."""
    try:
        phone = None
        if payload.phone is not None:
            phone = normalize_phone(payload.phone) if payload.phone.strip() else ""
        await db.set_player_profile(
            user["telegram_id"],
            about=sanitize_bio(payload.about),
            display_mode=payload.display_mode,
            show_username=payload.show_username,
            phone=phone,
            notifications_enabled=payload.notifications_enabled,
            notify_matches=payload.notify_matches,
            notify_trainings=payload.notify_trainings,
            notify_messages=payload.notify_messages,
        )
        updated = await db.get_user(user["telegram_id"])
        if updated is None:
            raise HTTPException(status_code=500, detail="Профиль не найден.")
        return {
            "ok": True,
            "profile": {
                "about": updated["about"] or "",
                "display_mode": updated["display_mode"] or "name",
                "show_username": bool(updated["show_username"]),
                "phone": updated["phone"] or "",
                "notifications_enabled": bool(updated["notifications_enabled"]),
                "notify_matches": bool(updated["notify_matches"]),
                "notify_trainings": bool(updated["notify_trainings"]),
                "notify_messages": bool(updated["notify_messages"]),
                "animal": animal_identity(user["telegram_id"]),
            },
        }
    except ValueError as exc:
        return error_json(str(exc), 422)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Ошибка сохранения профиля игрока")
        return error_json("Внутренняя ошибка сервера", 500)


@app.put("/api/me/notifications")
async def api_set_notifications(
    payload: NotificationsIn,
    user: Annotated[dict, Depends(current_user)],
):
    await db.set_notification_settings(
        user["telegram_id"],
        enabled=payload.enabled,
        matches=payload.notify_matches,
        trainings=payload.notify_trainings,
        messages=payload.notify_messages,
    )
    updated = await db.get_user(user["telegram_id"])
    if updated is None:
        raise HTTPException(status_code=500, detail="Профиль не найден.")
    return {
        "ok": True,
        "notifications_enabled": bool(updated["notifications_enabled"]),
        "notify_matches": bool(updated["notify_matches"]),
        "notify_trainings": bool(updated["notify_trainings"]),
        "notify_messages": bool(updated["notify_messages"]),
    }


@app.post("/api/feedback")
async def api_feedback(
    payload: FeedbackIn,
    user: Annotated[dict, Depends(current_user)],
):
    """Принимает сообщение о проблеме или предложение и уведомляет тренера."""
    _feedback_limiter.check(str(user["telegram_id"]))
    text = payload.text.strip()
    if len(text) < 3:
        return error_json("Опишите проблему или предложение чуть подробнее.", 422)
    page = payload.page if payload.page in {"Тренировки", "Матч", "Тренер"} else "Приложение"
    try:
        await db.add_feedback(user["telegram_id"], payload.kind, text, page)
        _spawn(_notify_feedback(user, payload.kind, text, page))
        return {"ok": True, "message": "Спасибо! Сообщение отправлено."}
    except Exception:
        logger.exception("Ошибка сохранения обратной связи")
        return error_json("Не удалось отправить сообщение", 500)


@app.get("/api/me/export")
async def api_export_me(user: Annotated[dict, Depends(current_user)]):
    """Экспорт собственных данных пользователя (profile/bookings/messages)."""
    application = await db.get_application(user["telegram_id"])
    return {
        "ok": True,
        "user": {
            "telegram_id": user["telegram_id"],
            "username": user["username"],
            "first_name": user["first_name"],
            "phone": user["phone"] or "",
            "notifications_enabled": bool(user["notifications_enabled"]),
            "notify_matches": bool(user["notify_matches"]),
            "notify_trainings": bool(user["notify_trainings"]),
            "notify_messages": bool(user["notify_messages"]),
            "notes": user["notes"],
            "about": user["about"] or "",
            "display_mode": user["display_mode"] or "name",
            "show_username": bool(user["show_username"]),
            "onboarding_seen": bool(user["onboarding_seen"]),
        },
        "bookings": [dict(row) for row in await db.list_user_bookings(user["telegram_id"])],
        "messages": [dict(row) for row in await db.list_user_messages(user["telegram_id"])],
        "feedback": [dict(row) for row in await db.list_user_feedback(user["telegram_id"])],
        "application": dict(application) if application is not None else None,
    }


@app.delete("/api/me")
async def api_delete_me(
    user: Annotated[dict, Depends(current_user)],
    confirm: bool = False,
):
    """
    Удаляет профиль игрока и его персональные данные.

    Требует `?confirm=true`. Тренер/владелец должен сначала снять роль:
    иначе удаление сломает владение занятиями.
    """
    if user.get("is_coach") or user.get("is_owner"):
        return error_json(
            "Сначала снимите роль тренера (занятия нужно передать другому).", 403
        )
    if not confirm:
        return error_json("Подтвердите удаление: ?confirm=true", 400)

    # Сначала отменяем активные записи, чтобы освободить места и продвинуть очередь.
    for booking in await db.list_user_bookings(user["telegram_id"]):
        if booking["status"] not in {"booked", "waitlist"}:
            continue
        if booking["deleted_at"]:
            continue
        training = await db.get_training(booking["training_id"])
        promoted = await db.cancel_booking(
            booking["training_id"], user["telegram_id"]
        )
        if training is not None and promoted:
            _spawn(_notify_promoted(int(promoted), training))

    await db.delete_user_data(user["telegram_id"])
    await _avatar_store().delete_user(user["telegram_id"])
    return {"ok": True, "deleted": True}


# --------------------------------------------------------------------------- #
# API: тренировки (игрок)
# --------------------------------------------------------------------------- #

@app.get("/api/trainings")
async def api_trainings(user: Annotated[dict, Depends(current_user)], days: int = 7):
    days = max(1, min(days, 31))
    now = int(time.time())
    until = int((datetime.now(tz=settings.timezone) + timedelta(days=days)).timestamp())
    rows = await db.list_upcoming_trainings(now, until)

    training_ids = [row["id"] for row in rows]
    participants_by_training = await db.get_participants_for_trainings(training_ids)
    my_bookings = await db.get_bookings_for_user(user["telegram_id"], training_ids)

    # У каждого занятия свой тренер — берём по создателю (created_by).
    coach_cache: dict[Any, dict[str, Any] | None] = {}
    result = []
    for row in rows:
        creator = row["created_by"]
        if creator not in coach_cache:
            coach_cache[creator] = serialize_coach(
                await db.get_user(creator) if creator is not None else None
            )
        item = serialize_training(row, viewer_id=user["telegram_id"])
        item["coach"] = coach_cache[creator]
        participants = participants_by_training.get(row["id"], [])
        item["participants"] = [
            serialize_participant(p)
            for p in participants
            if _row_status(p) == "booked"
        ]
        item["waitlist_count"] = sum(
            1 for p in participants if p["status"] == "waitlist"
        )
        # Статус текущего пользователя.
        my_status = my_bookings.get(row["id"])
        if my_status == "booked":
            item["is_booked"] = True
        elif my_status == "waitlist":
            item["is_waitlisted"] = True
        elif my_status == "pending":
            item["is_pending"] = True
        result.append(item)
    return {"ok": True, "trainings": result}


@app.post("/api/trainings/{training_id}/book")
async def api_book(training_id: int, user: Annotated[dict, Depends(current_user)]):
    _booking_limiter.check(str(user["telegram_id"]))
    try:
        training = await db.get_training(training_id)
        if training is None or training["deleted_at"]:
            return error_json("Тренировка не найдена", 404)
        if training["start_at"] <= int(time.time()):
            return error_json("Тренировка уже началась", 400)
        if _phone_required_for_new_events() and not user["phone"]:
            return error_json("Укажите телефон в настройках профиля, чтобы записаться.", 403)

        status = await db.book_training(
            training_id,
            user["telegram_id"],
            training["capacity"],
            requires_approval=settings.require_event_approval,
        )
        _spawn(
            _notify_training_event(
                training,
                user["telegram_id"],
                status,
                actor_id=user["telegram_id"],
            )
        )
        if status == "pending":
            msg = "Заявка отправлена тренеру."
        elif status == "booked":
            msg = "Вы записаны!"
        else:
            msg = "Мест нет — вы в листе ожидания."
        return {"ok": True, "status": status, "message": msg}
    except Exception:
        logger.exception("Ошибка записи на тренировку")
        return error_json("Внутренняя ошибка сервера", 500)


@app.post("/api/trainings/{training_id}/cancel")
async def api_cancel(training_id: int, user: Annotated[dict, Depends(current_user)]):
    _booking_limiter.check(str(user["telegram_id"]))
    try:
        training = await db.get_training(training_id)
        if training is None or training["deleted_at"]:
            return error_json("Тренировка не найдена", 404)

        booking = await db.get_booking(training_id, user["telegram_id"])
        if booking is None or booking["status"] not in {
            "pending", "booked", "waitlist"
        }:
            return error_json("Вы не записаны на эту тренировку", 400)

        seconds_left = training["start_at"] - int(time.time())
        # Правило: игрок не может выйти из тренировки, если до старта меньше 4.5 ч.
        if seconds_left <= 0:
            return error_json("Тренировка уже началась — отмена недоступна.", 403)
        if seconds_left < BOOKING_CANCEL_LOCK_MINUTES * 60:
            return error_json(
                "Отмена недоступна: до старта меньше 4.5 часов. Свяжитесь с тренером.", 403
            )

        promoted = await db.cancel_booking(training_id, user["telegram_id"])
        _spawn(
            _notify_training_event(
                training,
                user["telegram_id"],
                "cancelled",
                actor_id=user["telegram_id"],
            )
        )
        if promoted:
            _spawn(_notify_promoted(int(promoted), training))
        return {"ok": True, "promoted": promoted or None}
    except Exception:
        logger.exception("Ошибка отмены записи")
        return error_json("Внутренняя ошибка сервера", 500)


@app.get("/calendar/{training_id}.ics")
async def api_calendar(
    training_id: int,
    token: str = "",
    x_init_data: Annotated[str, Header(alias="X-Init-Data")] = "",
    init_data: Annotated[str, Query(alias="initData")] = "",
):
    """
    Отдаёт .ics для добавления тренировки в календарь смартфона.

    Авторизация — узким токеном из поля ``ics_token`` (выдаётся API вместе с
    занятием). Для совместимости принимается и обычный заголовок X-Init-Data.
    Полный initData в query принимается только в dev-режиме
    (``ALLOW_DEV_INIT_DATA=true``), чтобы не светить bearer-токен в логах.
    """
    allowed = False
    if token:
        allowed = (
            verify_scoped_token(settings.bot_token, token, "ics", training_id)
            is not None
        )
    elif x_init_data:
        try:
            await _authenticate(x_init_data)
            allowed = True
        except HTTPException:
            allowed = False
    elif settings.allow_dev_init_data and init_data:
        try:
            await _authenticate(init_data)
            allowed = True
        except HTTPException:
            allowed = False

    if not allowed:
        raise HTTPException(status_code=401, detail="Требуется авторизация.")

    training = await db.get_training_with_count(training_id)
    if training is None or training["deleted_at"]:
        raise HTTPException(status_code=404, detail="Тренировка не найдена.")
    ics = build_ics(training)
    return Response(
        content=ics,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="squash-{training_id}.ics"'},
    )


# --------------------------------------------------------------------------- #
# API: матчи игроков
# --------------------------------------------------------------------------- #

@app.get("/api/matches")
async def api_matches(user: Annotated[dict, Depends(current_user)], days: int = 30):
    days = max(1, min(days, 90))
    now = int(time.time())
    until = int((datetime.now(tz=settings.timezone) + timedelta(days=days)).timestamp())
    rows = await db.list_upcoming_matches(now, until)
    participants = await db.get_match_players_for_matches([r["id"] for r in rows])
    creator_cache: dict[Any, aiosqlite.Row | None] = {}
    result = []
    for row in rows:
        creator_id = row["created_by"]
        if creator_id not in creator_cache:
            creator_cache[creator_id] = await db.get_user(creator_id)
        result.append(
            serialize_match(
                row,
                participants.get(row["id"], []),
                creator_cache[creator_id],
                user["telegram_id"],
                await db.is_event_coauthor("match", row["id"], user["telegram_id"]),
            )
        )
    return {"ok": True, "matches": result}


@app.post("/api/matches")
async def api_create_match(
    payload: MatchIn, user: Annotated[dict, Depends(current_user)]
):
    _match_create_limiter.check(str(user["telegram_id"]))
    try:
        if _phone_required_for_new_events() and not user["phone"]:
            return error_json("Укажите телефон в настройках профиля, чтобы создать матч.", 403)
        start_at = _parse_local_epoch(payload.date, payload.time)
        if start_at <= int(time.time()):
            return error_json("Нельзя создать матч в прошлом", 400)
        level = (payload.level or "").strip()
        if level and level not in TRAINING_LEVELS:
            return error_json("Неизвестный уровень игры", 422)
        match_id = await db.create_match(
            title=payload.title.strip(),
            description=sanitize_bio(payload.description),
            location=(payload.location or "").strip(),
            start_at=start_at,
            duration_min=payload.duration_min,
            max_players=payload.max_players,
            level=level,
            created_by=user["telegram_id"],
        )
        await db.join_match(
        match_id, user["telegram_id"], payload.max_players, approved=True
    )
        created_match = await db.get_match(match_id)
        if created_match is not None:
            _spawn(_notify_match_event(created_match, user["telegram_id"], "joined"))
        _spawn(_notify_match_feed(user["telegram_id"]))
        return {"ok": True, "id": match_id}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Ошибка создания матча")
        return error_json("Внутренняя ошибка сервера", 500)


@app.post("/api/matches/{match_id}/join")
async def api_join_match(
    match_id: int, user: Annotated[dict, Depends(current_user)]
):
    _match_join_limiter.check(str(user["telegram_id"]))
    try:
        match = await db.get_match(match_id)
        if match is None or match["deleted_at"]:
            return error_json("Матч не найден", 404)
        if match["start_at"] <= int(time.time()):
            return error_json("Матч уже начался", 400)
        if _phone_required_for_new_events() and not user["phone"]:
            return error_json("Укажите телефон в настройках профиля, чтобы присоединиться.", 403)
        status = await db.join_match(
            match_id,
            user["telegram_id"],
            match["max_players"],
            requires_approval=settings.require_event_approval,
        )
        if status == "full":
            return error_json("Мест нет", 409)
        if status == "pending":
            _spawn(_notify_match_request(match, user["telegram_id"]))
        else:
            _spawn(_notify_match_event(match, user["telegram_id"], "joined"))
        return {"ok": True, "status": status}
    except Exception:
        logger.exception("Ошибка входа в матч")
        return error_json("Внутренняя ошибка сервера", 500)


@app.post("/api/matches/{match_id}/requests/{user_id}/approve")
async def api_approve_match_request(
    match_id: int, user_id: int, user: Annotated[dict, Depends(current_user)]
):
    try:
        match = await db.get_match(match_id)
        if (
            match is None
            or match["deleted_at"]
            or not await _can_manage_match(match, user["telegram_id"])
        ):
            return error_json("Матч не найден", 404)
        status = await db.approve_match_player(match_id, user_id, user["telegram_id"])
        if not status:
            return error_json("Заявка не найдена", 404)
        _spawn(_notify_match_event(match, user_id, status))
        return {"ok": True, "status": status}
    except Exception:
        logger.exception("Ошибка подтверждения заявки на матч")
        return error_json("Внутренняя ошибка сервера", 500)


@app.post("/api/matches/{match_id}/requests/{user_id}/reject")
async def api_reject_match_request(
    match_id: int, user_id: int, user: Annotated[dict, Depends(current_user)]
):
    try:
        match = await db.get_match(match_id)
        if (
            match is None
            or match["deleted_at"]
            or not await _can_manage_match(match, user["telegram_id"])
        ):
            return error_json("Матч не найден", 404)
        if not await db.reject_match_player(
            match_id, user_id, user["telegram_id"]
        ):
            return error_json("Заявка не найдена", 404)
        _spawn(_notify_match_event(match, user_id, "rejected"))
        return {"ok": True}
    except Exception:
        logger.exception("Ошибка отклонения заявки на матч")
        return error_json("Внутренняя ошибка сервера", 500)


@app.post("/api/matches/{match_id}/leave")
async def api_leave_match(
    match_id: int, user: Annotated[dict, Depends(current_user)]
):
    _match_join_limiter.check(str(user["telegram_id"]))
    try:
        match = await db.get_match(match_id)
        if match is None or match["deleted_at"]:
            return error_json("Матч не найден", 404)
        if match["start_at"] <= int(time.time()):
            return error_json("Матч уже начался", 400)
        removed = await db.leave_match(match_id, user["telegram_id"])
        if not removed:
            return error_json("Вы не участвуете в этом матче", 400)
        _spawn(_notify_match_event(match, user["telegram_id"], "left"))
        return {"ok": True}
    except Exception:
        logger.exception("Ошибка выхода из матча")
        return error_json("Внутренняя ошибка сервера", 500)


@app.delete("/api/matches/{match_id}")
async def api_delete_match(
    match_id: int, user: Annotated[dict, Depends(current_user)]
):
    try:
        match = await db.get_match(match_id)
        if (
            match is None
            or match["deleted_at"]
            or match["created_by"] != user["telegram_id"]
        ):
            return error_json("Матч не найден", 404)
        await db.delete_match(match_id)
        deleted_match = await db.get_match(match_id)
        if deleted_match is not None:
            _spawn(_notify_match_deleted(deleted_match))
        _spawn(_notify_match_feed(user["telegram_id"]))
        return {"ok": True}
    except Exception:
        logger.exception("Ошибка удаления матча")
        return error_json("Внутренняя ошибка сервера", 500)


# --------------------------------------------------------------------------- #
# API: тренер
# --------------------------------------------------------------------------- #

@app.get("/api/coach/trainings")
async def api_coach_trainings(coach: Annotated[dict, Depends(require_coach)]):
    """Предстоящие тренировки ТЕКУЩЕГО тренера (только свои)."""
    rows = await db.list_active_trainings(int(time.time()), coach["telegram_id"])
    participants_by_training = await db.get_participants_for_trainings(
        [r["id"] for r in rows]
    )
    trainings = []
    for r in rows:
        participants = participants_by_training.get(r["id"], [])
        item = serialize_training(r, viewer_id=coach["telegram_id"])
        item["is_owner"] = r["created_by"] == coach["telegram_id"]
        item["can_manage"] = (
            r["created_by"] == coach["telegram_id"]
            or await db.is_event_coauthor("training", r["id"], coach["telegram_id"])
        )
        item["pending_count"] = sum(
            1 for p in participants if _row_status(p) == "pending"
        )
        trainings.append(item)
    return {"ok": True, "trainings": trainings}


@app.get("/api/coach/pending")
async def api_coach_pending(coach: Annotated[dict, Depends(require_coach)]):
    """Счётчик заявок на занятия — для точки на вкладке «Тренер»."""
    rows = await db.list_active_trainings(int(time.time()), coach["telegram_id"])
    participants_by_training = await db.get_participants_for_trainings(
        [r["id"] for r in rows]
    )
    pending = sum(
        1
        for participants in participants_by_training.values()
        for p in participants
        if _row_status(p) == "pending"
    )
    return {"ok": True, "pending_count": pending}


@app.get("/api/coach/history")
async def api_coach_history(
    coach: Annotated[dict, Depends(require_coach)], limit: int = 100
):
    """История: прошедшие занятия текущего тренера."""
    limit = max(1, min(limit, 500))
    rows = await db.list_history_trainings(
        int(time.time()), coach["telegram_id"], limit
    )
    return {
        "ok": True,
        "trainings": [
            serialize_training(r, viewer_id=coach["telegram_id"]) for r in rows
        ],
    }


@app.get("/api/coach/deleted")
async def api_coach_deleted(
    coach: Annotated[dict, Depends(require_coach)], limit: int = 100
):
    """Корзина текущего тренера."""
    limit = max(1, min(limit, 500))
    rows = await db.list_deleted_trainings(coach["telegram_id"], limit)
    return {
        "ok": True,
        "trainings": [
            serialize_training(r, viewer_id=coach["telegram_id"]) for r in rows
        ],
    }


@app.post("/api/coach/trainings")
async def api_create_training(payload: TrainingIn, coach: Annotated[dict, Depends(require_coach)]):
    try:
        start_at = _parse_local_epoch(payload.date, payload.time)
        if start_at <= int(time.time()):
            return error_json("Нельзя создать тренировку в прошлом", 400)

        level = (payload.level or "").strip()
        if level and level not in TRAINING_LEVELS:
            return error_json("Неизвестный уровень игры", 422)

        # Человеческий фактор №1: защита от двойного тапа / повторной отправки.
        # При совпадении времени и типа возвращаем 409, а не создаём дубль.
        if not payload.force:
            duplicate = await db.find_duplicate_training(
                start_at, payload.training_type, coach["telegram_id"]
            )
            if duplicate is not None:
                return JSONResponse(
                    status_code=409,
                    content={
                        "ok": False,
                        "error": "Такая тренировка уже есть в расписании.",
                        "duplicate_id": duplicate["id"],
                    },
                )

        training_id = await db.create_training(
            title=payload.training_type,
            training_type=payload.training_type,
            start_at=start_at,
            capacity=payload.capacity,
            duration_min=payload.duration_min,
            reminder_offset_min=payload.reminder_offset_min,
            reminder_enabled=payload.reminder_enabled,
            created_by=coach["telegram_id"],
            level=level,
        )
        return {"ok": True, "id": training_id}
    except HTTPException:
        # Ошибки валидации даты/времени должны оставаться 422, а не 500.
        raise
    except Exception:
        logger.exception("Ошибка создания тренировки")
        return error_json("Внутренняя ошибка сервера", 500)


@app.post("/api/coach/trainings/batch")
async def api_create_training_batch(
    payload: TrainingBatchIn, coach: Annotated[dict, Depends(require_coach)]
):
    """Создаёт занятия по выбранным дням недели в заданном периоде."""
    try:
        try:
            start_day = datetime.strptime(payload.period_start, "%Y-%m-%d").date()
            end_day = datetime.strptime(payload.period_end, "%Y-%m-%d").date()
            start_time = datetime.strptime(payload.time, "%H:%M").time()
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="Некорректная дата или время."
            ) from exc

        if end_day < start_day:
            return error_json("Дата окончания раньше начала", 422)
        if (end_day - start_day).days > _TRAINING_BATCH_MAX_DAYS:
            return error_json("Период слишком длинный: максимум 92 дня", 422)

        weekdays = sorted(set(payload.weekdays))
        if not weekdays or any(day < 0 or day > 6 for day in weekdays):
            return error_json("Некорректные дни недели", 422)

        level = (payload.level or "").strip()
        if level and level not in TRAINING_LEVELS:
            return error_json("Неизвестный уровень игры", 422)

        dates = []
        cursor = start_day
        while cursor <= end_day:
            if cursor.weekday() in weekdays:
                dates.append(cursor)
            cursor += timedelta(days=1)
        if not dates:
            return error_json("В выбранном периоде нет таких дней недели", 422)
        if len(dates) > _TRAINING_BATCH_MAX_COUNT:
            return error_json("Слишком много тренировок: максимум 100", 422)

        created: list[int] = []
        skipped: list[dict[str, str]] = []
        now = int(time.time())
        for day in dates:
            start_at = int(
                datetime.combine(day, start_time)
                .replace(tzinfo=settings.timezone)
                .timestamp()
            )
            date_str = day.isoformat()
            if start_at <= now:
                skipped.append({"date": date_str, "reason": "past"})
                continue
            duplicate = await db.find_duplicate_training(
                start_at, payload.training_type, coach["telegram_id"]
            )
            if duplicate is not None:
                skipped.append({"date": date_str, "reason": "duplicate"})
                continue
            training_id = await db.create_training(
                title=payload.training_type,
                training_type=payload.training_type,
                start_at=start_at,
                capacity=payload.capacity,
                duration_min=payload.duration_min,
                reminder_offset_min=payload.reminder_offset_min,
                reminder_enabled=payload.reminder_enabled,
                created_by=coach["telegram_id"],
                level=level,
            )
            created.append(training_id)

        return {
            "ok": True,
            "created": created,
            "created_count": len(created),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Ошибка массового создания тренировок")
        return error_json("Внутренняя ошибка сервера", 500)


@app.delete("/api/coach/trainings/{training_id}")
async def api_delete_training(training_id: int, coach: Annotated[dict, Depends(require_coach)]):
    """
    Человеческий фактор №2: мягкое удаление. Тренировка и все записи
    сохраняются в корзине и могут быть восстановлены одной кнопкой.
    """
    try:
        training = await db.get_training(training_id)
        if (
            training is None
            or training["deleted_at"]
            or training["created_by"] != coach["telegram_id"]
        ):
            return error_json("Тренировка не найдена", 404)
        await db.soft_delete_training(training_id, coach["telegram_id"])
        _spawn(_notify_training_deleted(training))
        return {"ok": True, "restorable": True}
    except Exception:
        logger.exception("Ошибка удаления тренировки")
        return error_json("Внутренняя ошибка сервера", 500)


@app.post("/api/coach/trainings/{training_id}/restore")
async def api_restore_training(training_id: int, coach: Annotated[dict, Depends(require_coach)]):
    """Восстановление тренировки из корзины."""
    try:
        training = await db.get_training(training_id)
        if (
            training is None
            or not training["deleted_at"]
            or training["created_by"] != coach["telegram_id"]
        ):
            return error_json("Тренировка не найдена в корзине", 404)
        await db.restore_training(training_id)
        _spawn(_notify_training_restored(training))
        return {"ok": True}
    except Exception:
        logger.exception("Ошибка восстановления тренировки")
        return error_json("Внутренняя ошибка сервера", 500)


@app.delete("/api/coach/trainings/{training_id}/purge")
async def api_purge_training(training_id: int, coach: Annotated[dict, Depends(require_coach)]):
    """Безвозвратное удаление — доступно только из корзины."""
    try:
        training = await db.get_training(training_id)
        if (training is None or not training["deleted_at"]
                or training["created_by"] != coach["telegram_id"]):
            return error_json("В корзине такой тренировки нет", 404)
        await db.purge_training(training_id)
        return {"ok": True}
    except Exception:
        logger.exception("Ошибка очистки тренировки")
        return error_json("Внутренняя ошибка сервера", 500)


@app.get("/api/coach/trainings/{training_id}/participants")
async def api_coach_participants(training_id: int, coach: Annotated[dict, Depends(require_coach)]):
    """Участники занятия — доступно любому тренеру (клубный просмотр, только чтение)."""
    try:
        training = await db.get_training(training_id)
        if training is None or training["deleted_at"]:
            return error_json("Тренировка не найдена", 404)
        rows = await db.get_participants_full(training_id)
        return {
            "ok": True,
            "participants": [
                {
                    **participant_identity(r),
                    "telegram_id": r["telegram_id"],
                    "username": r["username"] if r["show_username"] else None,
                    "username_visible": bool(r["show_username"]),
                    "about": r["about"] or "",
                    "status": r["status"],
                }
                for r in rows
            ],
        }
    except Exception:
        logger.exception("Ошибка чтения участников")
        return error_json("Внутренняя ошибка сервера", 500)


@app.post("/api/coach/trainings/{training_id}/participants/{user_id}/approve")
async def api_coach_approve_participant(
    training_id: int, user_id: int, coach: Annotated[dict, Depends(require_coach)]
):
    """Подтверждает заявку: выдаёт место или ставит в очередь."""
    try:
        training = await db.get_training(training_id)
        if (
            training is None
            or training["deleted_at"]
            or not await _can_manage_training(training, coach["telegram_id"])
        ):
            return error_json("Тренировка не найдена", 404)
        status = await db.approve_booking(training_id, user_id)
        if not status:
            return error_json("Заявка не найдена", 404)
        _spawn(
            _notify_training_event(
                training,
                user_id,
                status,
                actor_id=coach["telegram_id"],
            )
        )
        return {"ok": True, "status": status}
    except Exception:
        logger.exception("Ошибка подтверждения заявки")
        return error_json("Внутренняя ошибка сервера", 500)


@app.post("/api/coach/trainings/{training_id}/participants/{user_id}/reject")
async def api_coach_reject_participant(
    training_id: int, user_id: int, coach: Annotated[dict, Depends(require_coach)]
):
    """Отклоняет заявку на тренировку."""
    try:
        training = await db.get_training(training_id)
        if (
            training is None
            or training["deleted_at"]
            or not await _can_manage_training(training, coach["telegram_id"])
        ):
            return error_json("Тренировка не найдена", 404)
        if not await db.reject_booking(training_id, user_id):
            return error_json("Заявка не найдена", 404)
        _spawn(
            _notify_training_event(
                training,
                user_id,
                "rejected",
                actor_id=coach["telegram_id"],
            )
        )
        return {"ok": True}
    except Exception:
        logger.exception("Ошибка отклонения заявки")
        return error_json("Внутренняя ошибка сервера", 500)


@app.post("/api/coach/trainings/{training_id}/participants/{user_id}/cancel")
async def api_coach_cancel_participant(
    training_id: int, user_id: int, coach: Annotated[dict, Depends(require_coach)]
):
    """Человеческий фактор №3: тренер убирает игрока (с возможностью вернуть)."""
    try:
        training = await db.get_training(training_id)
        if (
            training is None
            or training["deleted_at"]
            or training["created_by"] != coach["telegram_id"]
        ):
            return error_json("Тренировка не найдена", 404)
        booking = await db.get_booking(training_id, user_id)
        if booking is None or booking["status"] not in {"booked", "waitlist"}:
            return error_json("Записи не существует", 404)
        promoted = await db.cancel_booking(training_id, user_id)
        _spawn(
            _notify_participant_removed(
                user_id, training, actor_id=coach["telegram_id"]
            )
        )
        if promoted:
            _spawn(_notify_promoted(int(promoted), training))
        return {"ok": True, "promoted": promoted or None}
    except Exception:
        logger.exception("Ошибка снятия игрока")
        return error_json("Внутренняя ошибка сервера", 500)


@app.post("/api/coach/trainings/{training_id}/participants/{user_id}/restore")
async def api_coach_restore_participant(
    training_id: int, user_id: int, coach: Annotated[dict, Depends(require_coach)]
):
    """Возврат случайно снятого игрока (в место или в очередь по FIFO)."""
    try:
        training = await db.get_training(training_id)
        if (
            training is None
            or training["deleted_at"]
            or training["created_by"] != coach["telegram_id"]
        ):
            return error_json("Тренировка не найдена", 404)
        if await db.get_user(user_id) is None:
            return error_json("Игрок не найден", 404)
        booking = await db.get_booking(training_id, user_id)
        if booking is None or booking["status"] != "cancelled":
            return error_json("Можно вернуть только снятого игрока", 400)
        status = await db.restore_booking(training_id, user_id, training["capacity"])
        _spawn(
            _notify_participant_restored(
                user_id, training, status, actor_id=coach["telegram_id"]
            )
        )
        return {"ok": True, "status": status}
    except Exception:
        logger.exception("Ошибка возврата игрока")
        return error_json("Внутренняя ошибка сервера", 500)


@app.patch("/api/coach/trainings/{training_id}/reminder")
async def api_update_reminder(
    training_id: int,
    payload: ReminderIn,
    coach: Annotated[dict, Depends(require_coach)],
):
    try:
        training = await db.get_training(training_id)
        if (
            training is None
            or training["deleted_at"]
            or training["created_by"] != coach["telegram_id"]
        ):
            return error_json("Тренировка не найдена", 404)
        await db.update_reminder_settings(
            training_id, enabled=payload.enabled, offset_min=payload.offset_min
        )
        return {"ok": True}
    except Exception:
        logger.exception("Ошибка обновления напоминания")
        return error_json("Внутренняя ошибка сервера", 500)


@app.get("/api/coach/players")
async def api_players(coach: Annotated[dict, Depends(require_coach)]):
    rows = await db.list_players()
    return {
        "ok": True,
        "players": [
            {
                **participant_identity(r),
                "telegram_id": r["telegram_id"],
                "username": r["username"] if r["show_username"] else None,
                "username_visible": bool(r["show_username"]),
                "about": r["about"] or "",
                "display_mode": r["display_mode"] or "name",
                "notes": r["notes"],
                "notifications_enabled": bool(r["notifications_enabled"]),
            }
            for r in rows
        ],
    }


@app.put("/api/coach/players/{telegram_id}/notes")
async def api_set_notes(
    telegram_id: int,
    payload: NotesIn,
    coach: Annotated[dict, Depends(require_coach)],
):
    try:
        if await db.get_user(telegram_id) is None:
            return error_json("Игрок не найден", 404)
        await db.set_user_notes(telegram_id, payload.notes)
        return {"ok": True}
    except Exception:
        logger.exception("Ошибка сохранения заметок")
        return error_json("Внутренняя ошибка сервера", 500)


# --------------------------------------------------------------------------- #
# API: уровни и публичный профиль тренера
# --------------------------------------------------------------------------- #

@app.get("/api/levels")
async def api_levels(user: Annotated[dict, Depends(current_user)]):
    """Справочник уровней игры для формы тренера."""
    return {"ok": True, "levels": list(TRAINING_LEVELS)}


@app.get("/api/coach/profile")
async def api_get_coach_profile(coach: Annotated[dict, Depends(require_coach)]):
    return {"ok": True, "profile": serialize_coach(await db.get_user(coach["telegram_id"]))}


@app.put("/api/coach/profile")
async def api_set_coach_profile(
    payload: TrainerProfileIn,
    coach: Annotated[dict, Depends(require_coach)],
):
    """Сохраняет публичный профиль тренера (кратко о себе)."""
    try:
        await db.set_trainer_profile(coach["telegram_id"], sanitize_bio(payload.bio))
        return {"ok": True, "profile": serialize_coach(await db.get_user(coach["telegram_id"]))}
    except Exception:
        logger.exception("Ошибка сохранения профиля тренера")
        return error_json("Внутренняя ошибка сервера", 500)


@app.post("/api/messages/coach")
async def api_message_coach(payload: MessageIn, user: Annotated[dict, Depends(current_user)]):
    """
    Личное сообщение тренеру. Сохраняем в БД и доставляем через бота в фоне
    (чтобы недоступность Telegram не блокировала ответ игроку).
    Тренер читает сообщения командой /inbox и отвечает кнопкой «Ответить» —
    без показа Telegram ID игрока.
    """
    _message_limiter.check(str(user["telegram_id"]))
    text = (payload.text or "").strip()
    if not text:
        return error_json("Пустое сообщение.", 422)

    # Сообщение уходит тренеру того занятия, на карточке которого нажали «Написать».
    coach_id = settings.coach_telegram_id
    if payload.training_id is not None:
        training = await db.get_training(payload.training_id)
        if training is None or training["deleted_at"]:
            return error_json("Тренировка не найдена.", 404)
        coach_id = training["created_by"] or coach_id

    await db.add_message(user["telegram_id"], coach_id, text)
    sender_name = (
        animal_identity(user["telegram_id"])["name"]
        if user.get("display_mode") == "animal"
        else (user["first_name"] or "Игрок")
    )
    _spawn(_deliver_to_coach(coach_id, user["telegram_id"], sender_name, text))
    return {"ok": True}


@app.get("/api/messages/thread")
async def api_player_thread(user: Annotated[dict, Depends(current_user)]):
    rows = await db.list_user_thread(user["telegram_id"])
    return {
        "ok": True,
        "messages": [
            {
                "id": r["id"],
                "from_me": r["from_user"] == user["telegram_id"],
                "text": r["text"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


@app.get("/api/coach/messages")
async def api_coach_messages(coach: Annotated[dict, Depends(require_coach)]):
    rows = await db.list_coach_conversations(coach["telegram_id"])
    return {
        "ok": True,
        "conversations": [
            {
                "telegram_id": r["other_id"],
                "first_name": animal_identity(r["other_id"])["name"]
                if r["display_mode"] == "animal"
                else (r["first_name"] or "Игрок"),
                "avatar_url": ""
                if r["display_mode"] == "animal"
                else _avatar_url(
                    r["other_id"], real=True, file_id=_row_file_id(r)
                ),
                "last_text": r["text"],
                "last_at": r["created_at"],
            }
            for r in rows
        ],
    }


@app.get("/api/coach/messages/{user_id}")
async def api_coach_thread(user_id: int, coach: Annotated[dict, Depends(require_coach)]):
    rows = await db.list_coach_thread(coach["telegram_id"], user_id)
    return {
        "ok": True,
        "messages": [
            {
                "id": r["id"],
                "from_me": r["from_user"] == coach["telegram_id"],
                "text": r["text"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


@app.post("/api/coach/messages/{user_id}")
async def api_coach_reply(
    user_id: int, payload: MessageIn, coach: Annotated[dict, Depends(require_coach)]
):
    _message_limiter.check(str(coach["telegram_id"]))
    text = (payload.text or "").strip()
    if not text:
        return error_json("Пустое сообщение.", 422)
    if await db.get_user(user_id) is None:
        return error_json("Игрок не найден.", 404)
    await db.add_message(coach["telegram_id"], user_id, text)
    _spawn(_notify_new_chat_message(user_id, coach["telegram_id"]))
    return {"ok": True}


async def _notify_new_chat_message(telegram_id: int, author_id: int) -> None:
    """Уведомление о сообщении без текста: переписка остаётся в приложении."""
    if not await _notifications_allowed(telegram_id, "messages"):
        return
    try:
        await app.state.bot.send_message(
            telegram_id,
            "✉️ Новое сообщение от тренера в приложении.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="Открыть чат",
                    web_app=WebAppInfo(url=bot_module._web_app_url()),
                )
            ]]),
        )
    except Exception:
        logger.info("Не удалось уведомить о сообщении %s", telegram_id, exc_info=True)


async def _notify_feedback(
    user: dict[str, Any], kind: str, text: str, page: str
) -> None:
    """Отправляет обратную связь в чат тренера с кнопкой ответа."""
    label = "🐞 <b>Проблема</b>" if kind == "bug" else "💡 <b>Предложение</b>"
    name = html.escape(
        participant_identity(cast(aiosqlite.Row, user))["first_name"] or "Игрок"
    )
    lines = [label, f"👤 {name}"]
    if page:
        lines.append(f"📍 {html.escape(page)}")
    lines.extend(["", html.escape(text)])
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="Открыть приложение",
                web_app=WebAppInfo(url=bot_module._web_app_url()),
            )
        ]]
    )
    try:
        await app.state.bot.send_message(
            settings.coach_telegram_id,
            "\n".join(lines),
            reply_markup=keyboard,
        )
    except Exception:
        logger.warning("Не удалось доставить обратную связь", exc_info=True)


async def _deliver_to_coach(coach_id: int, sender_id: int, sender_name: str, text: str) -> None:
    """Фоновая доставка сообщения тренеру с кнопкой быстрого ответа."""
    if not await _notifications_allowed(coach_id, "messages"):
        return
    try:
        await app.state.bot.send_message(
            coach_id,
            "✉️ Новое сообщение в приложении.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="Открыть чат",
                    web_app=WebAppInfo(url=bot_module._web_app_url()),
                )
            ]]),
        )
    except Exception:
        logger.warning("Не удалось доставить сообщение тренеру", exc_info=True)


def _training_brief(training: aiosqlite.Row) -> str:
    """Короткое описание занятия для уведомлений (HTML-экранированное)."""
    start = _local_time(training["start_at"])
    training_type = html.escape(training["training_type"] or "Тренировка")
    return f"{training_type} · {start.strftime('%d.%m.%Y в %H:%M')}"


async def _send_user_message(
    telegram_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Best-effort уведомление пользователя; недоступность Telegram не ломает API."""
    try:
        await app.state.bot.send_message(
            telegram_id, text, reply_markup=reply_markup
        )
    except Exception:
        logger.info("Не удалось отправить уведомление %s", telegram_id, exc_info=True)


async def _send_event_notification(
    kind: str,
    entity_id: int,
    chat_id: int,
    user_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> str:
    """
    Одно обновляемое сообщение на событие и чат.

    Сначала пытаемся отредактировать ранее отправленное сообщение, чтобы
    у тренера/организатора и игрока не росла лента одинаковых уведомлений.
    Возвращает способ доставки: sent | edited | same | failed — по нему
    вызывающий код решает, нужно ли отдельное всплывающее уведомление
    (Telegram не присылает push на edit_message_text).
    """
    try:
        record = await db.get_event_notification(kind, entity_id, chat_id, user_id)
        message_id = int(record["message_id"] or 0) if record is not None else 0
        if (
            message_id
            and record is not None
            and record["text"] == text
            and reply_markup is None
        ):
            return "same"
        if message_id:
            try:
                await app.state.bot.edit_message_text(
                    text=text,
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=reply_markup,
                )
                await db.save_event_notification(
                    kind, entity_id, chat_id, user_id, message_id, text
                )
                return "edited"
            except Exception as exc:
                if "message is not modified" in str(exc).lower():
                    await db.save_event_notification(
                        kind, entity_id, chat_id, user_id, message_id, text
                    )
                    return "same"
                logger.info("Не удалось обновить сообщение %s", chat_id, exc_info=True)
        sent = await app.state.bot.send_message(
            chat_id, text, reply_markup=reply_markup
        )
        new_id = int(getattr(sent, "message_id", 0) or 0)
        await db.save_event_notification(
            kind, entity_id, chat_id, user_id, new_id, text
        )
        return "sent"
    except Exception:
        logger.info("Не удалось отправить уведомление %s", chat_id, exc_info=True)
        return "failed"


def _training_header(training: aiosqlite.Row, coach_name: str = "") -> str:
    """Первая строка уведомления: «Тип: уровень - тренер»."""
    training_type = str(_row_value(training, "training_type") or "Тренировка")
    level = str(_row_value(training, "level") or "")
    coach = (coach_name or "").strip()
    details = " - ".join(part for part in (level, coach) if part)
    if details:
        training_type += f": {details}"
    return html.escape(training_type)


def _training_datetime(training: aiosqlite.Row) -> str:
    """Дата и время занятия в утверждённом формате: 26.10 / 19:00."""
    return _local_time(training["start_at"]).strftime("%d.%m / %H:%M")


async def _training_coach_name(training: aiosqlite.Row) -> str:
    """Имя тренера для заголовка; занятие всегда создаёт тренер."""
    creator = _row_value(training, "created_by", None)
    if not creator:
        return ""
    try:
        row = await db.get_user(int(creator))
    except Exception:
        logger.info("Не удалось прочитать тренера %s", creator, exc_info=True)
        return ""
    return str(_row_value(row, "first_name") or "") if row is not None else ""


def _append_numbered_names(
    lines: list[str], rows: list[aiosqlite.Row], limit: int = 25
) -> None:
    """Нумерованный список имён; длинные списки обрезаются многоточием."""
    for index, row in enumerate(rows[:limit], start=1):
        name = participant_identity(row)["first_name"]
        lines.append(f"{index}. {html.escape(name)}")
    if len(rows) > limit:
        lines.append("…")


def _training_participants_text(
    training: aiosqlite.Row,
    participants: list[aiosqlite.Row],
    coach_name: str = "",
) -> str:
    """Сводка занятия в утверждённом формате: уровень - тренер, дата, список."""
    booked = [p for p in participants if _row_status(p) == "booked"]
    lines = [
        _training_header(training, coach_name),
        _training_datetime(training),
        "Записаны:",
    ]
    if booked:
        _append_numbered_names(lines, booked)
    else:
        lines.append("Пока никто не записан")
    if training["deleted_at"]:
        lines.append("Тренировка отменена")
    return "\n".join(lines)


def _training_alert_text(
    training: aiosqlite.Row, coach_name: str, status: str, actor_name: str
) -> str:
    """Короткое отдельное уведомление тренеру об изменении записи."""
    actions = {
        "pending": "Новая заявка",
        "booked": "Новая запись",
        "waitlist": "В лист ожидания",
        "cancelled": "Отмена записи",
        "rejected": "Заявка отклонена",
        "promoted": "Место освободилось",
    }
    action = actions.get(status, "Изменение записи")
    return (
        f"{action}: {html.escape(actor_name)}\n"
        f"{_training_header(training, coach_name)}\n"
        f"{_training_datetime(training)}"
    )


def _training_player_alert_text(
    training: aiosqlite.Row, coach_name: str, status: str
) -> str:
    """Короткое отдельное уведомление игроку: правка сообщения не даёт push."""
    return (
        f"{_training_player_status(status)}\n"
        f"{_training_player_header(training, coach_name)}\n"
        f"{_training_datetime(training)}"
    )


async def _training_actor_name(
    actor_id: int | None, participants: list[aiosqlite.Row]
) -> str:
    """Имя того, кто изменил запись; после отмены берём профиль из БД."""
    if actor_id is None:
        return "Игрок"
    row = next(
        (p for p in participants if p["telegram_id"] == actor_id),
        None,
    )
    if row is None:
        row = await db.get_user(actor_id)
    if row is None:
        return "Игрок"
    return str(participant_identity(row)["first_name"])


def _training_player_header(training: aiosqlite.Row, coach_name: str = "") -> str:
    """Заголовок для игрока: «Тип: уровень. Тренер: имя»."""
    training_type = str(_row_value(training, "training_type") or "Тренировка")
    level = str(_row_value(training, "level") or "")
    coach = (coach_name or "").strip()
    header = training_type
    if level:
        header += f": {level}"
    if coach:
        header += f". Тренер: {coach}"
    return html.escape(header)


def _training_player_status(status: str) -> str:
    """Текст статуса заявки/записи для игрока."""
    if status == "pending":
        return "Заявка на занятие отправлена."
    if status in {"booked", "promoted"}:
        return "Вы записаны."
    if status == "waitlist":
        return (
            "Заявка на занятие одобрена, но пока нет мест,\n"
            "записали вас в лист ожидания."
        )
    if status == "rejected":
        return "Заявка на занятие отклонена — к сожалению, мест нет."
    if status == "cancelled":
        return "Тренер снял вас с тренировки."
    if status == "deleted":
        return "Тренировка отменена."
    return "Запись обновлена."


def _training_personal_text(
    training: aiosqlite.Row,
    participants: list[aiosqlite.Row],
    status: str,
    coach_name: str = "",
) -> str:
    """Личное сообщение игроку: статус заявки и актуальный состав."""
    booked = [p for p in participants if _row_status(p) == "booked"]
    lines = [
        _training_player_status(status),
        "",
        _training_player_header(training, coach_name),
        _training_datetime(training),
        "Записаны:",
    ]
    if booked:
        _append_numbered_names(lines, booked)
    else:
        lines.append("Пока никто не записан")
    return "\n".join(lines)


def _training_end_at(training: aiosqlite.Row) -> int:
    return int(training["start_at"]) + int(training["duration_min"]) * 60


def _training_delete_at(training: aiosqlite.Row) -> int:
    """Через 4 часа после окончания занятия."""
    return _training_end_at(training) + 4 * 3600


def _needs_preliminary(training: aiosqlite.Row, now: int) -> bool:
    """
    Нужно ли раннее сообщение с заменой за день до старта.

    Ориентир — 2 дня, плюс страховка на лимит Telegram: финальное
    сообщение должно успеть удалиться в пределах 48 часов.
    """
    return (
        int(training["start_at"]) - now >= 2 * 86400
        or _training_delete_at(training) - now > 47 * 3600
    )


def _training_booked_message_text(
    training: aiosqlite.Row,
    participants: list[aiosqlite.Row],
    coach_name: str,
    phase: str,
) -> str:
    """Подтверждение записи: полный состав и пометка об удалении."""
    booked = [p for p in participants if _row_status(p) == "booked"]
    lines = [
        "Вы записаны.",
        "",
        _training_player_header(training, coach_name),
        _training_datetime(training),
        "Записаны:",
    ]
    if booked:
        _append_numbered_names(lines, booked)
    else:
        lines.append("Пока никто не записан")
    if phase == "preliminary":
        lines.extend([
            "",
            "<i>Это сообщение будет удалено.",
            "Актуальную информацию пришлём за день до занятия.</i>",
        ])
    else:
        lines.extend([
            "",
            "<i>Это сообщение будет автоматически удалено",
            "через 4 часа после окончания занятия.</i>",
        ])
    return "\n".join(lines)


def _training_waitlist_text(
    training: aiosqlite.Row, coach_name: str
) -> str:
    return (
        f"{_training_player_status('waitlist')}\n\n"
        f"{_training_player_header(training, coach_name)}\n"
        f"{_training_datetime(training)}"
    )


def _training_rejected_text(
    training: aiosqlite.Row, coach_name: str
) -> str:
    return (
        f"{_training_player_status('rejected')}\n\n"
        f"{_training_player_header(training, coach_name)}\n"
        f"{_training_datetime(training)}\n\n"
        "<i>Это сообщение будет автоматически очищено.</i>"
    )


def _pending_booking_keyboard(pending: list[aiosqlite.Row]) -> InlineKeyboardMarkup:
    if not pending:
        return InlineKeyboardMarkup(inline_keyboard=[])
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=f"Открыть заявки · {len(pending)}",
                web_app=WebAppInfo(url=bot_module._web_app_url()),
            )
        ]]
    )


_STATUS_NOTIFICATION_KINDS = (
    "training_booked",
    "training_waitlist",
    "training_rejected",
)


async def _delete_notification_row(row: aiosqlite.Row) -> None:
    """Удаляет сообщение в Telegram и запись о нём; ошибки не фатальны."""
    message_id = int(row["message_id"] or 0)
    if message_id:
        try:
            await app.state.bot.delete_message(
                int(row["chat_id"]), message_id
            )
        except Exception:
            logger.info(
                "Не удалось удалить сообщение %s", row["chat_id"], exc_info=True
            )
    await db.delete_event_notification_by_id(int(row["id"]))


async def _edit_notification_row(row: aiosqlite.Row, text: str) -> None:
    """Тихо обновляет текст уже отправленного сообщения."""
    message_id = int(row["message_id"] or 0)
    if message_id:
        try:
            await app.state.bot.edit_message_text(
                text=text,
                chat_id=int(row["chat_id"]),
                message_id=message_id,
            )
        except Exception as exc:
            if "message is not modified" not in str(exc).lower():
                logger.info(
                    "Не удалось обновить сообщение %s",
                    row["chat_id"],
                    exc_info=True,
                )
    await db.update_event_notification_text(
        str(row["kind"]),
        int(row["entity_id"]),
        int(row["chat_id"]),
        int(row["user_id"]),
        text,
    )


async def _delete_training_status_messages(training_id: int) -> None:
    rows = await db.list_event_notifications_for_training(training_id)
    for row in rows:
        if row["kind"] in _STATUS_NOTIFICATION_KINDS:
            await _delete_notification_row(row)


async def _send_new_event_message(
    kind: str,
    training_id: int,
    chat_id: int,
    text: str,
    *,
    delete_at: int,
    phase: str,
    sent_at: int,
    final_at: int | None = None,
) -> int:
    """Отправляет новое сообщение и сохраняет его таймер."""
    try:
        sent = await app.state.bot.send_message(chat_id, text)
    except Exception:
        logger.info("Не удалось отправить уведомление %s", chat_id, exc_info=True)
        return 0
    message_id = int(getattr(sent, "message_id", 0) or 0)
    await db.save_event_notification(
        kind,
        training_id,
        chat_id,
        chat_id,
        message_id,
        text,
        delete_at=delete_at,
        phase=phase,
        sent_at=sent_at,
        final_at=final_at,
    )
    return message_id


async def _send_final_enrollment(
    training: aiosqlite.Row,
    row: aiosqlite.Row,
    participants: list[aiosqlite.Row],
    coach_name: str,
    now: int,
) -> bool:
    """Заменяет предварительное подтверждение финальным."""
    user_id = int(row["user_id"])
    if not await _notifications_allowed(user_id, "trainings"):
        await _delete_notification_row(row)
        return False
    text = _training_booked_message_text(
        training, participants, coach_name, "final"
    )
    try:
        sent = await app.state.bot.send_message(user_id, text)
    except Exception:
        logger.info(
            "Не удалось отправить финальное подтверждение %s",
            user_id,
            exc_info=True,
        )
        return False
    message_id = int(getattr(sent, "message_id", 0) or 0)
    old_message_id = int(row["message_id"] or 0)
    if old_message_id:
        try:
            await app.state.bot.delete_message(user_id, old_message_id)
        except Exception:
            logger.info(
                "Не удалось удалить предварительное сообщение %s",
                user_id,
                exc_info=True,
            )
    await db.save_event_notification(
        "training_booked",
        int(row["entity_id"]),
        user_id,
        user_id,
        message_id,
        text,
        delete_at=_training_delete_at(training),
        phase="final",
        sent_at=now,
        final_at=None,
    )
    return True


async def _ensure_training_status_message(
    training: aiosqlite.Row,
    user_id: int,
    status: str,
    participants: list[aiosqlite.Row],
    coach_name: str,
    now: int,
) -> None:
    """Создаёт/обновляет отдельное уведомление о статусе записи."""
    rows = await db.list_event_notifications_for_player(training["id"], user_id)
    by_kind = {row["kind"]: row for row in rows}

    if int(training["start_at"]) <= now:
        for kind in _STATUS_NOTIFICATION_KINDS:
            row = by_kind.get(kind)
            if row is not None:
                await _delete_notification_row(row)
        return

    if status in {"booked", "promoted"}:
        for kind in ("training_waitlist", "training_rejected"):
            row = by_kind.get(kind)
            if row is not None:
                await _delete_notification_row(row)
        existing = by_kind.get("training_booked")
        if existing is not None:
            phase = str(existing["phase"] or "final")
            final_at = int(existing["final_at"] or 0)
            if phase == "preliminary" and final_at and final_at <= now:
                await _send_final_enrollment(
                    training, existing, participants, coach_name, now
                )
            elif existing["message_id"]:
                text = _training_booked_message_text(
                    training, participants, coach_name, phase
                )
                await db.update_event_notification_text(
                    "training_booked", training["id"], user_id, user_id, text
                )
            return
        if not await _notifications_allowed(user_id, "trainings"):
            return
        if _needs_preliminary(training, now):
            final_at = int(training["start_at"]) - 86400
            await _send_new_event_message(
                "training_booked",
                training["id"],
                user_id,
                _training_booked_message_text(
                    training, participants, coach_name, "preliminary"
                ),
                delete_at=min(now + 86400, final_at),
                phase="preliminary",
                sent_at=now,
                final_at=final_at,
            )
        else:
            await _send_new_event_message(
                "training_booked",
                training["id"],
                user_id,
                _training_booked_message_text(
                    training, participants, coach_name, "final"
                ),
                delete_at=_training_delete_at(training),
                phase="final",
                sent_at=now,
            )
        return

    if status == "waitlist":
        for kind in ("training_booked", "training_rejected"):
            row = by_kind.get(kind)
            if row is not None:
                await _delete_notification_row(row)
        if by_kind.get("training_waitlist") is not None and by_kind[
            "training_waitlist"
        ]["message_id"]:
            return
        if not await _notifications_allowed(user_id, "trainings"):
            return
        await _send_new_event_message(
            "training_waitlist",
            training["id"],
            user_id,
            _training_waitlist_text(training, coach_name),
            delete_at=min(_training_delete_at(training), now + 47 * 3600),
            phase="status",
            sent_at=now,
        )
        return

    if status == "rejected":
        for kind in ("training_booked", "training_waitlist"):
            row = by_kind.get(kind)
            if row is not None:
                await _delete_notification_row(row)
        if by_kind.get("training_rejected") is not None and by_kind[
            "training_rejected"
        ]["message_id"]:
            return
        if not await _notifications_allowed(user_id, "trainings"):
            return
        await _send_new_event_message(
            "training_rejected",
            training["id"],
            user_id,
            _training_rejected_text(training, coach_name),
            delete_at=min(_training_delete_at(training), now + 47 * 3600),
            phase="status",
            sent_at=now,
        )
        return

    # pending / cancelled / deleted: отдельные статусные сообщения больше не нужны
    for kind in _STATUS_NOTIFICATION_KINDS:
        row = by_kind.get(kind)
        if row is not None:
            await _delete_notification_row(row)


async def _refresh_training_messages(
    training: aiosqlite.Row,
    participants: list[aiosqlite.Row],
    coach_name: str,
) -> None:
    """Тихо обновляет составы в уже отправленных сообщениях."""
    full = await db.get_participants_full(training["id"])
    status_by_user = {
        int(p["telegram_id"]): str(p["status"]) for p in full
    }
    rows = await db.list_event_notifications_for_training(training["id"])
    for row in rows:
        kind = str(row["kind"])
        user_id = int(row["user_id"])
        if kind == "training" and user_id:
            status = status_by_user.get(user_id)
            if status is None:
                continue
            text = _training_personal_text(
                training, participants, status, coach_name
            )
            await _edit_notification_row(row, text)
        elif kind == "training_booked":
            if status_by_user.get(user_id) != "booked":
                continue
            phase = str(row["phase"] or "final")
            if phase == "preliminary" and not row["message_id"]:
                continue
            text = _training_booked_message_text(
                training, participants, coach_name, phase
            )
            await _edit_notification_row(row, text)


async def _notify_training_event(
    training: aiosqlite.Row,
    user_id: int | None = None,
    status: str = "",
    actor_id: int | None = None,
) -> None:
    participants = await db.get_participants(training["id"])
    pending = [p for p in participants if _row_status(p) == "pending"]
    coach_name = await _training_coach_name(training)
    now = int(time.time())
    summary = _training_participants_text(training, participants, coach_name)
    keyboard = _pending_booking_keyboard(pending) if pending else None
    if user_id is not None and not status:
        status = next(
            (p["status"] for p in participants if p["telegram_id"] == user_id),
            "cancelled",
        )
    actor_name = (
        await _training_actor_name(actor_id, participants)
        if actor_id is not None
        else ""
    )
    if user_id is not None:
        await _ensure_training_status_message(
            training, user_id, status, participants, coach_name, now
        )
        if await _notifications_allowed(user_id, "trainings"):
            delivery = await _send_event_notification(
                "training",
                training["id"],
                user_id,
                user_id,
                _training_personal_text(
                    training, participants, status, coach_name
                ),
            )
            # Для решений тренера отдельный push уже отправил статус выше.
            if (
                delivery == "edited"
                and user_id != actor_id
                and status == "cancelled"
            ):
                await _send_user_message(
                    user_id,
                    _training_player_alert_text(training, coach_name, status),
                )
    await _refresh_training_messages(training, participants, coach_name)

    recipients: list[int] = []
    creator = _row_value(training, "created_by", None)
    if creator:
        recipients.append(int(creator))
    for coauthor in await db.list_event_coauthors("training", training["id"]):
        coauthor_id = int(coauthor["telegram_id"])
        if coauthor_id not in recipients:
            recipients.append(coauthor_id)
    for recipient in recipients:
        if not await _notifications_allowed(recipient, "trainings"):
            continue
        delivery = await _send_event_notification(
            "training",
            training["id"],
            recipient,
            0,
            summary,
            reply_markup=keyboard,
        )
        # Правка сообщения не даёт push в Telegram. Для заявок/очереди/отмены
        # сводка может не измениться, поэтому шлём отдельный алерт всегда.
        should_alert = delivery == "edited" or status in {
            "pending",
            "waitlist",
            "cancelled",
            "rejected",
        }
        if should_alert and actor_id is not None and recipient != actor_id:
            await _send_user_message(
                recipient,
                _training_alert_text(
                    training, coach_name, status or "booked", actor_name
                ),
                reply_markup=keyboard,
            )


async def _notify_promoted(user_id: int, training: aiosqlite.Row) -> None:
    await _notify_training_event(training, user_id, "promoted")


async def _notify_participant_removed(
    user_id: int, training: aiosqlite.Row, actor_id: int | None = None
) -> None:
    await _notify_training_event(
        training, user_id, "cancelled", actor_id=actor_id
    )


async def _notify_participant_restored(
    user_id: int,
    training: aiosqlite.Row,
    status: str,
    actor_id: int | None = None,
) -> None:
    await _notify_training_event(
        training, user_id, status, actor_id=actor_id
    )


async def _notify_training_deleted(training: aiosqlite.Row) -> None:
    fresh = await db.get_training(training["id"])
    if fresh is not None:
        training = fresh
    participants = await db.get_participants(training["id"])
    coach_name = await _training_coach_name(training)
    await _delete_training_status_messages(training["id"])
    if training["created_by"] and await _notifications_allowed(
        training["created_by"], "trainings"
    ):
        await _send_event_notification(
            "training",
            training["id"],
            training["created_by"],
            0,
            _training_participants_text(training, participants, coach_name),
        )
    for row in participants:
        if not await _notifications_allowed(row["telegram_id"], "trainings"):
            continue
        delivery = await _send_event_notification(
            "training",
            training["id"],
            row["telegram_id"],
            row["telegram_id"],
            _training_personal_text(
                training, participants, "deleted", coach_name
            ),
        )
        if delivery == "edited":
            await _send_user_message(
                row["telegram_id"],
                _training_player_alert_text(training, coach_name, "deleted"),
            )


async def _notify_training_restored(training: aiosqlite.Row) -> None:
    """Возвращает тренеру и игрокам актуальные сообщения после восстановления."""
    fresh = await db.get_training(training["id"])
    if fresh is not None:
        training = fresh
    participants = await db.get_participants(training["id"])
    coach_name = await _training_coach_name(training)
    now = int(time.time())
    await _notify_training_event(training, None)
    for row in participants:
        if _row_status(row) == "booked":
            await _ensure_training_status_message(
                training,
                int(row["telegram_id"]),
                "booked",
                participants,
                coach_name,
                now,
            )
    await _refresh_training_messages(training, participants, coach_name)


def _is_transient_delete_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("timeout", "timed out", "too many requests",
                       "network", "connection")
    )


async def _process_due_deletions(now: int) -> None:
    for row in await db.list_due_event_notifications(now):
        kind = str(row["kind"])
        phase = str(row["phase"] or "")
        final_at = int(row["final_at"] or 0)
        if kind == "training_booked" and phase == "preliminary" and final_at:
            if final_at <= now:
                # Финальное сообщение отправит отдельный обработчик.
                continue
            message_id = int(row["message_id"] or 0)
            if message_id:
                try:
                    await app.state.bot.delete_message(
                        int(row["chat_id"]), message_id
                    )
                except Exception as exc:
                    if _is_transient_delete_error(exc):
                        continue
                    logger.info(
                        "Не удалось удалить предварительное сообщение %s",
                        row["chat_id"],
                        exc_info=True,
                    )
            await db.clear_event_notification_message(int(row["id"]))
            continue

        message_id = int(row["message_id"] or 0)
        if message_id:
            try:
                await app.state.bot.delete_message(
                    int(row["chat_id"]), message_id
                )
            except Exception as exc:
                if _is_transient_delete_error(exc):
                    continue
                logger.info(
                    "Не удалось удалить сообщение %s",
                    row["chat_id"],
                    exc_info=True,
                )
        await db.delete_event_notification_by_id(int(row["id"]))


async def _process_due_final_enrollments(now: int) -> None:
    for row in await db.list_due_final_enrollments(now):
        training = await db.get_training(int(row["entity_id"]))
        if training is None or training["deleted_at"]:
            await db.delete_event_notification_by_id(int(row["id"]))
            continue
        participants = await db.get_participants(training["id"])
        coach_name = await _training_coach_name(training)
        await _send_final_enrollment(
            training, row, participants, coach_name, now
        )


async def _cleanup_stale_preliminary(now: int) -> None:
    for row in await db.list_stale_preliminary_enrollments(now):
        await _delete_notification_row(row)


async def _training_notifications_loop(interval_seconds: int = 60) -> None:
    """Удаляет устаревшие сообщения и заменяет предварительные финальными."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            now = int(time.time())
            await _process_due_deletions(now)
            await _process_due_final_enrollments(now)
            await _cleanup_stale_preliminary(now)
        except asyncio.CancelledError:
            logger.info("Планировщик уведомлений остановлен.")
            raise
        except Exception:
            logger.exception("Ошибка в планировщике уведомлений")


def _match_participants_text(match: aiosqlite.Row, players: list[aiosqlite.Row]) -> str:
    names = ", ".join(
        html.escape(participant_identity(p)["first_name"]) for p in players[:25]
    )
    if len(players) > 25:
        names += "…"
    start = _local_time(match["start_at"])
    lines = [
        f"🏸 <b>{html.escape(match['title'] or 'Матч')}</b>",
        f"📅 {start.strftime('%d.%m.%Y в %H:%M')}",
        f"👥 {len(players)}/{match['max_players']}",
    ]
    if match["location"]:
        lines.append(f"📍 {html.escape(match['location'])}")
    if names:
        lines.append(names)
    if match["deleted_at"]:
        lines.append("❌ Матч отменён")
    return "\n".join(lines)


def _match_personal_text(match: aiosqlite.Row, status: str) -> str:
    header = (
        f"🏸 <b>{html.escape(match['title'] or 'Матч')}</b>\n"
        f"📅 {_local_time(match['start_at']).strftime('%d.%m.%Y в %H:%M')}"
    )
    if status == "left":
        return "Вы вышли из матча.\n\n" + header
    if status == "deleted":
        return "Матч отменён организатором.\n\n" + header
    if status == "pending":
        return "📝 Заявка на матч отправлена организатору.\n\n" + header
    if status == "rejected":
        return "К сожалению, заявка на матч отклонена.\n\n" + header
    if status == "waitlist":
        return "⏳ Вы в листе ожидания матча.\n\n" + header
    return "✅ Вы присоединились к матчу.\n\n" + header


def _match_update_text(
    match: aiosqlite.Row,
    players: list[aiosqlite.Row],
    status: str = "updated",
) -> str:
    """Одна карточка матча для участников: статус + актуальный состав."""
    if status == "joined":
        prefix = "✅ Вы присоединились к матчу."
    elif status == "left":
        prefix = "Вы вышли из матча."
    elif status == "deleted":
        prefix = "Матч отменён организатором."
    else:
        prefix = "🏸 Обновление матча."
    return prefix + "\n\n" + _match_participants_text(match, players)


def _match_request_keyboard(pending_count: int) -> InlineKeyboardMarkup:
    if not pending_count:
        return InlineKeyboardMarkup(inline_keyboard=[])
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=f"Открыть заявки · {pending_count}",
                web_app=WebAppInfo(url=bot_module._web_app_url()),
            )
        ]]
    )


async def _notify_match_request(match: aiosqlite.Row, user_id: int) -> None:
    """Заявка на матч: организатор видит её в приложении, игрок — статус."""
    players = await db.get_match_players(match["id"])
    booked = [p for p in players if _row_status(p) == "booked"]
    pending = [p for p in players if _row_status(p) == "pending"]
    text = _match_participants_text(match, booked)
    if pending:
        text += f"\n📝 Заявок: {len(pending)}"
    kwargs = (
        {"reply_markup": _match_request_keyboard(len(pending))}
        if pending else {}
    )
    if match["created_by"] and await _notifications_allowed(
        match["created_by"], "matches"
    ):
        await _send_event_notification(
            "match",
            match["id"],
            match["created_by"],
            0,
            text,
            **kwargs,
        )
    for coauthor in await db.list_event_coauthors("match", match["id"]):
        coauthor_id = int(coauthor["telegram_id"])
        if coauthor_id == match["created_by"]:
            continue
        if not await _notifications_allowed(coauthor_id, "matches"):
            continue
        await _send_event_notification(
            "match",
            match["id"],
            coauthor_id,
            0,
            text,
            **kwargs,
        )
    if user_id != match["created_by"] and await _notifications_allowed(
        user_id, "matches"
    ):
        await _send_event_notification(
            "match",
            match["id"],
            user_id,
            user_id,
            _match_personal_text(match, "pending"),
        )


async def _notify_match_event(
    match: aiosqlite.Row, user_id: int | None = None, status: str = "joined"
) -> None:
    players = await db.get_match_players(match["id"])
    booked = [p for p in players if _row_status(p) == "booked"]
    pending = [p for p in players if _row_status(p) == "pending"]
    text = _match_participants_text(match, booked)
    if pending:
        text += f"\n📝 Заявок: {len(pending)}"
    kwargs = (
        {"reply_markup": _match_request_keyboard(len(pending))}
        if pending else {}
    )

    if match["created_by"] and await _notifications_allowed(
        match["created_by"], "matches"
    ):
        await _send_event_notification(
            "match",
            match["id"],
            match["created_by"],
            0,
            text,
            **kwargs,
        )
    for coauthor in await db.list_event_coauthors("match", match["id"]):
        coauthor_id = int(coauthor["telegram_id"])
        if coauthor_id == match["created_by"]:
            continue
        if not await _notifications_allowed(coauthor_id, "matches"):
            continue
        await _send_event_notification(
            "match",
            match["id"],
            coauthor_id,
            0,
            text,
            **kwargs,
        )

    # Все подтверждённые участники видят актуальный состав в одном
    # редактируемом сообщении на матч.
    for player in booked:
        player_id = int(player["telegram_id"])
        if player_id == match["created_by"] or player_id == user_id:
            continue
        if not await _notifications_allowed(player_id, "matches"):
            continue
        await _send_event_notification(
            "match",
            match["id"],
            player_id,
            player_id,
            _match_update_text(match, booked),
        )

    if (
        user_id is not None
        and user_id != match["created_by"]
        and await _notifications_allowed(user_id, "matches")
    ):
        if status in {"joined", "left"}:
            text = _match_update_text(match, booked, status)
        else:
            text = _match_personal_text(match, status)
        await _send_event_notification(
            "match",
            match["id"],
            user_id,
            user_id,
            text,
        )


def _match_feed_text(matches: list[aiosqlite.Row]) -> str:
    if not matches:
        return "🏸 Ближайших матчей пока нет."
    lines = ["🏸 <b>Ближайшие матчи</b>"]
    for match in matches[:10]:
        start = _local_time(match["start_at"])
        line = (
            f"• {start.strftime('%d.%m в %H:%M')} — "
            f"{html.escape(match['title'] or 'Матч')}"
        )
        if match["location"]:
            line += f" · {html.escape(match['location'])}"
        lines.append(line)
    return "\n".join(lines)


async def _notify_match_feed(exclude_user_id: int | None = None) -> None:
    """Одна обновляемая лента матчей на каждого пользователя."""
    now = int(time.time())
    until = int((datetime.now(tz=settings.timezone) + timedelta(days=30)).timestamp())
    matches = await db.list_upcoming_matches(now, until)
    recipients = await db.list_match_feed_recipients()
    text = _match_feed_text(matches)
    for row in recipients:
        user_id = row["telegram_id"]
        if exclude_user_id is not None and user_id == exclude_user_id:
            continue
        await _send_event_notification(
            "match_feed", 0, user_id, user_id, text
        )


async def _notify_match_deleted(match: aiosqlite.Row) -> None:
    players = await db.get_match_players(match["id"])
    if match["created_by"] and await _notifications_allowed(
        match["created_by"], "matches"
    ):
        await _send_event_notification(
            "match",
            match["id"],
            match["created_by"],
            0,
            _match_participants_text(match, players),
        )
    for row in players:
        if row["telegram_id"] != match["created_by"] and await _notifications_allowed(
            row["telegram_id"], "matches"
        ):
            await _send_event_notification(
                "match",
                match["id"],
                row["telegram_id"],
                row["telegram_id"],
                _match_personal_text(match, "deleted"),
            )


async def _notify_training_transferred(new_coach_id: int, training: aiosqlite.Row) -> None:
    if not await _notifications_allowed(new_coach_id, "trainings"):
        return
    await _send_user_message(
        new_coach_id,
        "Вам передали занятие.\n\n"
        f"🏸 {_training_brief(training)}",
    )


@app.get("/api/coach/trainings/{training_id}/coauthors")
async def api_training_coauthors(
    training_id: int, coach: Annotated[dict, Depends(require_coach)]
):
    training = await db.get_training(training_id)
    if training is None or training["deleted_at"]:
        return error_json("Тренировка не найдена", 404)
    rows = await db.list_event_coauthors("training", training_id)
    return {
        "ok": True,
        "can_manage": training["created_by"] == coach["telegram_id"],
        "coauthors": [
            {
                "telegram_id": r["telegram_id"],
                "first_name": r["first_name"] or "Тренер",
                "username": r["username"],
                "avatar_url": _avatar_url(
                    r["telegram_id"], real=True, file_id=_row_file_id(r)
                ),
            }
            for r in rows
        ],
    }


@app.post("/api/coach/trainings/{training_id}/coauthors")
async def api_add_training_coauthor(
    training_id: int,
    payload: CoauthorIn,
    coach: Annotated[dict, Depends(require_coach)],
):
    training = await db.get_training(training_id)
    if training is None or training["deleted_at"]:
        return error_json("Тренировка не найдена", 404)
    if training["created_by"] != coach["telegram_id"]:
        return error_json("Добавлять соавторов может только создатель.", 403)
    target = await _resolve_user_id(payload)
    if target is None:
        return error_json("Тренер не найден.", 404)
    if target == training["created_by"]:
        return error_json("Создатель уже управляет занятием.", 400)
    if not await db.is_coach_id(target):
        return error_json("Соавтором тренировки может быть только тренер.", 400)
    created = await db.add_event_coauthor(
        "training", training_id, target, coach["telegram_id"]
    )
    return {"ok": True, "created": created, "telegram_id": target}


@app.delete("/api/coach/trainings/{training_id}/coauthors/{user_id}")
async def api_remove_training_coauthor(
    training_id: int, user_id: int, coach: Annotated[dict, Depends(require_coach)]
):
    training = await db.get_training(training_id)
    if training is None or training["deleted_at"]:
        return error_json("Тренировка не найдена", 404)
    if training["created_by"] != coach["telegram_id"]:
        return error_json("Снимать соавторов может только создатель.", 403)
    removed = await db.remove_event_coauthor("training", training_id, user_id)
    return {"ok": True, "removed": removed}


@app.get("/api/matches/{match_id}/coauthors")
async def api_match_coauthors(
    match_id: int, user: Annotated[dict, Depends(current_user)]
):
    match = await db.get_match(match_id)
    if match is None or match["deleted_at"]:
        return error_json("Матч не найден", 404)
    rows = await db.list_event_coauthors("match", match_id)
    return {
        "ok": True,
        "can_manage": match["created_by"] == user["telegram_id"],
        "coauthors": [
            {
                "telegram_id": r["telegram_id"],
                "first_name": r["first_name"] or "Игрок",
                "username": r["username"],
                "avatar_url": _avatar_url(
                    r["telegram_id"], real=True, file_id=_row_file_id(r)
                ),
            }
            for r in rows
        ],
    }


@app.post("/api/matches/{match_id}/coauthors")
async def api_add_match_coauthor(
    match_id: int, payload: CoauthorIn, user: Annotated[dict, Depends(current_user)]
):
    match = await db.get_match(match_id)
    if match is None or match["deleted_at"]:
        return error_json("Матч не найден", 404)
    if match["created_by"] != user["telegram_id"]:
        return error_json("Добавлять соавторов может только организатор.", 403)
    target = await _resolve_user_id(payload)
    if target is None:
        return error_json("Игрок не найден. Попросите его запустить бота.", 404)
    if target == match["created_by"]:
        return error_json("Организатор уже управляет матчем.", 400)
    created = await db.add_event_coauthor(
        "match", match_id, target, user["telegram_id"]
    )
    return {"ok": True, "created": created, "telegram_id": target}


@app.delete("/api/matches/{match_id}/coauthors/{user_id}")
async def api_remove_match_coauthor(
    match_id: int, user_id: int, user: Annotated[dict, Depends(current_user)]
):
    match = await db.get_match(match_id)
    if match is None or match["deleted_at"]:
        return error_json("Матч не найден", 404)
    if match["created_by"] != user["telegram_id"]:
        return error_json("Снимать соавторов может только организатор.", 403)
    removed = await db.remove_event_coauthor("match", match_id, user_id)
    return {"ok": True, "removed": removed}


# --------------------------------------------------------------------------- #
# API: реестр тренеров, заявки, клубный просмотр и передача занятий
# --------------------------------------------------------------------------- #

@app.get("/api/coach/coaches")
async def api_list_coaches(coach: Annotated[dict, Depends(require_coach)]):
    """Реестр тренеров (доступен любому тренеру — для передачи занятий)."""
    rows = await db.list_coaches()
    return {
        "ok": True,
        "coaches": [
            {
                "telegram_id": r["telegram_id"],
                "first_name": r["first_name"] or "Тренер",
                "username": r["username"] if r["show_username"] else None,
                "username_visible": bool(r["show_username"]),
                "avatar_url": _avatar_url(
                    r["telegram_id"], real=True, file_id=_row_file_id(r)
                ),
                "is_owner": r["telegram_id"] == settings.coach_telegram_id,
                "is_manager": bool(r["is_manager"]),
            }
            for r in rows
        ],
    }


@app.post("/api/coach/coaches")
async def api_add_coach(payload: CoachIn, manager: Annotated[dict, Depends(require_manager)]):
    """
    Добавляет тренера (владелец или менеджер). Принимает telegram_id или
    @username (если пользователь уже запускал бота).
    """
    try:
        tg_id = payload.telegram_id
        if tg_id is None and payload.username:
            found = await db.find_user_by_username(payload.username)
            if found is None:
                return error_json(
                    "Пользователь не найден. Попросите его сначала запустить бота (/start).", 404
                )
            tg_id = found["telegram_id"]
        if tg_id is None:
            return error_json("Укажите telegram_id или @username.", 422)
        if tg_id == manager["telegram_id"]:
            return error_json("Это ваш собственный аккаунт.", 400)

        created = await db.add_coach(tg_id, manager["telegram_id"])
        if created:
            await db.decide_application(tg_id, "approved", manager["telegram_id"])
            _spawn(_notify_new_coach(tg_id))

        return {"ok": True, "created": created, "telegram_id": tg_id}
    except Exception:
        logger.exception("Ошибка добавления тренера")
        return error_json("Внутренняя ошибка сервера", 500)


async def _notify_new_coach(tg_id: int) -> None:
    """Фоновое уведомление новому тренеру со ссылкой на Web App."""
    try:
        markup = None
        if settings.webhook_base_url:
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="🏸 Открыть тренировки",
                    web_app=WebAppInfo(url=bot_module._web_app_url()),
                )
            ]])
        await app.state.bot.send_message(
            tg_id,
            "Вам выдан доступ тренера 🏸\nОткройте приложение — появится вкладка «Тренер».",
            reply_markup=markup,
        )
    except Exception:
        logger.info("Не удалось уведомить нового тренера %s", tg_id, exc_info=True)


@app.delete("/api/coach/coaches/{telegram_id}")
async def api_remove_coach(telegram_id: int, manager: Annotated[dict, Depends(require_manager)]):
    """Снимает роль тренера (владельца снять нельзя)."""
    if telegram_id == settings.coach_telegram_id:
        return error_json("Нельзя снять владельца.", 400)
    await db.remove_coach(telegram_id)
    return {"ok": True}


@app.patch("/api/coach/coaches/{telegram_id}/manager")
async def api_set_manager(
    telegram_id: int,
    payload: ManagerIn,
    owner: Annotated[dict, Depends(require_owner)],
):
    """Выдаёт/снимает права менеджера (только владелец)."""
    if telegram_id == settings.coach_telegram_id:
        return error_json("Владелец и так управляет реестром.", 400)
    if not await db.is_coach_id(telegram_id):
        return error_json("Сначала добавьте тренера.", 404)
    await db.set_manager(telegram_id, payload.enabled)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# API: заявки в тренеры
# --------------------------------------------------------------------------- #

@app.post("/api/coach/apply")
async def api_apply(payload: ApplyIn, user: Annotated[dict, Depends(current_user)]):
    """Заявка пользователя на роль тренера (идемпотентна)."""
    _apply_limiter.check(str(user["telegram_id"]))
    if user.get("is_coach"):
        return error_json("Вы уже тренер.", 400)
    await db.apply_for_coach(user["telegram_id"], (payload.message or "").strip()[:1000])
    applicant_name = (
        animal_identity(user["telegram_id"])["name"]
        if user.get("display_mode") == "animal"
        else (user["first_name"] or "Пользователь")
    )
    _spawn(_notify_managers_about_application(user["telegram_id"], applicant_name))
    return {"ok": True}


@app.get("/api/coach/apply")
async def api_apply_status(user: Annotated[dict, Depends(current_user)]):
    """Статус своей заявки (и являюсь ли тренером)."""
    row = await db.get_application(user["telegram_id"])
    return {
        "ok": True,
        "is_coach": bool(user.get("is_coach")),
        "application": (
            {"status": row["status"], "message": row["message"]} if row is not None else None
        ),
    }


@app.get("/api/coach/applications")
async def api_applications(manager: Annotated[dict, Depends(require_manager)]):
    """Ожидающие заявки (владелец или менеджер)."""
    rows = await db.list_applications("pending")
    return {
        "ok": True,
        "applications": [
            {
                "telegram_id": r["telegram_id"],
                **participant_identity(r),
                "username": r["username"] if r["show_username"] else None,
                "username_visible": bool(r["show_username"]),
                "message": r["message"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


@app.post("/api/coach/applications/{telegram_id}/approve")
async def api_approve_application(
    telegram_id: int, manager: Annotated[dict, Depends(require_manager)]
):
    application = await db.get_application(telegram_id)
    if application is None:
        return error_json("Заявка не найдена", 404)
    if application["status"] != "pending":
        return error_json("Заявка уже рассмотрена", 409)
    await db.add_coach(telegram_id, manager["telegram_id"])
    await db.decide_application(telegram_id, "approved", manager["telegram_id"])
    _spawn(_notify_new_coach(telegram_id))
    return {"ok": True}


@app.post("/api/coach/applications/{telegram_id}/reject")
async def api_reject_application(
    telegram_id: int, manager: Annotated[dict, Depends(require_manager)]
):
    application = await db.get_application(telegram_id)
    if application is None:
        return error_json("Заявка не найдена", 404)
    if application["status"] != "pending":
        return error_json("Заявка уже рассмотрена", 409)
    await db.decide_application(telegram_id, "rejected", manager["telegram_id"])
    _spawn(_notify_application_decision(telegram_id, approved=False))
    return {"ok": True}


async def _notify_application_decision(telegram_id: int, *, approved: bool) -> None:
    """Фоновое уведомление заявителю о решении по заявке (Web App)."""
    text = (
        "🎉 Ваша заявка одобрена! Откройте приложение — появится вкладка «Тренер»."
        if approved
        else "Ваша заявка на тренера отклонена."
    )
    try:
        await app.state.bot.send_message(telegram_id, text)
    except Exception:
        logger.info(
            "Не удалось уведомить заявителя %s", telegram_id, exc_info=True
        )


async def _notify_managers_about_application(applicant_id: int, name: str) -> None:
    """Фоновая рассылка о новой заявке владельцу и менеджерам."""
    try:
        recipients = {settings.coach_telegram_id}
        recipients.update(r["telegram_id"] for r in await db.list_managers())
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"apply:ok:{applicant_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"apply:no:{applicant_id}"),
        ]])
        for rid in recipients:
            if not await _notifications_allowed(rid, "messages"):
                continue
            try:
                await app.state.bot.send_message(
                    rid,
                    f"📝 <b>Заявка в тренеры</b>\n{html.escape(name)}",
                    reply_markup=keyboard,
                )
            except Exception:
                logger.info("Не удалось уведомить о заявке %s", rid, exc_info=True)
    except Exception:
        logger.exception("Ошибка рассылки заявки")


# --------------------------------------------------------------------------- #
# API: клубный просмотр (только чтение) и передача занятий
# --------------------------------------------------------------------------- #

@app.get("/api/coach/club/trainings")
async def api_club_trainings(coach: Annotated[dict, Depends(require_coach)]):
    """Предстоящие занятия других тренеров (только просмотр)."""
    rows = await db.list_other_active_trainings(int(time.time()), coach["telegram_id"])
    participants_by_training = await db.get_participants_for_trainings(
        [r["id"] for r in rows]
    )
    cache: dict[Any, dict[str, Any] | None] = {}
    result = []
    for row in rows:
        creator = row["created_by"]
        if creator not in cache:
            cache[creator] = serialize_coach(await db.get_user(creator) if creator else None)
        item = serialize_training(row, viewer_id=coach["telegram_id"])
        item["coach"] = cache[creator]
        item["pending_count"] = sum(
            1
            for p in participants_by_training.get(row["id"], [])
            if _row_status(p) == "pending"
        )
        result.append(item)
    return {"ok": True, "trainings": result}


@app.post("/api/coach/trainings/{training_id}/transfer")
async def api_transfer_training(
    training_id: int,
    payload: CoachIn,
    coach: Annotated[dict, Depends(require_coach)],
):
    """Передаёт занятие другому тренеру (автор занятия или владелец бота)."""
    try:
        training = await db.get_training(training_id)
        if training is None or training["deleted_at"] or (
            training["created_by"] != coach["telegram_id"] and not coach.get("is_owner")
        ):
            return error_json("Тренировка не найдена", 404)

        target = payload.telegram_id
        if target is None and payload.username:
            found = await db.find_user_by_username(payload.username)
            target = found["telegram_id"] if found is not None else None
        if target is None:
            return error_json("Укажите тренера (telegram_id).", 422)
        if not await db.is_coach_id(target):
            return error_json("Получатель должен быть тренером.", 400)
        if target == training["created_by"]:
            return error_json("Занятие уже принадлежит этому тренеру.", 400)

        await db.set_training_owner(training_id, target)
        _spawn(_notify_training_transferred(target, training))
        return {"ok": True, "telegram_id": target}
    except Exception:
        logger.exception("Ошибка передачи занятия")
        return error_json("Внутренняя ошибка сервера", 500)


# --------------------------------------------------------------------------- #
# Прокси аватарок (без сохранения файлов на диск)
# --------------------------------------------------------------------------- #

def _etag_matches(header: str, etag: str) -> bool:
    """Сравнивает ``If-None-Match`` с текущим ETag (включая ``W/``)."""
    for candidate in header.split(","):
        value = candidate.strip()
        if value in {"*", etag, f"W/{etag}"}:
            return True
    return False


def _avatar_response(
    request: Request,
    content: bytes,
    media_type: str,
    version: str,
    *,
    immutable: bool,
) -> Response:
    """Ответ с ETag/304 и правильным кэшированием версионированного URL."""
    etag = f'"{version}"'
    cache_control = (
        f"private, max-age={AVATAR_URL_TTL_SECONDS}, immutable"
        if immutable
        else "private, no-cache"
    )
    headers = {"Cache-Control": cache_control, "ETag": etag}
    if _etag_matches(request.headers.get("if-none-match", ""), etag):
        return Response(status_code=304, headers=headers)
    headers["Content-Length"] = str(len(content))
    return Response(content, media_type=media_type, headers=headers)


async def _download_avatar(
    telegram_id: int, file_id: str | None
) -> tuple[bytes, str] | None:
    """Скачивает файл из Telegram; ``None`` — фото нет, исключение — сбой."""
    if not file_id:
        return None
    try:
        file = await app.state.bot.get_file(file_id)
        if not file.file_path:
            return None
        buffer = await app.state.bot.download_file(file.file_path)
    except Exception as exc:
        logger.warning("Не удалось скачать аватарку %s", telegram_id, exc_info=True)
        raise HTTPException(status_code=502, detail="Telegram недоступен.") from exc

    content = buffer.getvalue()
    if not content:
        return None
    if len(content) > _AVATAR_MAX_BYTES:
        logger.warning(
            "Аватарка %s слишком большая: %d байт", telegram_id, len(content)
        )
        raise HTTPException(status_code=502, detail="Аватарка слишком большая.")
    return content, sniff_media_type(content)


async def _serve_avatar(
    request: Request,
    telegram_id: int,
    token: str,
    requested_version: str | None,
) -> Response:
    """Общая логика версионированного и старого (без версии) URL."""
    real = (
        verify_scoped_token(settings.bot_token, token, "avatar_real", telegram_id)
        == telegram_id
    )
    if not real and verify_scoped_token(
        settings.bot_token, token, "avatar", telegram_id
    ) != telegram_id:
        raise HTTPException(status_code=401, detail="Требуется авторизация.")

    row = await db.get_user(telegram_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Аватарка недоступна.")
    if not real and row["display_mode"] == "animal":
        # Пользователь выбрал анонимный режим: публичная ссылка больше не
        # должна отдавать его фотографию, даже если старый токен ещё жив.
        raise HTTPException(status_code=404, detail="Аватарка скрыта.")

    file_id = row["avatar_file_id"]
    store = _avatar_store()
    version = avatar_version(settings.bot_token, telegram_id, file_id)
    if not file_id:
        # Пользователь мог запустить бота до появления кэша аватарок.
        # Negative cache не даёт дёргать Telegram на каждый <img>.
        if store.is_missing(telegram_id, version):
            raise HTTPException(status_code=404, detail="Аватарка недоступна.")
        await bot_module._cache_avatar(app.state.bot, telegram_id)
        row = await db.get_user(telegram_id)
        file_id = row["avatar_file_id"] if row is not None else None
        version = avatar_version(settings.bot_token, telegram_id, file_id)

    cached = await store.get(telegram_id, version)

    if cached is None:
        if store.is_missing(telegram_id, version):
            raise HTTPException(status_code=404, detail="Аватарка недоступна.")
        async with store.single_flight(telegram_id, version):
            cached = await store.get(telegram_id, version)
            if cached is None:
                downloaded = await _download_avatar(telegram_id, file_id)
                if downloaded is None:
                    store.mark_missing(telegram_id, version)
                    raise HTTPException(status_code=404, detail="Аватарка недоступна.")
                cached = downloaded
                await store.put(telegram_id, version, *cached)

    content, media_type = cached
    # Старый URL без версии и URL с устаревшей версией не кэшируем надолго:
    # следующая сериализация уже отдаст актуальный версионированный адрес.
    immutable = requested_version == version
    return _avatar_response(
        request, content, media_type, version, immutable=immutable
    )


@app.get("/api/avatar/{telegram_id}")
async def api_avatar_legacy(telegram_id: int, request: Request, token: str = "") -> Response:
    """Старый URL без версии: работает, но не кэшируется (для открытых страниц)."""
    return await _serve_avatar(request, telegram_id, token, requested_version=None)


@app.get("/api/avatar/{telegram_id}/{version}")
async def api_avatar(
    telegram_id: int, version: str, request: Request, token: str = ""
) -> Response:
    """
    Отдаёт аватарку из кэша или Telegram.

    Авторизация — узким токеном из ``avatar_url``: браузерный <img> не может
    передать заголовок X-Init-Data, а полный initData в URL светить нельзя.
    Версия в пути меняется только при смене фото, поэтому браузер кэширует
    картинку между рендерами и после действий тренера.
    """
    return await _serve_avatar(request, telegram_id, token, requested_version=version)


# --------------------------------------------------------------------------- #
# Вебхук Telegram (используется только при BOT_MODE=webhook)
# --------------------------------------------------------------------------- #

@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Annotated[str, Header()] = "",
):
    # Маршрут существует только для webhook-режима. В polling он не нужен:
    # иначе при пустом/известном секрете можно подделать апдейт от имени владельца.
    if settings.bot_mode != "webhook":
        raise HTTPException(status_code=404, detail="Not Found")
    if not settings.webhook_secret or not hmac.compare_digest(
        x_telegram_bot_api_secret_token.encode("utf-8"),
        settings.webhook_secret.encode("utf-8"),
    ):
        return error_json("Forbidden", 403)
    try:
        payload = await request.json()
        update = Update.model_validate(payload)
        await app.state.dp.feed_webhook_update(app.state.bot, update)
        return {"ok": True}
    except Exception:
        logger.exception("Ошибка обработки вебхука")
        return {"ok": False}
