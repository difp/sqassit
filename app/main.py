"""
Точка входа приложения: FastAPI (HTTP/Web App API) + aiogram (Telegram-бот).

Один монолитный процесс обслуживает:
  * Telegram-бота (polling или webhook);
  * REST API Web App;
  * HTML-страницы (Jinja2 + TailwindCSS).
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Optional

import aiosqlite
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
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
from .secure_auth import InitDataError, validate_init_data

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
    await db.upsert_user(
        telegram_id=tg_id,
        username=tg_user.get("username"),
        first_name=tg_user.get("first_name"),
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


async def current_user_flexible(
    x_init_data: Annotated[str, Header(alias="X-Init-Data")] = "",
    init_data: Annotated[str, Query(alias="initData")] = "",
) -> dict[str, Any]:
    """
    Как current_user, но принимает initData ещё и из query-параметра.
    Нужно для обычных ссылок-скачиваний (например, .ics), где нельзя
    выставить HTTP-заголовок. Подпись всё равно проверяется.
    """
    return await _authenticate(x_init_data or init_data)


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


def serialize_training(row: aiosqlite.Row, *, viewer: Optional[dict] = None) -> dict[str, Any]:
    start = _local_time(row["start_at"])
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
        "level": row["level"] if "level" in row.keys() else "",
        "booked_count": row["booked_count"] if "booked_count" in row.keys() else 0,
        "reminder_enabled": bool(row["reminder_enabled"]),
        "reminder_offset_min": row["reminder_offset_min"],
        "deleted": bool(row["deleted_at"]) if "deleted_at" in row.keys() else False,
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
    if viewer is not None:
        # Информация о записи текущего игрока подставляется отдельно.
        data["_viewer_id"] = viewer["telegram_id"]
    return data


def serialize_participant(row: aiosqlite.Row) -> dict[str, Any]:
    """Участник для «социальной сетки» — имя и аватарка, без username."""
    return {
        "telegram_id": row["telegram_id"],
        "first_name": row["first_name"] or "Игрок",
        "status": row["status"],
        "avatar_url": f"/api/avatar/{row['telegram_id']}",
    }


def serialize_coach(row: Optional[aiosqlite.Row]) -> Optional[dict[str, Any]]:
    """Публичный профиль тренера для карточки тренировки (без username)."""
    if row is None:
        return None
    return {
        "telegram_id": row["telegram_id"],
        "first_name": row["first_name"] or "Тренер",
        "avatar_url": f"/api/avatar/{row['telegram_id']}",
        "bio": row["bio"] or "",
    }


def sanitize_bio(value: str) -> str:
    """Обрезает описание профиля тренера."""
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


def build_ics(training: aiosqlite.Row) -> str:
    """Формирует iCalendar-файл (RFC 5545) для добавления в календарь."""
    start = datetime.fromtimestamp(training["start_at"], tz=timezone.utc)
    end = start + timedelta(minutes=training["duration_min"])
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fmt = lambda d: d.strftime("%Y%m%dT%H%M%SZ")  # noqa: E731

    summary = _escape_ics(training["training_type"] or "Сквош-тренировка")
    description = _escape_ics(
        f"Сквош-тренировка · {training['duration_min']} мин · записано "
        f"{training['booked_count'] if 'booked_count' in training.keys() else 0} из {training['capacity']}"
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
    return "\r\n".join(lines) + "\r\n"


# --------------------------------------------------------------------------- #
# Модели запросов
# --------------------------------------------------------------------------- #

class TrainingIn(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    time: str = Field(pattern=r"^\d{2}:\d{2}$")
    training_type: str = Field(default="Тренировка", max_length=100)
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


class CoachIn(BaseModel):
    telegram_id: Optional[int] = None
    username: Optional[str] = Field(default=None, max_length=64)


class ManagerIn(BaseModel):
    enabled: bool


class ApplyIn(BaseModel):
    message: str = Field(default="", max_length=1000)


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    training_id: Optional[int] = None


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
    # 1. Готовим директорию данных (изолированный volume).
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

    if settings.bot_mode == "webhook" and settings.webhook_base_url:
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


app = FastAPI(title="Squash Training Web App", lifespan=lifespan)


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
            "avatar_url": f"/api/avatar/{user['telegram_id']}",
        },
    }


@app.put("/api/me/notifications")
async def api_set_notifications(
    payload: NotificationsIn,
    user: Annotated[dict, Depends(current_user)],
):
    await db.set_notifications_enabled(user["telegram_id"], payload.enabled)
    return {"ok": True, "notifications_enabled": payload.enabled}


# --------------------------------------------------------------------------- #
# API: тренировки (игрок)
# --------------------------------------------------------------------------- #

@app.get("/api/trainings")
async def api_trainings(user: Annotated[dict, Depends(current_user)], days: int = 7):
    days = max(1, min(days, 31))
    now = int(time.time())
    until = int((datetime.now(tz=settings.timezone) + timedelta(days=days)).timestamp())
    rows = await db.list_upcoming_trainings(now, until)

    # У каждого занятия свой тренер — берём по создателю (created_by).
    coach_cache: dict[Any, Optional[dict[str, Any]]] = {}
    result = []
    for row in rows:
        creator = row["created_by"]
        if creator not in coach_cache:
            coach_cache[creator] = serialize_coach(
                await db.get_user(creator) if creator is not None else None
            )
        item = serialize_training(row, viewer=user)
        item["coach"] = coach_cache[creator]
        participants = await db.get_participants(row["id"])
        item["participants"] = [serialize_participant(p) for p in participants if p["status"] == "booked"]
        item["waitlist_count"] = sum(1 for p in participants if p["status"] == "waitlist")
        # Статус текущего пользователя.
        my = await db.get_booking(row["id"], user["telegram_id"])
        if my is not None and my["status"] == "booked":
            item["is_booked"] = True
        elif my is not None and my["status"] == "waitlist":
            item["is_waitlisted"] = True
        item.pop("_viewer_id", None)
        result.append(item)
    return {"ok": True, "trainings": result}


@app.post("/api/trainings/{training_id}/book")
async def api_book(training_id: int, user: Annotated[dict, Depends(current_user)]):
    try:
        training = await db.get_training(training_id)
        if training is None:
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
    try:
        training = await db.get_training(training_id)
        if training is None:
            return error_json("Тренировка не найдена", 404)

        seconds_left = training["start_at"] - int(time.time())
        # Правило: игрок не может выйти из тренировки, если до старта меньше 4.5 ч.
        if seconds_left <= 0:
            return error_json("Тренировка уже началась — отмена недоступна.", 403)
        if seconds_left < BOOKING_CANCEL_LOCK_MINUTES * 60:
            return error_json(
                "Отмена недоступна: до старта меньше 4.5 часов. Свяжитесь с тренером.", 403
            )

        promoted = await db.cancel_booking(training_id, user["telegram_id"])
        return {"ok": True, "promoted": promoted or None}
    except Exception:
        logger.exception("Ошибка отмены записи")
        return error_json("Внутренняя ошибка сервера", 500)


@app.get("/calendar/{training_id}.ics")
async def api_calendar(
    training_id: int,
    user: Annotated[dict, Depends(current_user_flexible)],
):
    """Отдаёт .ics для добавления тренировки в календарь смартфона."""
    training = await db.get_training(training_id)
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
    return {"ok": True, "trainings": [serialize_training(r) for r in rows]}


@app.get("/api/coach/history")
async def api_coach_history(coach: Annotated[dict, Depends(require_coach)]):
    """История: прошедшие занятия текущего тренера."""
    rows = await db.list_history_trainings(int(time.time()), coach["telegram_id"])
    return {"ok": True, "trainings": [serialize_training(r) for r in rows]}


@app.get("/api/coach/deleted")
async def api_coach_deleted(coach: Annotated[dict, Depends(require_coach)]):
    """Корзина текущего тренера."""
    rows = await db.list_deleted_trainings(coach["telegram_id"])
    return {"ok": True, "trainings": [serialize_training(r) for r in rows]}


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
        if training is None or training["created_by"] != coach["telegram_id"]:
            return error_json("Тренировка не найдена", 404)
        await db.soft_delete_training(training_id, coach["telegram_id"])
        return {"ok": True, "restorable": True}
    except Exception:
        logger.exception("Ошибка удаления тренировки")
        return error_json("Внутренняя ошибка сервера", 500)


@app.post("/api/coach/trainings/{training_id}/restore")
async def api_restore_training(training_id: int, coach: Annotated[dict, Depends(require_coach)]):
    """Восстановление тренировки из корзины."""
    try:
        training = await db.get_training(training_id)
        if training is None or training["created_by"] != coach["telegram_id"]:
            return error_json("Тренировка не найдена", 404)
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
                    "username": r["username"],
                    "status": r["status"],
                    "avatar_url": f"/api/avatar/{r['telegram_id']}",
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
        if training is None or training["created_by"] != coach["telegram_id"]:
            return error_json("Тренировка не найдена", 404)
        if await db.get_booking(training_id, user_id) is None:
            return error_json("Записи не существует", 404)
        promoted = await db.cancel_booking(training_id, user_id)
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
        if training is None or training["created_by"] != coach["telegram_id"]:
            return error_json("Тренировка не найдена", 404)
        if await db.get_user(user_id) is None:
            return error_json("Игрок не найден", 404)
        status = await db.book_training(training_id, user_id, training["capacity"])
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
        if training is None or training["created_by"] != coach["telegram_id"]:
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
                "username": r["username"],
                "avatar_url": f"/api/avatar/{r['telegram_id']}",
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
        await db.set_trainer_profile(coach["telegram_id"], sanitize_bio(payload.bio), "[]")
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
                "username": r["username"],
                "avatar_url": f"/api/avatar/{r['telegram_id']}",
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
                "username": r["username"],
                "avatar_url": f"/api/avatar/{r['telegram_id']}",
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
    if await db.get_application(telegram_id) is None:
        return error_json("Заявка не найдена", 404)
    await db.add_coach(telegram_id, manager["telegram_id"])
    await db.decide_application(telegram_id, "approved", manager["telegram_id"])
    asyncio.create_task(_notify_new_coach(telegram_id))
    return {"ok": True}


@app.post("/api/coach/applications/{telegram_id}/reject")
async def api_reject_application(
    telegram_id: int, manager: Annotated[dict, Depends(require_manager)]
):
    if await db.get_application(telegram_id) is None:
        return error_json("Заявка не найдена", 404)
    await db.decide_application(telegram_id, "rejected", manager["telegram_id"])
    return {"ok": True}


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
    cache: dict[Any, Optional[dict[str, Any]]] = {}
    result = []
    for row in rows:
        creator = row["created_by"]
        if creator not in cache:
            cache[creator] = serialize_coach(await db.get_user(creator) if creator else None)
        item = serialize_training(row)
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
        if training is None or (
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
        return {"ok": True, "telegram_id": target}
    except Exception:
        logger.exception("Ошибка передачи занятия")
        return error_json("Внутренняя ошибка сервера", 500)


# --------------------------------------------------------------------------- #
# Прокси аватарок (без сохранения файлов на диск)
# --------------------------------------------------------------------------- #

@app.get("/api/avatar/{telegram_id}")
async def api_avatar(telegram_id: int, user: Annotated[dict, Depends(current_user)]):
    """Отдаёт аватарку из Telegram через бота, не раскрывая токен и не сохраняя файл."""
    cached = _avatar_cache.get(telegram_id)
    if cached and time.time() - cached[0] < _AVATAR_TTL_SECONDS:
        return StreamingResponse(iter([cached[1]]), media_type=cached[2])

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
    _avatar_cache[telegram_id] = (time.time(), content, media_type)
    return StreamingResponse(iter([content]), media_type=media_type)


# --------------------------------------------------------------------------- #
# Вебхук Telegram (используется только при BOT_MODE=webhook)
# --------------------------------------------------------------------------- #

@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Annotated[str, Header()] = "",
):
    # Проверяем секрет вебхука, чтобы принимали апдейты только от Telegram.
    if settings.webhook_secret and x_telegram_bot_api_secret_token != settings.webhook_secret:
        return error_json("Forbidden", 403)
    try:
        payload = await request.json()
        update = Update.model_validate(payload)
        await app.state.dp.feed_webhook_update(app.state.bot, update)
        return {"ok": True}
    except Exception:
        logger.exception("Ошибка обработки вебхука")
        return {"ok": False}
