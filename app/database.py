"""
Слой работы с SQLite (aiosqlite).

Правила безопасности:
  * Любой SQL-запрос — строго параметризованный (плейсхолдеры `?`).
    Склейка пользовательских данных в SQL запрещена.
  * Единственное общее соединение защищено асинхронным локом (SQLite не любит
    параллельную запись).
  * WAL-режим для надёжности и производительности.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import aiosqlite

# Одно общее соединение на процесс + лок для сериализации доступа.
_conn: aiosqlite.Connection | None = None
_lock = asyncio.Lock()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id           INTEGER PRIMARY KEY,
    username              TEXT,
    first_name            TEXT,
    avatar_file_id        TEXT,      -- file_id аватарки; сам файл НЕ сохраняем на диск
    is_coach              INTEGER NOT NULL DEFAULT 0,
    notifications_enabled INTEGER NOT NULL DEFAULT 1,
    notify_matches        INTEGER NOT NULL DEFAULT 1, -- лента матчей и события матчей
    notify_trainings      INTEGER NOT NULL DEFAULT 1, -- напоминания и изменения записи
    notify_messages       INTEGER NOT NULL DEFAULT 1, -- личные сообщения от пользователей
    notes                 TEXT NOT NULL DEFAULT '',
    bio                   TEXT NOT NULL DEFAULT '',   -- кратко о тренере
    links                 TEXT NOT NULL DEFAULT '[]', -- ссылки тренера (JSON-массив)
    about                 TEXT NOT NULL DEFAULT '',   -- «о себе» игрока (видит тренер)
    display_mode          TEXT NOT NULL DEFAULT 'name', -- name | animal
    show_username         INTEGER NOT NULL DEFAULT 0, -- согласие показывать @username
    onboarding_seen       INTEGER NOT NULL DEFAULT 0, -- памятка при первом запуске
    created_at            INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trainings (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    title                 TEXT NOT NULL,
    training_type         TEXT NOT NULL DEFAULT 'Тренировка',
    start_at              INTEGER NOT NULL,           -- Unix-время (UTC)
    duration_min          INTEGER NOT NULL DEFAULT 60,
    capacity              INTEGER NOT NULL DEFAULT 4,
    level                 TEXT NOT NULL DEFAULT '',   -- уровень игры
    reminder_enabled      INTEGER NOT NULL DEFAULT 1,
    reminder_offset_min   INTEGER NOT NULL DEFAULT 270,
    reminder_sent         INTEGER NOT NULL DEFAULT 0,
    deleted_at            INTEGER,                    -- мягкое удаление (корзина)
    deleted_by            INTEGER,
    created_by            INTEGER,
    created_at            INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    training_id  INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'booked',   -- booked | waitlist | cancelled
    position     INTEGER NOT NULL DEFAULT 0,       -- порядок в очереди ожидания (FIFO)
    created_at   INTEGER NOT NULL,
    UNIQUE (training_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_trainings_start ON trainings(start_at);
CREATE INDEX IF NOT EXISTS idx_bookings_training ON bookings(training_id, status, position);

CREATE TABLE IF NOT EXISTS coaches (
    telegram_id INTEGER PRIMARY KEY,
    added_by    INTEGER,                 -- кто выдал доступ (telegram_id), NULL для владельца
    is_manager  INTEGER NOT NULL DEFAULT 0,  -- право подтверждать заявки и вести реестр
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS coach_applications (
    telegram_id INTEGER PRIMARY KEY,
    message     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    created_at  INTEGER NOT NULL,
    decided_by  INTEGER,
    decided_at  INTEGER
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user   INTEGER NOT NULL,
    to_user     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,             -- bug | idea
    text        TEXT NOT NULL,
    page        TEXT NOT NULL DEFAULT '',  -- откуда отправлено
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL DEFAULT 'Матч',
    description   TEXT NOT NULL DEFAULT '',
    location      TEXT NOT NULL DEFAULT '',
    start_at      INTEGER NOT NULL,
    duration_min  INTEGER NOT NULL DEFAULT 60,
    max_players   INTEGER NOT NULL DEFAULT 4,
    level         TEXT NOT NULL DEFAULT '',
    created_by    INTEGER NOT NULL,
    created_at    INTEGER NOT NULL,
    deleted_at    INTEGER
);

CREATE TABLE IF NOT EXISTS match_players (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    UNIQUE (match_id, user_id)
);

CREATE TABLE IF NOT EXISTS event_notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,             -- training | match
    entity_id   INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL DEFAULT 0, -- 0 = сводка организатору
    message_id  INTEGER NOT NULL DEFAULT 0,
    text        TEXT NOT NULL DEFAULT '',
    updated_at  INTEGER NOT NULL,
    UNIQUE (kind, entity_id, chat_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_matches_start ON matches(start_at);
CREATE INDEX IF NOT EXISTS idx_match_players_match ON match_players(match_id);

CREATE INDEX IF NOT EXISTS idx_messages_to ON messages(to_user, created_at);
"""


async def connect(db_path: str) -> None:
    """Открывает соединение и создаёт схему, если её ещё нет."""
    global _conn, _lock
    # Lock привязан к event loop: создаём его заново вместе с соединением,
    # иначе повторный запуск приложения/тестов в другом loop падает.
    _lock = asyncio.Lock()
    _conn = await aiosqlite.connect(db_path)
    _conn.row_factory = aiosqlite.Row
    await _conn.execute("PRAGMA journal_mode=WAL;")
    await _conn.execute("PRAGMA foreign_keys=ON;")
    await _conn.execute("PRAGMA busy_timeout=5000;")
    await _conn.executescript(SCHEMA)
    await _migrate()
    await _conn.commit()


async def _migrate() -> None:
    """
    Идемпотентные миграции существующих БД (правило бесшовного обновления:
    старый squash.db продолжает работать без ручных действий).
    """
    assert _conn is not None

    async def columns_of(table: str) -> set[str]:
        async with _conn.execute(f"PRAGMA table_info({table})") as cur:
            return {row[1] for row in await cur.fetchall()}

    trainings_cols = await columns_of("trainings")
    users_cols = await columns_of("users")
    coaches_cols = await columns_of("coaches")
    onboarding_was_present = "onboarding_seen" in users_cols

    additions = {
        ("trainings", "deleted_at"): "ALTER TABLE trainings ADD COLUMN deleted_at INTEGER",
        ("trainings", "deleted_by"): "ALTER TABLE trainings ADD COLUMN deleted_by INTEGER",
        ("trainings", "level"): "ALTER TABLE trainings ADD COLUMN level TEXT NOT NULL DEFAULT ''",
        ("users", "bio"): "ALTER TABLE users ADD COLUMN bio TEXT NOT NULL DEFAULT ''",
        ("users", "links"): "ALTER TABLE users ADD COLUMN links TEXT NOT NULL DEFAULT '[]'",
        ("users", "about"): "ALTER TABLE users ADD COLUMN about TEXT NOT NULL DEFAULT ''",
        ("users", "display_mode"): (
            "ALTER TABLE users ADD COLUMN display_mode TEXT NOT NULL DEFAULT 'name'"
        ),
        ("users", "show_username"): (
            "ALTER TABLE users ADD COLUMN show_username INTEGER NOT NULL DEFAULT 0"
        ),
        ("users", "onboarding_seen"): (
            "ALTER TABLE users ADD COLUMN onboarding_seen INTEGER NOT NULL DEFAULT 0"
        ),
        ("users", "notify_matches"): (
            "ALTER TABLE users ADD COLUMN notify_matches INTEGER NOT NULL DEFAULT 1"
        ),
        ("users", "notify_trainings"): (
            "ALTER TABLE users ADD COLUMN notify_trainings INTEGER NOT NULL DEFAULT 1"
        ),
        ("users", "notify_messages"): (
            "ALTER TABLE users ADD COLUMN notify_messages INTEGER NOT NULL DEFAULT 1"
        ),
        ("coaches", "is_manager"): "ALTER TABLE coaches ADD COLUMN is_manager INTEGER NOT NULL DEFAULT 0",
    }
    present = {"trainings": trainings_cols, "users": users_cols, "coaches": coaches_cols}
    for (table, column), ddl in additions.items():
        if column not in present[table]:
            await _conn.execute(ddl)

    # Памятку показываем только новым пользователям. При обновлении старой БД
    # существующие аккаунты не должны внезапно получить онбординг.
    if not onboarding_was_present:
        await _conn.execute("UPDATE users SET onboarding_seen = 1")


async def close() -> None:
    """Корректно закрывает соединение при остановке приложения."""
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def _db() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("База данных не инициализирована (вызовите connect()).")
    return _conn


async def ping() -> bool:
    """Проверка живости БД для readiness-эндпоинта."""
    row = await _fetchone("SELECT 1")
    return row is not None


async def _fetchall(sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
    db = _db()
    async with _lock:
        cursor = await db.execute(sql, params)
        try:
            return list(await cursor.fetchall())
        finally:
            await cursor.close()


async def _fetchone(sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
    db = _db()
    async with _lock:
        cursor = await db.execute(sql, params)
        try:
            return await cursor.fetchone()
        finally:
            await cursor.close()


async def _execute(sql: str, params: tuple[Any, ...] = ()) -> int:
    """Выполняет INSERT/UPDATE/DELETE, возвращает lastrowid."""
    db = _db()
    async with _lock:
        cursor = await db.execute(sql, params)
        try:
            await db.commit()
            return int(cursor.lastrowid or 0)
        finally:
            await cursor.close()


async def _execute_rowcount(sql: str, params: tuple[Any, ...] = ()) -> int:
    """Выполняет INSERT/UPDATE/DELETE и возвращает число затронутых строк."""
    db = _db()
    async with _lock:
        cursor = await db.execute(sql, params)
        try:
            await db.commit()
            return int(cursor.rowcount or 0)
        finally:
            await cursor.close()


# --------------------------------------------------------------------------- #
# Пользователи (CRM)
# --------------------------------------------------------------------------- #

async def upsert_user(
    telegram_id: int,
    username: str | None,
    first_name: str | None,
    *,
    avatar_file_id: str | None = None,
    is_coach: bool = False,
) -> None:
    """Создаёт/обновляет карточку игрока (CRM-автозаполнение)."""
    await _execute(
        """
        INSERT INTO users (telegram_id, username, first_name, avatar_file_id,
                           is_coach, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            username       = excluded.username,
            first_name     = excluded.first_name,
            avatar_file_id = COALESCE(excluded.avatar_file_id, users.avatar_file_id),
            is_coach       = excluded.is_coach
        """,
        (telegram_id, username, first_name, avatar_file_id, int(is_coach), int(time.time())),
    )


async def get_user(telegram_id: int) -> aiosqlite.Row | None:
    return await _fetchone("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))


async def set_user_avatar(telegram_id: int, file_id: str) -> None:
    """Кэширует file_id аватарки (без скачивания файла)."""
    await _execute(
        "UPDATE users SET avatar_file_id = ? WHERE telegram_id = ?",
        (file_id, telegram_id),
    )


async def set_user_notes(telegram_id: int, notes: str) -> None:
    """Обновляет скрытые заметки тренера о игроке."""
    await _execute("UPDATE users SET notes = ? WHERE telegram_id = ?", (notes, telegram_id))


async def set_trainer_profile(telegram_id: int, bio: str) -> None:
    """Сохраняет публичный профиль тренера: краткое описание."""
    await _execute(
        "UPDATE users SET bio = ? WHERE telegram_id = ?", (bio, telegram_id)
    )


async def set_player_profile(
    telegram_id: int,
    *,
    about: str,
    display_mode: str,
    show_username: bool,
    notifications_enabled: bool | None = None,
    notify_matches: bool | None = None,
    notify_trainings: bool | None = None,
    notify_messages: bool | None = None,
) -> None:
    """Сохраняет профиль, приватность и настройки уведомлений игрока."""
    def flag(value: bool | None) -> int | None:
        return None if value is None else int(value)

    await _execute(
        """
        UPDATE users
           SET about = ?,
               display_mode = ?,
               show_username = ?,
               notifications_enabled = COALESCE(?, notifications_enabled),
               notify_matches = COALESCE(?, notify_matches),
               notify_trainings = COALESCE(?, notify_trainings),
               notify_messages = COALESCE(?, notify_messages)
         WHERE telegram_id = ?
        """,
        (
            about,
            display_mode,
            int(show_username),
            flag(notifications_enabled),
            flag(notify_matches),
            flag(notify_trainings),
            flag(notify_messages),
            telegram_id,
        ),
    )


async def set_onboarding_seen(telegram_id: int) -> None:
    """Отмечает, что пользователь прошёл памятку первого запуска."""
    await _execute(
        "UPDATE users SET onboarding_seen = 1 WHERE telegram_id = ?",
        (telegram_id,),
    )


async def find_user_by_username(username: str) -> aiosqlite.Row | None:
    """Ищет пользователя по @username (без учёта регистра)."""
    return await _fetchone(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username.lstrip("@"),)
    )


# --------------------------------------------------------------------------- #
# Тренеры (роли)
# --------------------------------------------------------------------------- #

async def ensure_coach(telegram_id: int, added_by: int | None = None) -> None:
    """Гарантирует наличие тренера в реестре (идемпотентно)."""
    await _execute(
        "INSERT OR IGNORE INTO coaches (telegram_id, added_by, created_at) VALUES (?, ?, ?)",
        (telegram_id, added_by, int(time.time())),
    )


async def add_coach(telegram_id: int, added_by: int) -> bool:
    """Добавляет тренера. False, если он уже есть (идемпотентно)."""
    inserted = await _execute_rowcount(
        "INSERT OR IGNORE INTO coaches (telegram_id, added_by, created_at) VALUES (?, ?, ?)",
        (telegram_id, added_by, int(time.time())),
    )
    if inserted:
        # Держим денормализованный флаг в users в актуальном состоянии:
        # по нему строится CRM и некоторые проверки отображения.
        await _execute(
            "UPDATE users SET is_coach = 1 WHERE telegram_id = ?", (telegram_id,)
        )
    return bool(inserted)


async def remove_coach(telegram_id: int) -> None:
    """Снимает роль тренера и синхронизирует users.is_coach."""
    await _execute("DELETE FROM coaches WHERE telegram_id = ?", (telegram_id,))
    await _execute(
        "UPDATE users SET is_coach = 0 WHERE telegram_id = ?", (telegram_id,)
    )


async def is_coach_id(telegram_id: int) -> bool:
    """Является ли пользователь тренером."""
    return await _fetchone("SELECT 1 FROM coaches WHERE telegram_id = ?", (telegram_id,)) is not None


async def is_manager_id(telegram_id: int) -> bool:
    """Может ли тренер подтверждать заявки и вести реестр."""
    row = await _fetchone(
        "SELECT is_manager FROM coaches WHERE telegram_id = ?", (telegram_id,)
    )
    return bool(row["is_manager"]) if row is not None else False


async def set_manager(telegram_id: int, enabled: bool) -> None:
    """Выдаёт/снимает права менеджера (делает только владелец)."""
    await _execute(
        "UPDATE coaches SET is_manager = ? WHERE telegram_id = ?",
        (int(enabled), telegram_id),
    )


async def list_coaches() -> list[aiosqlite.Row]:
    """Реестр тренеров с профилями (кто добавил, когда, права менеджера)."""
    return await _fetchall(
        """
        SELECT c.telegram_id, c.added_by, c.created_at, c.is_manager,
               u.first_name, u.username, u.show_username, u.avatar_file_id
          FROM coaches c
          LEFT JOIN users u ON u.telegram_id = c.telegram_id
         ORDER BY c.created_at ASC
        """
    )


# --------------------------------------------------------------------------- #
# Заявки в тренеры
# --------------------------------------------------------------------------- #

async def apply_for_coach(telegram_id: int, message: str) -> None:
    """Создаёт/обновляет заявку (снова pending, если ранее была отклонена)."""
    await _execute(
        """
        INSERT INTO coach_applications (telegram_id, message, status, created_at)
        VALUES (?, ?, 'pending', ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            message = excluded.message,
            status = 'pending',
            created_at = excluded.created_at,
            decided_by = NULL,
            decided_at = NULL
        """,
        (telegram_id, message, int(time.time())),
    )


async def get_application(telegram_id: int) -> aiosqlite.Row | None:
    return await _fetchone(
        "SELECT * FROM coach_applications WHERE telegram_id = ?", (telegram_id,)
    )


async def list_applications(status: str = "pending") -> list[aiosqlite.Row]:
    """Заявки с профилями заявителей."""
    return await _fetchall(
        """
        SELECT a.telegram_id, a.message, a.status, a.created_at,
               u.first_name, u.username, u.show_username, u.avatar_file_id,
               u.display_mode
          FROM coach_applications a
          LEFT JOIN users u ON u.telegram_id = a.telegram_id
         WHERE a.status = ?
         ORDER BY a.created_at ASC
        """,
        (status,),
    )


async def decide_application(telegram_id: int, status: str, decided_by: int) -> None:
    await _execute(
        "UPDATE coach_applications SET status = ?, decided_by = ?, decided_at = ? "
        "WHERE telegram_id = ?",
        (status, decided_by, int(time.time()), telegram_id),
    )


async def list_managers() -> list[aiosqlite.Row]:
    """Все, кто может рассматривать заявки: владелец добавляется отдельно."""
    return await _fetchall("SELECT telegram_id FROM coaches WHERE is_manager = 1")


async def set_notifications_enabled(telegram_id: int, enabled: bool) -> None:
    """Общий тумблер уведомлений игрока."""
    await _execute(
        "UPDATE users SET notifications_enabled = ? WHERE telegram_id = ?",
        (int(enabled), telegram_id),
    )


async def set_notification_settings(
    telegram_id: int,
    *,
    enabled: bool | None = None,
    matches: bool | None = None,
    trainings: bool | None = None,
    messages: bool | None = None,
) -> None:
    """Обновляет только выбранные переключатели уведомлений."""
    def flag(value: bool | None) -> int | None:
        return None if value is None else int(value)

    await _execute(
        """
        UPDATE users
           SET notifications_enabled = COALESCE(?, notifications_enabled),
               notify_matches = COALESCE(?, notify_matches),
               notify_trainings = COALESCE(?, notify_trainings),
               notify_messages = COALESCE(?, notify_messages)
         WHERE telegram_id = ?
        """,
        (
            flag(enabled),
            flag(matches),
            flag(trainings),
            flag(messages),
            telegram_id,
        ),
    )


async def notification_enabled(telegram_id: int, category: str) -> bool:
    """Проверяет общий и тематический переключатели уведомлений."""
    columns = {
        "matches": "notify_matches",
        "trainings": "notify_trainings",
        "messages": "notify_messages",
    }
    if category not in columns:
        raise ValueError(f"Неизвестная категория уведомлений: {category}")
    row = await _fetchone(
        """
        SELECT notifications_enabled,
               CASE ?
                   WHEN 'matches' THEN notify_matches
                   WHEN 'trainings' THEN notify_trainings
                   WHEN 'messages' THEN notify_messages
               END AS category_enabled
          FROM users
         WHERE telegram_id = ?
        """,
        (category, telegram_id),
    )
    return bool(
        row is not None
        and row["notifications_enabled"]
        and row["category_enabled"]
    )


async def list_user_bookings(telegram_id: int) -> list[aiosqlite.Row]:
    """Записи пользователя на тренировки (для экспорта персональных данных)."""
    return await _fetchall(
        """
        SELECT b.training_id, b.status, b.position, b.created_at,
               t.training_type, t.start_at, t.deleted_at
          FROM bookings b
          JOIN trainings t ON t.id = b.training_id
         WHERE b.user_id = ?
         ORDER BY t.start_at DESC
        """,
        (telegram_id,),
    )


async def list_user_messages(telegram_id: int) -> list[aiosqlite.Row]:
    """Отправленные пользователем сообщения (для экспорта)."""
    return await _fetchall(
        """
        SELECT id, to_user, text, created_at FROM messages
         WHERE from_user = ? ORDER BY created_at ASC
        """,
        (telegram_id,),
    )


async def add_feedback(user_id: int, kind: str, text: str, page: str) -> int:
    """Сохраняет сообщение о проблеме или предложение."""
    return await _execute(
        "INSERT INTO feedback (user_id, kind, text, page, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, kind, text, page, int(time.time())),
    )


async def list_user_feedback(telegram_id: int) -> list[aiosqlite.Row]:
    """Обращения пользователя (для экспорта персональных данных)."""
    return await _fetchall(
        """
        SELECT id, kind, text, page, created_at FROM feedback
         WHERE user_id = ? ORDER BY created_at ASC
        """,
        (telegram_id,),
    )


async def delete_user_data(telegram_id: int) -> None:
    """
    Удаляет профиль и связанные персональные данные.

    Записи на тренировки должны быть предварительно отменены вызывающим кодом:
    так сохраняются корректные счётчики и продвигается лист ожидания.
    """
    await _execute(
        "DELETE FROM messages WHERE from_user = ? OR to_user = ?",
        (telegram_id, telegram_id),
    )
    await _execute(
        "DELETE FROM coach_applications WHERE telegram_id = ?", (telegram_id,)
    )
    await _execute("DELETE FROM feedback WHERE user_id = ?", (telegram_id,))
    # Активные записи уже отменены вызывающим кодом; удаляем и историю, чтобы
    # в БД не оставался telegram_id удалённого пользователя.
    await _execute("DELETE FROM bookings WHERE user_id = ?", (telegram_id,))
    await _execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))


async def list_all_users() -> list[aiosqlite.Row]:
    """Все пользователи, которым можно доставить закреплённые правила."""
    return await _fetchall("SELECT telegram_id FROM users ORDER BY telegram_id")


async def list_players() -> list[aiosqlite.Row]:
    """
    Список игроков для CRM тренера.

    Источник истины — реестр coaches, а не денормализованный users.is_coach:
    после снятия роли игрок не должен навсегда исчезать из CRM.
    """
    return await _fetchall(
        """
        SELECT u.* FROM users u
         WHERE NOT EXISTS (
               SELECT 1 FROM coaches c WHERE c.telegram_id = u.telegram_id
         )
         ORDER BY u.first_name COLLATE NOCASE
        """
    )


# --------------------------------------------------------------------------- #
# Тренировки
# --------------------------------------------------------------------------- #

async def create_training(
    *,
    title: str,
    training_type: str,
    start_at: int,
    capacity: int,
    duration_min: int,
    reminder_offset_min: int,
    reminder_enabled: bool,
    created_by: int,
    level: str = "",
) -> int:
    return await _execute(
        """
        INSERT INTO trainings (title, training_type, start_at, duration_min, capacity, level,
                               reminder_offset_min, reminder_enabled, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title, training_type, start_at, duration_min, capacity, level,
            reminder_offset_min, int(reminder_enabled), created_by, int(time.time()),
        ),
    )


async def update_reminder_settings(
    training_id: int, *, enabled: bool, offset_min: int
) -> None:
    """Тумблер напоминания и смещение таймера."""
    await _execute(
        "UPDATE trainings SET reminder_enabled = ?, reminder_offset_min = ?, "
        "reminder_sent = 0 WHERE id = ?",
        (int(enabled), offset_min, training_id),
    )


async def get_training(training_id: int) -> aiosqlite.Row | None:
    return await _fetchone("SELECT * FROM trainings WHERE id = ?", (training_id,))


async def get_training_with_count(training_id: int) -> aiosqlite.Row | None:
    """Тренировка вместе с числом записавшихся (для .ics и карточек)."""
    return await _fetchone(
        """
        SELECT t.*,
               (SELECT COUNT(*) FROM bookings b
                 WHERE b.training_id = t.id AND b.status = 'booked') AS booked_count
          FROM trainings t
         WHERE t.id = ?
        """,
        (training_id,),
    )


async def list_upcoming_trainings(now: int, until: int) -> list[aiosqlite.Row]:
    """Тренировки в окне [now, until] вместе с числом записавшихся (без удалённых)."""
    return await _fetchall(
        """
        SELECT t.*,
               (SELECT COUNT(*) FROM bookings b
                 WHERE b.training_id = t.id AND b.status = 'booked') AS booked_count
          FROM trainings t
         WHERE t.start_at >= ? AND t.start_at <= ? AND t.deleted_at IS NULL
         ORDER BY t.start_at ASC
        """,
        (now, until),
    )


async def list_active_trainings(now: int, owner_id: int) -> list[aiosqlite.Row]:
    """Предстоящие тренировки конкретного тренера (без удалённых)."""
    return await _fetchall(
        """
        SELECT t.*,
               (SELECT COUNT(*) FROM bookings b
                 WHERE b.training_id = t.id AND b.status = 'booked') AS booked_count,
               (SELECT COUNT(*) FROM bookings b
                 WHERE b.training_id = t.id AND b.status = 'waitlist') AS waitlist_count
          FROM trainings t
         WHERE t.start_at >= ? AND t.deleted_at IS NULL AND t.created_by = ?
         ORDER BY t.start_at ASC
        """,
        (now, owner_id),
    )


async def list_history_trainings(
    now: int, owner_id: int, limit: int = 100
) -> list[aiosqlite.Row]:
    """История конкретного тренера: прошедшие тренировки (без удалённых)."""
    return await _fetchall(
        """
        SELECT t.*,
               (SELECT COUNT(*) FROM bookings b
                 WHERE b.training_id = t.id AND b.status = 'booked') AS booked_count,
               (SELECT COUNT(*) FROM bookings b
                 WHERE b.training_id = t.id AND b.status = 'waitlist') AS waitlist_count
          FROM trainings t
         WHERE t.start_at < ? AND t.deleted_at IS NULL AND t.created_by = ?
         ORDER BY t.start_at DESC
         LIMIT ?
        """,
        (now, owner_id, limit),
    )


async def list_deleted_trainings(
    owner_id: int, limit: int = 100
) -> list[aiosqlite.Row]:
    """Корзина конкретного тренера: удалённые (для восстановления)."""
    return await _fetchall(
        """
        SELECT t.*,
               (SELECT COUNT(*) FROM bookings b
                 WHERE b.training_id = t.id AND b.status = 'booked') AS booked_count,
               (SELECT COUNT(*) FROM bookings b
                 WHERE b.training_id = t.id AND b.status = 'waitlist') AS waitlist_count
          FROM trainings t
         WHERE t.deleted_at IS NOT NULL AND t.created_by = ?
         ORDER BY t.deleted_at DESC
         LIMIT ?
        """,
        (owner_id, limit),
    )


async def find_duplicate_training(
    start_at: int, training_type: str, owner_id: int
) -> aiosqlite.Row | None:
    """Ищет активную тренировку того же тренера с тем же началом и типом (защита от дублей)."""
    return await _fetchone(
        """
        SELECT * FROM trainings
         WHERE deleted_at IS NULL AND start_at = ? AND training_type = ? AND created_by = ?
         LIMIT 1
        """,
        (start_at, training_type, owner_id),
    )


async def list_other_active_trainings(now: int, coach_id: int) -> list[aiosqlite.Row]:
    """Предстоящие занятия ДРУГИХ тренеров (клубный просмотр, только чтение)."""
    return await _fetchall(
        """
        SELECT t.*,
               (SELECT COUNT(*) FROM bookings b
                 WHERE b.training_id = t.id AND b.status = 'booked') AS booked_count,
               (SELECT COUNT(*) FROM bookings b
                 WHERE b.training_id = t.id AND b.status = 'waitlist') AS waitlist_count
          FROM trainings t
         WHERE t.start_at >= ? AND t.deleted_at IS NULL AND t.created_by != ?
         ORDER BY t.start_at ASC
        """,
        (now, coach_id),
    )


async def set_training_owner(training_id: int, new_owner: int) -> None:
    """Передаёт занятие другому тренеру."""
    await _execute(
        "UPDATE trainings SET created_by = ? WHERE id = ?", (new_owner, training_id)
    )


async def soft_delete_training(training_id: int, deleted_by: int) -> None:
    """
    Мягкое удаление: строка и все записи СОХРАНЯЮТСЯ, помечается deleted_at.
    Это позволяет восстановить тренировку (сценарий «случайно удалил»).
    """
    await _execute(
        "UPDATE trainings SET deleted_at = ?, deleted_by = ? WHERE id = ?",
        (int(time.time()), deleted_by, training_id),
    )


async def restore_training(training_id: int) -> None:
    """Восстанавливает тренировку из корзины."""
    await _execute(
        "UPDATE trainings SET deleted_at = NULL, deleted_by = NULL WHERE id = ?",
        (training_id,),
    )


async def purge_training(training_id: int) -> None:
    """Полное удаление (только из корзины, необратимо) одной транзакцией."""
    db = _db()
    async with _lock:
        await db.execute("DELETE FROM bookings WHERE training_id = ?", (training_id,))
        await db.execute("DELETE FROM trainings WHERE id = ?", (training_id,))
        await db.commit()


# --------------------------------------------------------------------------- #
# Записи и лист ожидания
# --------------------------------------------------------------------------- #

async def get_booking(training_id: int, user_id: int) -> aiosqlite.Row | None:
    return await _fetchone(
        "SELECT * FROM bookings WHERE training_id = ? AND user_id = ?",
        (training_id, user_id),
    )


async def get_participants(training_id: int) -> list[aiosqlite.Row]:
    """Записавшиеся + лист ожидания с данными профиля (для аватарок)."""
    return await _fetchall(
        """
        SELECT b.status, b.position, b.created_at,
               u.telegram_id, u.first_name, u.avatar_file_id, u.display_mode
          FROM bookings b
          JOIN users u ON u.telegram_id = b.user_id
         WHERE b.training_id = ? AND b.status IN ('booked', 'waitlist')
         ORDER BY CASE b.status WHEN 'booked' THEN 0 ELSE 1 END, b.position ASC
        """,
        (training_id,),
    )


async def get_participants_for_trainings(
    training_ids: list[int],
) -> dict[int, list[aiosqlite.Row]]:
    """Участники сразу для набора тренировок (убирает N+1 в расписании)."""
    if not training_ids:
        return {}
    # JSON-массив передаётся bind-параметром; SQL остаётся статическим.
    rows = await _fetchall(
        """
        SELECT b.training_id, b.status, b.position, b.created_at,
               u.telegram_id, u.first_name, u.avatar_file_id, u.display_mode
          FROM bookings b
          JOIN users u ON u.telegram_id = b.user_id
         WHERE b.training_id IN (SELECT value FROM json_each(?))
           AND b.status IN ('booked', 'waitlist')
         ORDER BY b.training_id,
                  CASE b.status WHEN 'booked' THEN 0 ELSE 1 END,
                  b.position ASC
        """,
        (json.dumps(training_ids),),
    )
    grouped: dict[int, list[aiosqlite.Row]] = {tid: [] for tid in training_ids}
    for row in rows:
        grouped.setdefault(row["training_id"], []).append(row)
    return grouped


async def get_bookings_for_user(
    user_id: int, training_ids: list[int]
) -> dict[int, str]:
    """Статусы записей пользователя сразу по набору тренировок."""
    if not training_ids:
        return {}
    rows = await _fetchall(
        """
        SELECT training_id, status FROM bookings
         WHERE user_id = ?
           AND training_id IN (SELECT value FROM json_each(?))
        """,
        (user_id, json.dumps(training_ids)),
    )
    return {row["training_id"]: row["status"] for row in rows}


async def get_participants_full(training_id: int) -> list[aiosqlite.Row]:
    """
    Все записи по тренировке (включая отменённые) с профилями.
    Нужно тренеру, чтобы случайно убранного игрока можно было вернуть.
    """
    return await _fetchall(
        """
        SELECT b.status, b.position, b.created_at,
               u.telegram_id, u.first_name, u.username, u.show_username,
               u.avatar_file_id, u.about, u.display_mode
          FROM bookings b
          JOIN users u ON u.telegram_id = b.user_id
         WHERE b.training_id = ?
         ORDER BY CASE b.status
                    WHEN 'booked' THEN 0 WHEN 'waitlist' THEN 1 ELSE 2 END,
                  b.position ASC, b.created_at ASC
        """,
        (training_id,),
    )


async def book_training(training_id: int, user_id: int, capacity: int) -> str:
    """
    Записывает игрока. Если мест нет — ставит в лист ожидания (FIFO).
    Возвращает итоговый статус: 'booked' или 'waitlist'.
    """
    db = _db()
    now = int(time.time())
    async with _lock:
        # Идемпотентность: повторный запрос (двойной тап, retry) не должен
        # менять статус уже записанного игрока. Иначе на заполненной тренировке
        # booked-участник превращается в waitlist и теряет место.
        async with db.execute(
            "SELECT status FROM bookings WHERE training_id = ? AND user_id = ?",
            (training_id, user_id),
        ) as cur:
            existing = await cur.fetchone()
        if existing is not None and existing["status"] in {"booked", "waitlist"}:
            return existing["status"]

        # Считаем занятые места.
        async with db.execute(
            "SELECT COUNT(*) FROM bookings WHERE training_id = ? AND status = 'booked'",
            (training_id,),
        ) as cur:
            row = await cur.fetchone()
            booked = int(row[0]) if row is not None else 0

        if booked < capacity:
            status = "booked"
            position = 0
        else:
            status = "waitlist"
            async with db.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM bookings "
                "WHERE training_id = ? AND status = 'waitlist'",
                (training_id,),
            ) as cur:
                position_row = await cur.fetchone()
                position = int(position_row[0]) if position_row is not None else 1

        await db.execute(
            """
            INSERT INTO bookings (training_id, user_id, status, position, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(training_id, user_id) DO UPDATE SET
                status = excluded.status,
                position = excluded.position,
                created_at = excluded.created_at
            """,
            (training_id, user_id, status, position, now),
        )
        await db.commit()
    return status


async def cancel_booking(training_id: int, user_id: int) -> str:
    """
    Отменяет запись игрока и продвигает первого из листа ожидания (FIFO).
    Возвращает статус продвинутого пользователя или пустую строку.
    """
    db = _db()
    promoted_user: int | None = None
    async with _lock:
        cursor = await db.execute(
            "UPDATE bookings SET status = 'cancelled', position = 0 "
            "WHERE training_id = ? AND user_id = ? "
            "AND status IN ('booked', 'waitlist')",
            (training_id, user_id),
        )
        if cursor.rowcount == 0:
            # Игрок не записан (или уже отменён) — не трогаем очередь.
            await db.commit()
            return ""

        # Продвигаем первого в очереди, если есть свободное место.
        async with db.execute(
            "SELECT capacity FROM trainings WHERE id = ?", (training_id,)
        ) as cur:
            row = await cur.fetchone()
            capacity = row[0] if row else 0
        async with db.execute(
            "SELECT COUNT(*) FROM bookings WHERE training_id = ? AND status = 'booked'",
            (training_id,),
        ) as cur:
            row = await cur.fetchone()
            booked = int(row[0]) if row is not None else 0

        if booked < capacity:
            async with db.execute(
                "SELECT user_id FROM bookings WHERE training_id = ? AND status = 'waitlist' "
                "ORDER BY position ASC LIMIT 1",
                (training_id,),
            ) as cur:
                wl = await cur.fetchone()
            if wl:
                promoted_user = wl[0]
                await db.execute(
                    "UPDATE bookings SET status = 'booked', position = 0 "
                    "WHERE training_id = ? AND user_id = ?",
                    (training_id, promoted_user),
                )
        await db.commit()
    return str(promoted_user) if promoted_user is not None else ""


async def restore_booking(training_id: int, user_id: int, capacity: int) -> str:
    """
    Возвращает снятого игрока.

    Если есть лист ожидания — игрок встаёт в его конец (FIFO), даже когда
    формально есть свободное место: нельзя обгонять очередь при восстановлении.
    """
    db = _db()
    now = int(time.time())
    async with _lock:
        async with db.execute(
            "SELECT status FROM bookings WHERE training_id = ? AND user_id = ?",
            (training_id, user_id),
        ) as cur:
            existing = await cur.fetchone()
        if existing is not None and existing["status"] in {"booked", "waitlist"}:
            return existing["status"]

        async with db.execute(
            "SELECT 1 FROM bookings WHERE training_id = ? AND status = 'waitlist' LIMIT 1",
            (training_id,),
        ) as cur:
            has_waitlist = await cur.fetchone() is not None

        async with db.execute(
            "SELECT COUNT(*) FROM bookings WHERE training_id = ? AND status = 'booked'",
            (training_id,),
        ) as cur:
            row = await cur.fetchone()
            booked = int(row[0]) if row is not None else 0

        if booked < capacity and not has_waitlist:
            status = "booked"
            position = 0
        else:
            status = "waitlist"
            async with db.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM bookings "
                "WHERE training_id = ? AND status = 'waitlist'",
                (training_id,),
            ) as cur:
                position_row = await cur.fetchone()
                position = int(position_row[0]) if position_row is not None else 1

        await db.execute(
            """
            INSERT INTO bookings (training_id, user_id, status, position, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(training_id, user_id) DO UPDATE SET
                status = excluded.status,
                position = excluded.position,
                created_at = excluded.created_at
            """,
            (training_id, user_id, status, position, now),
        )
        await db.commit()
    return status


# --------------------------------------------------------------------------- #
# Напоминания (фоновый планировщик)
# --------------------------------------------------------------------------- #

async def trainings_due_for_reminder(now: int) -> list[aiosqlite.Row]:
    """
    Тренировки, которым пора отправить напоминание:
    reminder_enabled=1, ещё не отправлено, и наступило время (start - offset).
    """
    return await _fetchall(
        """
        SELECT t.* FROM trainings t
         WHERE t.reminder_enabled = 1
           AND t.reminder_sent = 0
           AND t.deleted_at IS NULL
           AND t.start_at > ?
           AND (t.start_at - t.reminder_offset_min * 60) <= ?
        """,
        (now, now),
    )


async def mark_reminder_sent(training_id: int) -> None:
    await _execute("UPDATE trainings SET reminder_sent = 1 WHERE id = ?", (training_id,))


async def notification_targets(training_id: int) -> list[aiosqlite.Row]:
    """Получатели пуша: записавшиеся игроки, у которых включены уведомления."""
    return await _fetchall(
        """
        SELECT u.telegram_id
          FROM bookings b
          JOIN users u ON u.telegram_id = b.user_id
         WHERE b.training_id = ? AND b.status = 'booked'
           AND u.notifications_enabled = 1
           AND u.notify_trainings = 1
        """,
        (training_id,),
    )


# --------------------------------------------------------------------------- #
# Личные сообщения (игрок -> тренер)
# --------------------------------------------------------------------------- #

async def add_message(from_user: int, to_user: int, text: str) -> int:
    """Сохраняет личное сообщение игрока тренеру."""
    return await _execute(
        "INSERT INTO messages (from_user, to_user, text, created_at) VALUES (?, ?, ?, ?)",
        (from_user, to_user, text, int(time.time())),
    )


async def list_inbox(to_user: int, limit: int = 20) -> list[aiosqlite.Row]:
    """Последние входящие сообщения (для тренера)."""
    return await _fetchall(
        """
        SELECT m.text, m.created_at, m.from_user,
               u.first_name AS sender_name, u.display_mode
          FROM messages m
          LEFT JOIN users u ON u.telegram_id = m.from_user
         WHERE m.to_user = ?
         ORDER BY m.created_at DESC
         LIMIT ?
        """,
        (to_user, limit),
    )



# --------------------------------------------------------------------------- #
# Матчи: отдельные игровые встречи, которые создают сами игроки
# --------------------------------------------------------------------------- #

async def create_match(
    *,
    title: str,
    description: str,
    location: str,
    start_at: int,
    duration_min: int,
    max_players: int,
    level: str,
    created_by: int,
) -> int:
    return await _execute(
        """
        INSERT INTO matches (title, description, location, start_at, duration_min,
                             max_players, level, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title, description, location, start_at, duration_min,
            max_players, level, created_by, int(time.time()),
        ),
    )


async def list_upcoming_matches(now: int, until: int) -> list[aiosqlite.Row]:
    return await _fetchall(
        """
        SELECT m.*,
               (SELECT COUNT(*) FROM match_players mp
                 WHERE mp.match_id = m.id) AS players_count
          FROM matches m
         WHERE m.start_at >= ? AND m.start_at <= ? AND m.deleted_at IS NULL
         ORDER BY m.start_at ASC
        """,
        (now, until),
    )


async def get_match(match_id: int) -> aiosqlite.Row | None:
    return await _fetchone(
        """
        SELECT m.*,
               (SELECT COUNT(*) FROM match_players mp
                 WHERE mp.match_id = m.id) AS players_count
          FROM matches m
         WHERE m.id = ?
        """,
        (match_id,),
    )


async def get_match_players(match_id: int) -> list[aiosqlite.Row]:
    return await _fetchall(
        """
        SELECT mp.created_at, u.telegram_id, u.first_name, u.username,
               u.show_username, u.avatar_file_id, u.about, u.display_mode
          FROM match_players mp
          JOIN users u ON u.telegram_id = mp.user_id
         WHERE mp.match_id = ?
         ORDER BY mp.created_at ASC
        """,
        (match_id,),
    )


async def get_match_players_for_matches(
    match_ids: list[int],
) -> dict[int, list[aiosqlite.Row]]:
    if not match_ids:
        return {}
    rows = await _fetchall(
        """
        SELECT mp.match_id, mp.created_at, u.telegram_id, u.first_name,
               u.username, u.show_username, u.avatar_file_id, u.about,
               u.display_mode
          FROM match_players mp
          JOIN users u ON u.telegram_id = mp.user_id
         WHERE mp.match_id IN (SELECT value FROM json_each(?))
         ORDER BY mp.match_id, mp.created_at ASC
        """,
        (json.dumps(match_ids),),
    )
    grouped: dict[int, list[aiosqlite.Row]] = {mid: [] for mid in match_ids}
    for row in rows:
        grouped.setdefault(row["match_id"], []).append(row)
    return grouped


async def join_match(match_id: int, user_id: int, max_players: int) -> str:
    """Возвращает joined либо full. Повторный вход идемпотентен."""
    db = _db()
    async with _lock:
        async with db.execute(
            "SELECT 1 FROM match_players WHERE match_id = ? AND user_id = ?",
            (match_id, user_id),
        ) as cur:
            if await cur.fetchone() is not None:
                return "joined"
        async with db.execute(
            "SELECT COUNT(*) FROM match_players WHERE match_id = ?", (match_id,)
        ) as cur:
            row = await cur.fetchone()
            count = int(row[0]) if row is not None else 0
        if count >= max_players:
            return "full"
        await db.execute(
            "INSERT INTO match_players (match_id, user_id, created_at) VALUES (?, ?, ?)",
            (match_id, user_id, int(time.time())),
        )
        await db.commit()
    return "joined"


async def leave_match(match_id: int, user_id: int) -> bool:
    removed = await _execute_rowcount(
        "DELETE FROM match_players WHERE match_id = ? AND user_id = ?",
        (match_id, user_id),
    )
    return bool(removed)


async def delete_match(match_id: int) -> None:
    await _execute(
        "UPDATE matches SET deleted_at = ? WHERE id = ?",
        (int(time.time()), match_id),
    )


# --------------------------------------------------------------------------- #
# Одно обновляемое сообщение-уведомление на событие и чат
# --------------------------------------------------------------------------- #

async def get_event_notification(
    kind: str, entity_id: int, chat_id: int, user_id: int = 0
) -> aiosqlite.Row | None:
    return await _fetchone(
        """
        SELECT * FROM event_notifications
         WHERE kind = ? AND entity_id = ? AND chat_id = ? AND user_id = ?
        """,
        (kind, entity_id, chat_id, user_id),
    )


async def save_event_notification(
    kind: str,
    entity_id: int,
    chat_id: int,
    user_id: int,
    message_id: int,
    text: str,
) -> None:
    await _execute(
        """
        INSERT INTO event_notifications
            (kind, entity_id, chat_id, user_id, message_id, text, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(kind, entity_id, chat_id, user_id) DO UPDATE SET
            message_id = excluded.message_id,
            text = excluded.text,
            updated_at = excluded.updated_at
        """,
        (kind, entity_id, chat_id, user_id, message_id, text, int(time.time())),
    )


async def list_match_feed_recipients() -> list[aiosqlite.Row]:
    """Кому отправлять обновляемую ленту опубликованных матчей."""
    return await _fetchall(
        """
        SELECT telegram_id FROM users
         WHERE notifications_enabled = 1 AND notify_matches = 1
        """
    )
