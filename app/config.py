"""
Конфигурация приложения.

Все секреты считываются ТОЛЬКО из переменных окружения (.env, который не
попадает в образ и в git). Никаких значений паролей/токенов в коде.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Загружаем .env при локальном запуске. В Docker переменные приходят из
# docker-compose (env_file) — load_dotenv просто не найдёт файл и не упадёт.
load_dotenv()


def _require(name: str) -> str:
    """Возвращает обязательную переменную окружения или падает с понятной ошибкой."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Не задана обязательная переменная окружения {name}. "
            f"Заполните .env (см. .env.example)."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    """Иммутабельный снимок настроек, собранный один раз при старте."""

    bot_token: str
    coach_telegram_id: int
    timezone: ZoneInfo
    db_path: Path
    bot_mode: str
    webhook_base_url: str
    webhook_secret: str
    allow_dev_init_data: bool


def load_settings() -> Settings:
    """Собирает настройки из окружения с валидацией типов."""
    coach_raw = _require("COACH_TELEGRAM_ID")
    try:
        coach_id = int(coach_raw)
    except ValueError as exc:  # защита от опечатки в .env
        raise RuntimeError("COACH_TELEGRAM_ID должен быть числом (telegram_id).") from exc

    tz_name = _optional("TIMEZONE", "Europe/Moscow") or "Europe/Moscow"
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:  # неизвестная таймзона
        raise RuntimeError(f"Неизвестный TIMEZONE: {tz_name}") from exc

    bot_mode = (_optional("BOT_MODE", "polling") or "polling").lower()
    if bot_mode not in {"polling", "webhook"}:
        raise RuntimeError("BOT_MODE должен быть 'polling' или 'webhook'.")

    return Settings(
        bot_token=_require("BOT_TOKEN"),
        coach_telegram_id=coach_id,
        timezone=tz,
        db_path=Path(_optional("DB_PATH", "/app/data/squash.db") or "/app/data/squash.db"),
        bot_mode=bot_mode,
        webhook_base_url=_optional("WEBHOOK_BASE_URL"),
        webhook_secret=_optional("WEBHOOK_SECRET"),
        allow_dev_init_data=_optional("ALLOW_DEV_INIT_DATA", "false").lower()
        in ("1", "true", "yes", "on"),
    )


# Бизнес-константы (не секреты, вынесены явно).
BOOKING_CANCEL_LOCK_MINUTES = 270  # 4.5 часа — окно, когда игрок сам отменить не может
DEFAULT_REMINDER_OFFSET_MINUTES = 270  # 4.5 часа — автонапоминание по умолчанию
DEFAULT_TRAINING_DURATION_MINUTES = 60

# Допустимые уровни игры (тренер выбирает один; пустая строка = не указан).
TRAINING_LEVELS = (
    "Новичок (Into)",
    "Начинающий (Beginner)",
    "Уверенный любитель (С)",
)

settings = load_settings()
