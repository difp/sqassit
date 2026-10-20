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
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

import aiosqlite
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import bot as bot_module
from . import database as db
from .config import (
    BOOKING_CANCEL_LOCK_MINUTES,
    DEFAULT_REMINDER_OFFSET_MINUTES,
    DEFAULT_TRAINING_DURATION_MINUTES,
    TRAINING_LEVELS,
    settings,
)
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

# Простой in-memory кэш аватарок (TTL), на диск ничего не пишем.
_avatar_cache: dict[int, tuple[float, bytes, str]] = {}
_AVATAR_TTL_SECONDS = 60 * 60
_AVATAR_CACHE_MAX_ENTRIES = 256


_bot_check_cache: tuple[float, bool] = (0.0, False)
_BOT_CHECK_TTL_SECONDS = 60

# Защита от спама/перебора. Лимиты подобраны для небольшого клуба.
_message_limiter = RateLimiter(20, 60)      # личные сообщения тренеру
_apply_limiter = RateLimiter(5, 300)        # заявки в тренеры
_booking_limiter = RateLimiter(60, 60)      # book/cancel


async def _bot_is_ready() -> bool:
    """Проверяет доступность Telegram Bot API с кэшем, чтобы не дёргать его зря."""
    global _bot_check_cache
    now = time.monotonic()
    cached_at, cached_ok = _bot_check_cache
    if now - cached_at < _BOT_CHECK_TTL_SECONDS:
        return cached_ok
    try:
        me = await app.state.bot.get_me()
        ok = me is not None
    except Exception:
        logger.warning("Проверка Telegram Bot API не прошла", exc_info=True)
        ok = False
    _bot_check_cache = (now, ok)
    return ok


def _store_avatar(telegram_id: int, content: bytes, media_type: str) -> None:
    """Кладёт аватарку в память, вытесняя просроченные/самые старые записи."""
    now = time.time()
    for key, (created_at, _, _) in list(_avatar_cache.items()):
        if now - created_at >= _AVATAR_TTL_SECONDS:
            _avatar_cache.pop(key, None)
    if len(_avatar_cache) >= _AVATAR_CACHE_MAX_ENTRIES:
        oldest = min(_avatar_cache, key=lambda key: _avatar_cache[key][0])
        _avatar_cache.pop(oldest, None)
    _avatar_cache[telegram_id] = (now, content, media_type)


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


ANIMALS: tuple[tuple[str, str], ...] = (
    ("🦊", "Лиса"),
    ("🐻", "Медведь"),
    ("🐼", "Панда"),
    ("🦉", "Сова"),
    ("🦦", "Выдра"),
    ("🦔", "Ёж"),
    ("🦡", "Барсук"),
    ("🐧", "Пингвин"),
    ("🐰", "Кролик"),
    ("🐸", "Лягушка"),
    ("🐯", "Тигр"),
    ("🐨", "Коала"),
)


def animal_identity(telegram_id: int) -> dict[str, str]:
    """Стабильное вымышленное животное для анонимного отображения."""
    emoji, name = ANIMALS[int(telegram_id) % len(ANIMALS)]
    return {"emoji": emoji, "name": name}


def _avatar_url(telegram_id: int, *, real: bool = False) -> str:
    """
    Возвращает URL аватарки с узким токеном.

    ``real=True`` — только для ответов самому пользователю и тренерам:
    такой токен обходит режим «животное». Публичные карточки используют
    обычный токен, который уважает настройку приватности.
    """
    kind = "avatar_real" if real else "avatar"
    token = issue_scoped_token(
        settings.bot_token, kind, telegram_id, telegram_id, ttl_seconds=86400
    )
    return f"/api/avatar/{telegram_id}?{urlencode({'token': token})}"


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


def serialize_participant(
    row: aiosqlite.Row, *, show_real: bool = False
) -> dict[str, Any]:
    """
    Участник для «социальной сетки».

    Игроки видят только имя/фото или вымышленное животное — без username,
    telegram_id и «о себе». Тренер видит реальные данные для управления
    занятием.
    """
    first_name = row["first_name"] or "Игрок"
    if not show_real and row["display_mode"] == "animal":
        animal = animal_identity(row["telegram_id"])
        return {
            "first_name": animal["name"],
            "status": row["status"],
            "avatar_url": "",
            "avatar_emoji": animal["emoji"],
            "anonymous": True,
        }
    return {
        "first_name": first_name,
        "status": row["status"],
        "avatar_url": _avatar_url(row["telegram_id"], real=show_real),
        "avatar_emoji": "",
        "anonymous": False,
    }


def serialize_coach(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    """Публичный профиль тренера для карточки тренировки (без username)."""
    if row is None:
        return None
    return {
        "telegram_id": row["telegram_id"],
        "first_name": row["first_name"] or "Тренер",
        "avatar_url": _avatar_url(row["telegram_id"], real=True),
        "bio": row["bio"] or "",
    }


def sanitize_bio(value: str) -> str:
    """Обрезает текст профиля (био тренера или «о себе» игрока)."""
    return (value or "").strip()[:300]


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


class ReminderIn(BaseModel):
    enabled: bool
    offset_min: int = Field(default=DEFAULT_REMINDER_OFFSET_MINUTES, ge=0, le=2880)


class NotesIn(BaseModel):
    notes: str = Field(default="", max_length=4000)


class NotificationsIn(BaseModel):
    enabled: bool


class TrainerProfileIn(BaseModel):
    bio: str = Field(default="", max_length=300)


class PlayerProfileIn(BaseModel):
    about: str = Field(default="", max_length=300)
    display_mode: Literal["name", "animal"] = "name"
    show_username: bool = False


class CoachIn(BaseModel):
    telegram_id: int | None = None
    username: str | None = Field(default=None, max_length=64)


class ManagerIn(BaseModel):
    enabled: bool


class ApplyIn(BaseModel):
    message: str = Field(default="", max_length=1000)


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    training_id: int | None = None


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

    # 5. Фоновый планировщик напоминаний.
    tasks.append(asyncio.create_task(bot_module.reminder_loop(tg_bot)))

    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
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
            "avatar_url": _avatar_url(user["telegram_id"], real=True),
            "about": user["about"] or "",
            "display_mode": user["display_mode"] or "name",
            "show_username": bool(user["show_username"]),
            "animal": animal_identity(user["telegram_id"]),
        },
    }


@app.put("/api/me/profile")
async def api_set_player_profile(
    payload: PlayerProfileIn,
    user: Annotated[dict, Depends(current_user)],
):
    """Сохраняет «о себе» и настройки показа в списке участников."""
    try:
        await db.set_player_profile(
            user["telegram_id"],
            about=sanitize_bio(payload.about),
            display_mode=payload.display_mode,
            show_username=payload.show_username,
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
                "animal": animal_identity(user["telegram_id"]),
            },
        }
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
    await db.set_notifications_enabled(user["telegram_id"], payload.enabled)
    return {"ok": True, "notifications_enabled": payload.enabled}


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
            "notifications_enabled": bool(user["notifications_enabled"]),
            "notes": user["notes"],
            "about": user["about"] or "",
            "display_mode": user["display_mode"] or "name",
            "show_username": bool(user["show_username"]),
        },
        "bookings": [dict(row) for row in await db.list_user_bookings(user["telegram_id"])],
        "messages": [dict(row) for row in await db.list_user_messages(user["telegram_id"])],
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
            asyncio.create_task(_notify_promoted(int(promoted), training))

    await db.delete_user_data(user["telegram_id"])
    _avatar_cache.pop(user["telegram_id"], None)
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
        show_real = bool(user.get("is_coach"))
        item["participants"] = [
            serialize_participant(p, show_real=show_real)
            for p in participants
            if p["status"] == "booked"
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

        status = await db.book_training(training_id, user["telegram_id"], training["capacity"])
        msg = "Вы записаны!" if status == "booked" else "Мест нет — вы в листе ожидания."
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
        if booking is None or booking["status"] not in {"booked", "waitlist"}:
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
        if promoted:
            asyncio.create_task(_notify_promoted(int(promoted), training))
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
# API: тренер
# --------------------------------------------------------------------------- #

@app.get("/api/coach/trainings")
async def api_coach_trainings(coach: Annotated[dict, Depends(require_coach)]):
    """Предстоящие тренировки ТЕКУЩЕГО тренера (только свои)."""
    rows = await db.list_active_trainings(int(time.time()), coach["telegram_id"])
    return {
        "ok": True,
        "trainings": [
            serialize_training(r, viewer_id=coach["telegram_id"]) for r in rows
        ],
    }


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
        asyncio.create_task(_notify_training_deleted(training))
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
                    "telegram_id": r["telegram_id"],
                    "first_name": r["first_name"],
                    "username": r["username"] if r["show_username"] else None,
                    "username_visible": bool(r["show_username"]),
                    "about": r["about"] or "",
                    "status": r["status"],
                    "avatar_url": _avatar_url(r["telegram_id"], real=True),
                }
                for r in rows
            ],
        }
    except Exception:
        logger.exception("Ошибка чтения участников")
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
        asyncio.create_task(_notify_participant_removed(user_id, training))
        if promoted:
            asyncio.create_task(_notify_promoted(int(promoted), training))
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
        asyncio.create_task(_notify_participant_restored(user_id, training, status))
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
                "telegram_id": r["telegram_id"],
                "first_name": r["first_name"],
                "username": r["username"] if r["show_username"] else None,
                "username_visible": bool(r["show_username"]),
                "about": r["about"] or "",
                "display_mode": r["display_mode"] or "name",
                "avatar_url": _avatar_url(r["telegram_id"], real=True),
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
    Тренер может прочитать всё командой /inbox и ответить /reply.
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
    asyncio.create_task(
        _deliver_to_coach(coach_id, user["telegram_id"], user["first_name"] or "Игрок", text)
    )
    return {"ok": True}


async def _deliver_to_coach(coach_id: int, sender_id: int, sender_name: str, text: str) -> None:
    """Фоновая доставка сообщения тренеру с кнопкой быстрого ответа."""
    try:
        sender = html.escape(sender_name)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="✍️ Ответить", callback_data=f"reply:{sender_id}")
            ]]
        )
        await app.state.bot.send_message(
            coach_id,
            f"✉️ <b>Сообщение от {sender}</b>\n"
            f"<code>id: {sender_id}</code>\n\n"
            f"{html.escape(text)}",
            reply_markup=keyboard,
        )
    except Exception:
        logger.warning("Не удалось доставить сообщение тренеру", exc_info=True)


def _training_brief(training: aiosqlite.Row) -> str:
    """Короткое описание занятия для уведомлений (HTML-экранированное)."""
    start = _local_time(training["start_at"])
    training_type = html.escape(training["training_type"] or "Тренировка")
    return f"{training_type} · {start.strftime('%d.%m.%Y в %H:%M')}"


async def _send_user_message(telegram_id: int, text: str) -> None:
    """Best-effort уведомление пользователя; недоступность Telegram не ломает API."""
    try:
        await app.state.bot.send_message(telegram_id, text)
    except Exception:
        logger.info("Не удалось отправить уведомление %s", telegram_id, exc_info=True)


async def _notify_promoted(user_id: int, training: aiosqlite.Row) -> None:
    await _send_user_message(
        user_id,
        "🎉 Освободилось место — вы записаны!\n\n"
        f"🏸 {_training_brief(training)}",
    )


async def _notify_participant_removed(user_id: int, training: aiosqlite.Row) -> None:
    await _send_user_message(
        user_id,
        "Тренер снял вас с тренировки.\n\n"
        f"🏸 {_training_brief(training)}",
    )


async def _notify_participant_restored(
    user_id: int, training: aiosqlite.Row, status: str
) -> None:
    if status == "booked":
        text = "Тренер вернул вас на тренировку.\n\n"
    else:
        text = "Тренер вернул вас в лист ожидания.\n\n"
    await _send_user_message(user_id, text + f"🏸 {_training_brief(training)}")


async def _notify_training_deleted(training: aiosqlite.Row) -> None:
    text = (
        "Тренировка отменена тренером.\n\n"
        f"🏸 {_training_brief(training)}"
    )
    for row in await db.notification_targets(training["id"]):
        await _send_user_message(row["telegram_id"], text)


async def _notify_training_transferred(new_coach_id: int, training: aiosqlite.Row) -> None:
    await _send_user_message(
        new_coach_id,
        "Вам передали занятие.\n\n"
        f"🏸 {_training_brief(training)}",
    )


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
                "avatar_url": _avatar_url(r["telegram_id"], real=True),
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
            asyncio.create_task(_notify_new_coach(tg_id))

        return {"ok": True, "created": created, "telegram_id": tg_id}
    except Exception:
        logger.exception("Ошибка добавления тренера")
        return error_json("Внутренняя ошибка сервера", 500)


async def _notify_new_coach(tg_id: int) -> None:
    """Фоновое уведомление новому тренеру со ссылкой на Web App."""
    try:
        base = settings.webhook_base_url.rstrip("/")
        markup = None
        if base:
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🏸 Открыть тренировки", web_app=WebAppInfo(url=base))
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
    asyncio.create_task(
        _notify_managers_about_application(user["telegram_id"], user["first_name"] or "Пользователь")
    )
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
                "first_name": r["first_name"] or "Пользователь",
                "username": r["username"] if r["show_username"] else None,
                "username_visible": bool(r["show_username"]),
                "avatar_url": _avatar_url(r["telegram_id"], real=True),
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
    asyncio.create_task(_notify_new_coach(telegram_id))
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
    asyncio.create_task(_notify_application_decision(telegram_id, approved=False))
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
            try:
                await app.state.bot.send_message(
                    rid,
                    f"📝 <b>Заявка в тренеры</b>\n{html.escape(name)} (<code>{applicant_id}</code>)",
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
    cache: dict[Any, dict[str, Any] | None] = {}
    result = []
    for row in rows:
        creator = row["created_by"]
        if creator not in cache:
            cache[creator] = serialize_coach(await db.get_user(creator) if creator else None)
        item = serialize_training(row, viewer_id=coach["telegram_id"])
        item["coach"] = cache[creator]
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
        asyncio.create_task(_notify_training_transferred(target, training))
        return {"ok": True, "telegram_id": target}
    except Exception:
        logger.exception("Ошибка передачи занятия")
        return error_json("Внутренняя ошибка сервера", 500)


# --------------------------------------------------------------------------- #
# Прокси аватарок (без сохранения файлов на диск)
# --------------------------------------------------------------------------- #

@app.get("/api/avatar/{telegram_id}")
async def api_avatar(telegram_id: int, token: str = ""):
    """
    Отдаёт аватарку из Telegram через бота, не раскрывая токен и не сохраняя файл.

    Авторизация — узким токеном из ``avatar_url``: браузерный <img> не может
    передать заголовок X-Init-Data, а полный initData в URL светить нельзя.
    """
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

    cached = _avatar_cache.get(telegram_id)
    if cached and time.time() - cached[0] < _AVATAR_TTL_SECONDS:
        return StreamingResponse(
            iter([cached[1]]),
            media_type=cached[2],
            headers={"Cache-Control": "private, max-age=3600"},
        )

    if not row["avatar_file_id"]:
        # Пользователь мог запустить бота до появления кэша аватарок.
        await bot_module._cache_avatar(app.state.bot, telegram_id)
        row = await db.get_user(telegram_id)
    if row is None or not row["avatar_file_id"]:
        raise HTTPException(status_code=404, detail="Аватарка недоступна.")

    tg_bot = app.state.bot
    try:
        file = await tg_bot.get_file(row["avatar_file_id"])
        if not file.file_path:
            raise HTTPException(status_code=404, detail="Аватарка недоступна.")
        buffer = await tg_bot.download_file(file.file_path)
        content = buffer.getvalue()
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Не удалось скачать аватарку %s", telegram_id, exc_info=True)
        raise HTTPException(status_code=502, detail="Telegram недоступен.") from exc

    media_type = "image/png"
    if file.file_path.lower().endswith((".jpg", ".jpeg")):
        media_type = "image/jpeg"
    _store_avatar(telegram_id, content, media_type)
    return StreamingResponse(
        iter([content]),
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


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
