"""
Конфигурация приложения.

Все секреты считываются ТОЛЬКО из переменных окружения (.env, который не
попадает в образ и в git). Никаких значений паролей/токенов в коде.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Загружаем .env при локальном запуске. В production переменные приходят из
# окружения/systemd EnvironmentFile — load_dotenv просто не найдёт файл и не упадёт.
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
    avatar_cache_dir: Path
    bot_mode: str
    webhook_base_url: str
    webhook_secret: str
    bot_proxy: str
    allow_dev_init_data: bool
    enable_api_docs: bool
    phone_required_from: datetime | None
    require_event_approval: bool


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

    phone_raw = _optional("PHONE_REQUIRED_FROM")
    phone_required_from: datetime | None = None
    if phone_raw:
        try:
            phone_required_from = datetime.fromisoformat(phone_raw)
        except ValueError as exc:
            raise RuntimeError(
                "PHONE_REQUIRED_FROM должен быть ISO-датой, например "
                "2026-10-24T00:00:00+03:00."
            ) from exc
        if phone_required_from.tzinfo is None:
            phone_required_from = phone_required_from.replace(tzinfo=tz)

    webhook_base_url = _optional("WEBHOOK_BASE_URL").rstrip("/")
    webhook_secret = _optional("WEBHOOK_SECRET")
    validate_webhook_settings(bot_mode, webhook_base_url, webhook_secret)

    return Settings(
        bot_token=_require("BOT_TOKEN"),
        coach_telegram_id=coach_id,
        timezone=tz,
        db_path=Path(_optional("DB_PATH", "./data/squash.db") or "./data/squash.db"),
        avatar_cache_dir=Path(
            _optional("AVATAR_CACHE_DIR", "./data/cache/avatars")
            or "./data/cache/avatars"
        ),
        bot_mode=bot_mode,
        webhook_base_url=webhook_base_url,
        webhook_secret=webhook_secret,
        bot_proxy=_optional("BOT_PROXY").strip(),
        allow_dev_init_data=_optional("ALLOW_DEV_INIT_DATA", "false").lower()
        in ("1", "true", "yes", "on"),
        enable_api_docs=_optional("ENABLE_API_DOCS", "false").lower()
        in ("1", "true", "yes", "on"),
        phone_required_from=phone_required_from,
        require_event_approval=_optional("REQUIRE_EVENT_APPROVAL", "false").lower()
        in ("1", "true", "yes", "on"),
    )


# Значения, которые нельзя использовать в качестве WEBHOOK_SECRET.
# Попадают в репозиторий/документацию и потому считаются публичными.
INSECURE_WEBHOOK_SECRETS = frozenset(
    {
        "change-me-to-random-string",
        "changeme",
        "change-me",
        "secret",
        "webhook-secret",
        "test",
    }
)


def validate_webhook_settings(bot_mode: str, base_url: str, secret: str) -> None:
    """
    Проверяет конфигурацию webhook до запуска приложения.

    В webhook-режиме публичный эндпоинт /telegram/webhook — единственный вход
    для апдейтов Telegram, поэтому без сильного секрета приложение не должно
    стартовать. В polling-режиме webhook-маршрут отключён на уровне API.
    """
    if bot_mode != "webhook":
        return
    if not base_url:
        raise RuntimeError(
            "BOT_MODE=webhook требует WEBHOOK_BASE_URL (публичный HTTPS-адрес)."
        )
    if not secret:
        raise RuntimeError(
            "BOT_MODE=webhook требует WEBHOOK_SECRET. "
            "Сгенерируйте: openssl rand -hex 32."
        )
    normalized = secret.strip().lower()
    if normalized in INSECURE_WEBHOOK_SECRETS or len(secret.strip()) < 32:
        raise RuntimeError(
            "WEBHOOK_SECRET слишком слабый: нужен случайный секрет не короче 32 "
            "символов (например, openssl rand -hex 32)."
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
