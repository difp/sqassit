"""
Общая настройка тестов.

ВНИМАНИЕ: переменные окружения выставляются ДО импорта app (config читает их
на импорте). Тестовый бот использует фиктивный токен.
"""

import os

os.environ.setdefault("BOT_TOKEN", "123456:TEST-ONLY-TOKEN")
os.environ.setdefault("COACH_TELEGRAM_ID", "123")
os.environ.setdefault("BOT_MODE", "polling")
os.environ.setdefault("TIMEZONE", "Europe/Moscow")
os.environ.setdefault("DB_PATH", "/tmp/squashbot-test.db")
os.environ.setdefault("AVATAR_CACHE_DIR", "/tmp/sqassit-test-avatars")
os.environ.setdefault("ALLOW_DEV_INIT_DATA", "true")

# Чистим БД прошлого прогона.
for suffix in ("", "-wal", "-shm"):
    path = os.environ["DB_PATH"] + suffix
    if os.path.exists(path):
        os.remove(path)
