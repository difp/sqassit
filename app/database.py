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

from .identity import ANIMALS

# Одно общее соединение на процесс + лок для сериализации доступа.
_conn: aiosqlite.Connection | None = None
_lock = asyncio.Lock()


class MessageConflict(Exception):
    """Тот же client_id уже использован для другого текста в этом диалоге."""


def search_norm(value: str | None) -> str:
    """
    Нормализация текста для поиска.

    SQLite `LIKE`/`lower()` не понимают регистр кириллицы, поэтому функция
    регистрируется в соединении (`connect`) и вызывается прямо в SQL.
    """
    return (value or "").casefold().replace("ё", "е")


def _like_pattern(query: str) -> str:
    """LIKE-шаблон `%query%` с экранированием `%`, `_` и `\\` (ESCAPE '\\')."""
    escaped = (
        search_norm(query)
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _animal_indexes(query: str) -> list[int]:
    """
    Животные, чьё имя совпадает с запросом.

    В анонимном режиме тренер видит только вымышленное имя, поэтому реальное
    имя такого игрока искать нельзя — иначе оно утекает через поиск.
    """
    needle = search_norm(query)
    return [
        index
        for index, (_, name) in enumerate(ANIMALS)
        if needle in search_norm(name)
    ]


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id           INTEGER PRIMARY KEY,
    username              TEXT,
    first_name            TEXT,
    phone                 TEXT NOT NULL DEFAULT '', -- введён вручную или из Telegram-контакта
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
    skill_level           TEXT NOT NULL DEFAULT '',
    show_level            INTEGER NOT NULL DEFAULT 0,
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
    room_code             TEXT,                       -- код комнаты турнира (squashtabs)
    created_at            INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    training_id  INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'booked',   -- booked | waitlist | cancelled
    position     INTEGER NOT NULL DEFAULT 0,       -- порядок в очереди ожидания (FIFO)
    needs_approval INTEGER NOT NULL DEFAULT 0,    -- очередь без подтверждения тренера
    created_at   INTEGER NOT NULL,
    UNIQUE (training_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_trainings_start ON trainings(start_at);
CREATE INDEX IF NOT EXISTS idx_bookings_training ON bookings(training_id, status, position);

CREATE TABLE IF NOT EXISTS training_tournaments (
    training_id INTEGER PRIMARY KEY,
    state_json TEXT NOT NULL,
    revision INTEGER NOT NULL,
    started INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);


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
    created_at  INTEGER NOT NULL,
    client_id   TEXT NOT NULL DEFAULT ''   -- идемпотентность отправки с клиента
);

-- Отметки прочтения личных переписок. Отдельная таблица: срок жизни
-- уведомлений (event_notifications.delete_at) не должен означать «прочитано».
CREATE TABLE IF NOT EXISTS chat_reads (
    user_id      INTEGER NOT NULL,
    peer_id      INTEGER NOT NULL,
    last_read_id INTEGER NOT NULL DEFAULT 0,
    updated_at   INTEGER NOT NULL,
    PRIMARY KEY (user_id, peer_id)
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
    inline_direct INTEGER NOT NULL DEFAULT 0,
    published     INTEGER NOT NULL DEFAULT 1,
    notify_all    INTEGER NOT NULL DEFAULT 0,
    created_by    INTEGER NOT NULL,
    created_at    INTEGER NOT NULL,
    room_code     TEXT,                       -- код комнаты турнира (squashtabs)
    deleted_at    INTEGER
);

CREATE TABLE IF NOT EXISTS match_players (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'booked', -- pending | booked | waitlist | rejected
    decided_by  INTEGER,
    decided_at  INTEGER,
    created_at  INTEGER NOT NULL,
    UNIQUE (match_id, user_id)
);

-- Inline-сообщения Telegram не имеют chat_id/message_id. Сохраняем их отдельно,
-- чтобы обновлять все опубликованные копии одного матча после перезапуска.
CREATE TABLE IF NOT EXISTS match_inline_messages (
    inline_message_id TEXT PRIMARY KEY,
    match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_match_inline_messages_match ON match_inline_messages(match_id);

-- Одна задача на актуальную версию турнира. Запись создаётся вместе с результатами.
CREATE TABLE IF NOT EXISTS tournament_notification_jobs (
    training_id INTEGER PRIMARY KEY,
    revision INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS training_notification_jobs (
    kind TEXT NOT NULL,
    training_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (kind, training_id, user_id)
);

CREATE TRIGGER IF NOT EXISTS enqueue_training_request_insert
AFTER INSERT ON bookings WHEN NEW.status = 'pending'
BEGIN
    INSERT INTO training_notification_jobs(kind, training_id, user_id, updated_at)
    VALUES ('request', NEW.training_id, NEW.user_id, unixepoch())
    ON CONFLICT(kind, training_id, user_id) DO UPDATE SET revision=revision+1, updated_at=excluded.updated_at;
END;
CREATE TRIGGER IF NOT EXISTS enqueue_training_request_update
AFTER UPDATE OF status ON bookings WHEN NEW.status = 'pending' AND OLD.status != 'pending'
BEGIN
    INSERT INTO training_notification_jobs(kind, training_id, user_id, updated_at)
    VALUES ('request', NEW.training_id, NEW.user_id, unixepoch())
    ON CONFLICT(kind, training_id, user_id) DO UPDATE SET revision=revision+1, updated_at=excluded.updated_at;
END;
CREATE TRIGGER IF NOT EXISTS expire_resolved_training_request
AFTER UPDATE OF status ON bookings WHEN OLD.status = 'pending' AND NEW.status != 'pending'
BEGIN
    UPDATE event_notifications SET delete_at = unixepoch()
    WHERE kind = 'training_request' AND entity_id = OLD.training_id AND user_id = OLD.user_id;
END;
CREATE TRIGGER IF NOT EXISTS expire_deleted_training_request
AFTER DELETE ON bookings WHEN OLD.status = 'pending'
BEGIN
    UPDATE event_notifications SET delete_at = unixepoch()
    WHERE kind = 'training_request' AND entity_id = OLD.training_id AND user_id = OLD.user_id;
END;
CREATE TRIGGER IF NOT EXISTS enqueue_training_vacancy_update
AFTER UPDATE OF status ON bookings WHEN
    (OLD.status = 'booked' AND NEW.status != 'booked') OR
    (OLD.status = 'pending' AND OLD.position > 0 AND OLD.needs_approval = 1 AND NEW.status NOT IN ('pending', 'booked'))
BEGIN
    INSERT INTO training_notification_jobs(kind, training_id, user_id, updated_at)
    VALUES ('vacancy', OLD.training_id, OLD.user_id, unixepoch())
    ON CONFLICT(kind, training_id, user_id) DO UPDATE SET revision=revision+1, updated_at=excluded.updated_at;
END;
CREATE TRIGGER IF NOT EXISTS enqueue_training_vacancy_delete
AFTER DELETE ON bookings WHEN OLD.status = 'booked' OR
    (OLD.status = 'pending' AND OLD.position > 0 AND OLD.needs_approval = 1)
BEGIN
    INSERT INTO training_notification_jobs(kind, training_id, user_id, updated_at)
    VALUES ('vacancy', OLD.training_id, OLD.user_id, unixepoch())
    ON CONFLICT(kind, training_id, user_id) DO UPDATE SET revision=revision+1, updated_at=excluded.updated_at;
END;

CREATE TABLE IF NOT EXISTS reminder_deliveries (
    training_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (training_id, user_id)
);

CREATE TABLE IF NOT EXISTS event_notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,             -- training | training_booked | ...
    entity_id   INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL DEFAULT 0, -- 0 = сводка организатору
    message_id  INTEGER NOT NULL DEFAULT 0,
    text        TEXT NOT NULL DEFAULT '',
    updated_at  INTEGER NOT NULL,
    delete_at   INTEGER,                    -- когда удалить сообщение
    phase       TEXT NOT NULL DEFAULT '',   -- preliminary | final | status
    sent_at     INTEGER NOT NULL DEFAULT 0, -- время последней отправки
    final_at    INTEGER,                    -- когда заменить предварительное
    UNIQUE (kind, entity_id, chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS event_coauthors (
    event_type  TEXT NOT NULL,
    event_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    added_by    INTEGER,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (event_type, event_id, user_id)
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
    await _conn.create_function("search_norm", 1, search_norm, deterministic=True)
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
    matches_cols = await columns_of("matches")
    match_players_cols = await columns_of("match_players")
    bookings_cols = await columns_of("bookings")
    event_notifications_cols = await columns_of("event_notifications")
    messages_cols = await columns_of("messages")
    onboarding_was_present = "onboarding_seen" in users_cols

    additions = {
        ("users", "skill_level"): "ALTER TABLE users ADD COLUMN skill_level TEXT NOT NULL DEFAULT ''",
        ("users", "show_level"): "ALTER TABLE users ADD COLUMN show_level INTEGER NOT NULL DEFAULT 0",
        ("bookings", "needs_approval"): "ALTER TABLE bookings ADD COLUMN needs_approval INTEGER NOT NULL DEFAULT 0",
        ("trainings", "deleted_at"): "ALTER TABLE trainings ADD COLUMN deleted_at INTEGER",
        ("trainings", "deleted_by"): "ALTER TABLE trainings ADD COLUMN deleted_by INTEGER",
        ("trainings", "level"): "ALTER TABLE trainings ADD COLUMN level TEXT NOT NULL DEFAULT ''",
        ("trainings", "room_code"): "ALTER TABLE trainings ADD COLUMN room_code TEXT",
        ("matches", "room_code"): "ALTER TABLE matches ADD COLUMN room_code TEXT",
        ("matches", "inline_direct"): "ALTER TABLE matches ADD COLUMN inline_direct INTEGER NOT NULL DEFAULT 0",
        ("matches", "published"): "ALTER TABLE matches ADD COLUMN published INTEGER NOT NULL DEFAULT 1",
        ("matches", "notify_all"): "ALTER TABLE matches ADD COLUMN notify_all INTEGER NOT NULL DEFAULT 0",
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
        ("users", "phone"): "ALTER TABLE users ADD COLUMN phone TEXT NOT NULL DEFAULT ''",
        ("coaches", "is_manager"): "ALTER TABLE coaches ADD COLUMN is_manager INTEGER NOT NULL DEFAULT 0",
        ("match_players", "status"): (
            "ALTER TABLE match_players ADD COLUMN status TEXT NOT NULL DEFAULT 'booked'"
        ),
        ("match_players", "decided_by"): (
            "ALTER TABLE match_players ADD COLUMN decided_by INTEGER"
        ),
        ("match_players", "decided_at"): (
            "ALTER TABLE match_players ADD COLUMN decided_at INTEGER"
        ),
        ("event_notifications", "delete_at"): (
            "ALTER TABLE event_notifications ADD COLUMN delete_at INTEGER"
        ),
        ("event_notifications", "phase"): (
            "ALTER TABLE event_notifications ADD COLUMN phase TEXT NOT NULL DEFAULT ''"
        ),
        ("event_notifications", "sent_at"): (
            "ALTER TABLE event_notifications ADD COLUMN sent_at INTEGER NOT NULL DEFAULT 0"
        ),
        ("event_notifications", "final_at"): (
            "ALTER TABLE event_notifications ADD COLUMN final_at INTEGER"
        ),
        ("messages", "client_id"): (
            "ALTER TABLE messages ADD COLUMN client_id TEXT NOT NULL DEFAULT ''"
        ),
    }
    present = {
        "trainings": trainings_cols,
        "users": users_cols,
        "coaches": coaches_cols,
        "matches": matches_cols,
        "match_players": match_players_cols,
        "bookings": bookings_cols,
        "event_notifications": event_notifications_cols,
        "messages": messages_cols,
    }
    for (table, column), ddl in additions.items():
        if column not in present[table]:
            await _conn.execute(ddl)

    # Индексы для сообщений создаём после миграции: в старой БД колонки
    # client_id ещё нет. Ключ идемпотентности — тройка (отправитель,
    # получатель, client_id): один и тот же клиентский id можно безопасно
    # использовать в разных диалогах, но не для двух разных текстов.
    # Перед созданием UNIQUE-индекса развязываем возможные дубли старых БД.
    await _conn.execute(
        "UPDATE messages SET client_id = '' "
        "WHERE client_id != '' AND id NOT IN ("
        "  SELECT MIN(id) FROM messages WHERE client_id != '' "
        "  GROUP BY from_user, to_user, client_id)"
    )
    await _conn.execute("DROP INDEX IF EXISTS idx_messages_client")
    await _conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_client "
        "ON messages(from_user, to_user, client_id) WHERE client_id != ''"
    )
    await _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_pair "
        "ON messages(from_user, to_user, id)"
    )
    await _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_pair_rev "
        "ON messages(to_user, from_user, id)"
    )

    # Индекс создаём после миграции: в старой БД колонки delete_at ещё нет.
    await _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_notifications_delete "
        "ON event_notifications(delete_at)"
    )

    # Старые сводки, ранее сохранённые без срока, тоже подлежат очистке.
    await _conn.execute(
        "UPDATE event_notifications SET delete_at = updated_at + 86400 "
        "WHERE delete_at IS NULL AND kind IN ('training', 'match', 'match_feed')"
    )

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
    phone: str | None = None,
    skill_level: str | None = None,
    show_level: bool | None = None,
) -> None:
    """Сохраняет профиль, приватность, телефон и настройки уведомлений."""
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
               notify_messages = COALESCE(?, notify_messages),
               phone = COALESCE(?, phone),
               skill_level = COALESCE(?, skill_level),
               show_level = COALESCE(?, show_level)
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
            phone,
            skill_level,
            flag(show_level),
            telegram_id,
        ),
    )


async def set_user_phone(telegram_id: int, phone: str) -> None:
    """Сохраняет телефон, полученный кнопкой контакта в боте."""
    await _execute(
        "UPDATE users SET phone = ? WHERE telegram_id = ?", (phone, telegram_id)
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
    db = _db()
    async with _lock:
        try:
            await db.execute("DELETE FROM coaches WHERE telegram_id = ?", (telegram_id,))
            await db.execute(
                "DELETE FROM event_coauthors WHERE event_type = 'training' AND user_id = ?",
                (telegram_id,),
            )
            await db.execute("UPDATE users SET is_coach = 0 WHERE telegram_id = ?", (telegram_id,))
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


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
    db = _db()
    async with _lock:
        try:
            for sql, params in (
                ("DELETE FROM messages WHERE from_user = ? OR to_user = ?", (telegram_id, telegram_id)),
                ("DELETE FROM coach_applications WHERE telegram_id = ?", (telegram_id,)),
                ("DELETE FROM feedback WHERE user_id = ?", (telegram_id,)),
                ("DELETE FROM event_coauthors WHERE user_id = ?", (telegram_id,)),
                ("UPDATE event_coauthors SET added_by = NULL WHERE added_by = ?", (telegram_id,)),
                ("DELETE FROM bookings WHERE user_id = ?", (telegram_id,)),
                ("DELETE FROM match_players WHERE user_id = ?", (telegram_id,)),
                ("DELETE FROM reminder_deliveries WHERE user_id = ?", (telegram_id,)),
                ("DELETE FROM event_notifications WHERE chat_id = ? OR user_id = ?", (telegram_id, telegram_id)),
                ("DELETE FROM matches WHERE created_by = ? AND published = 0", (telegram_id,)),
                ("UPDATE matches SET created_by = 0 WHERE created_by = ?", (telegram_id,)),
                ("UPDATE match_players SET decided_by = NULL WHERE decided_by = ?", (telegram_id,)),
                ("UPDATE coach_applications SET decided_by = NULL WHERE decided_by = ?", (telegram_id,)),
                ("UPDATE coaches SET added_by = NULL WHERE added_by = ?", (telegram_id,)),
                ("UPDATE trainings SET created_by = NULL WHERE created_by = ?", (telegram_id,)),
                ("UPDATE trainings SET deleted_by = NULL WHERE deleted_by = ?", (telegram_id,)),
                ("DELETE FROM users WHERE telegram_id = ?", (telegram_id,)),
            ):
                await db.execute(sql, params)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


async def list_event_coauthors(event_type: str, event_id: int) -> list[aiosqlite.Row]:
    return await _fetchall(
        """
        SELECT u.telegram_id, u.first_name, u.username, u.phone,
               u.avatar_file_id, u.display_mode, u.show_username
          FROM event_coauthors c
          JOIN users u ON u.telegram_id = c.user_id
         WHERE c.event_type = ? AND c.event_id = ?
         ORDER BY c.created_at ASC
        """,
        (event_type, event_id),
    )


async def is_event_coauthor(event_type: str, event_id: int, user_id: int) -> bool:
    return await _fetchone(
        "SELECT 1 FROM event_coauthors "
        "WHERE event_type = ? AND event_id = ? AND user_id = ?",
        (event_type, event_id, user_id),
    ) is not None


async def add_event_coauthor(
    event_type: str, event_id: int, user_id: int, added_by: int
) -> bool:
    return bool(await _execute_rowcount(
        "INSERT OR IGNORE INTO event_coauthors "
        "(event_type, event_id, user_id, added_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (event_type, event_id, user_id, added_by, int(time.time())),
    ))


async def remove_event_coauthor(event_type: str, event_id: int, user_id: int) -> bool:
    return bool(await _execute_rowcount(
        "DELETE FROM event_coauthors WHERE event_type = ? AND event_id = ? AND user_id = ?",
        (event_type, event_id, user_id),
    ))


async def list_all_users() -> list[aiosqlite.Row]:
    """Все пользователи, которым можно доставить закреплённые правила."""
    return await _fetchall("SELECT telegram_id FROM users ORDER BY telegram_id")


async def list_players(query: str | None = None) -> list[aiosqlite.Row]:
    """
    Список игроков для CRM тренера.

    Источник истины — реестр coaches, а не денормализованный users.is_coach:
    после снятия роли игрок не должен навсегда исчезать из CRM.

    `query` фильтрует по имени, username и заметкам. Поиск учитывает только то,
    что тренеру и так видно: реальное имя скрыто в режиме «животное», username —
    без согласия игрока.
    """
    sql = """
        SELECT u.* FROM users u
         WHERE NOT EXISTS (
               SELECT 1 FROM coaches c WHERE c.telegram_id = u.telegram_id
         )
    """
    params: list[Any] = []
    if query:
        clauses = [
            "(u.display_mode <> 'animal' AND search_norm(u.first_name) LIKE ? ESCAPE '\\')"
        ]
        params.append(_like_pattern(query))
        # @username ищется и с собакой, и без неё.
        handle = query.strip().lstrip("@")
        if handle:
            # Поиск в панели тренера: согласие «Показывать @username»
            # управляет видимостью для игроков, тренер находит по нику всегда.
            clauses.append("search_norm(u.username) LIKE ? ESCAPE '\\'")
            params.append(_like_pattern(handle))
        clauses.append("search_norm(u.notes) LIKE ? ESCAPE '\\'")
        params.append(_like_pattern(query))
        animals = _animal_indexes(query)
        if animals:
            marks = ",".join("?" for _ in animals)
            clauses.append(
                f"(u.display_mode = 'animal' AND (u.telegram_id % ?) IN ({marks}))"
            )
            params.append(len(ANIMALS))
            params.extend(animals)
        sql += " AND (" + " OR ".join(clauses) + ")"

    sql += " ORDER BY u.first_name COLLATE NOCASE"
    return await _fetchall(sql, tuple(params))


async def count_players() -> int:
    """Сколько не-тренеров в CRM (для счётчика без загрузки карточек)."""
    row = await _fetchone(
        """
        SELECT COUNT(*) FROM users u
         WHERE NOT EXISTS (
               SELECT 1 FROM coaches c WHERE c.telegram_id = u.telegram_id
         )
        """
    )
    return int(row[0]) if row is not None else 0


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


async def update_training_details(training_id: int, training_type: str, start_at: int) -> bool:
    """Меняет занятие, сохраняя записи; при переносе обновляет напоминания."""
    db = _db()
    async with _lock:
        try:
            async with db.execute(
                "SELECT start_at, duration_min FROM trainings WHERE id = ? AND deleted_at IS NULL",
                (training_id,),
            ) as cur:
                previous = await cur.fetchone()
            if previous is None:
                return False
            changed_time = int(previous["start_at"]) != start_at
            await db.execute(
                "UPDATE trainings SET title = ?, training_type = ?, start_at = ?, "
                "reminder_sent = CASE WHEN ? THEN 0 ELSE reminder_sent END WHERE id = ?",
                (training_type, training_type, start_at, int(changed_time), training_id),
            )
            if changed_time:
                await db.execute("DELETE FROM reminder_deliveries WHERE training_id = ?", (training_id,))
                await db.execute(
                    "UPDATE event_notifications SET delete_at = MIN(?, "
                    "CASE WHEN sent_at > 0 THEN sent_at + 47 * 3600 ELSE ? END) "
                    "WHERE kind = 'training' AND entity_id = ?",
                    (start_at + int(previous["duration_min"]) * 60 + 14400,
                     int(time.time()) + 86400, training_id),
                )
            await db.commit()
            return True
        except BaseException:
            await db.rollback()
            raise


async def update_reminder_settings(
    training_id: int, *, enabled: bool, offset_min: int
) -> None:
    """Тумблер напоминания и смещение таймера."""
    db = _db()
    async with _lock:
        try:
            await db.execute(
                "UPDATE trainings SET reminder_enabled = ?, reminder_offset_min = ?, "
                "reminder_sent = 0 WHERE id = ?",
                (int(enabled), offset_min, training_id),
            )
            await db.execute("DELETE FROM reminder_deliveries WHERE training_id = ?", (training_id,))
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


async def get_training(training_id: int) -> aiosqlite.Row | None:
    return await _fetchone("SELECT * FROM trainings WHERE id = ?", (training_id,))


async def list_user_tournaments(user_id: int, now: int) -> list[aiosqlite.Row]:
    return await _fetchall(
        "SELECT t.* FROM trainings t "
        "JOIN training_tournaments st ON st.training_id = t.id AND st.started = 1 "
        "JOIN bookings b ON b.training_id = t.id AND b.user_id = ? AND b.status = 'booked' "
        "WHERE t.deleted_at IS NULL AND t.start_at + t.duration_min * 60 <= ? ORDER BY t.start_at DESC LIMIT 100",
        (user_id, now),
    )


async def get_training_tournament(training_id: int) -> aiosqlite.Row | None:
    return await _fetchone(
        "SELECT state_json, revision, started, updated_at FROM training_tournaments WHERE training_id = ?",
        (training_id,),
    )


async def save_training_tournament(
    training_id: int, state_json: str, expected_revision: int, started: bool
) -> bool:
    """Атомарная запись с проверкой версии при одновременной работе тренеров."""
    db = _db()
    async with _lock:
        try:
            if expected_revision == 0:
                cursor = await db.execute(
                    "INSERT OR IGNORE INTO training_tournaments "
                    "(training_id, state_json, revision, started, updated_at) VALUES (?, ?, 1, ?, ?)",
                    (training_id, state_json, int(started), int(time.time())),
                )
            else:
                cursor = await db.execute(
                    "UPDATE training_tournaments SET state_json = ?, revision = revision + 1, "
                    "started = ?, updated_at = ? WHERE training_id = ? AND revision = ?",
                    (state_json, int(started), int(time.time()), training_id, expected_revision),
                )
            changed = cursor.rowcount == 1
            await cursor.close()
            if changed:
                await db.execute(
                    "INSERT INTO tournament_notification_jobs (training_id, revision, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(training_id) DO UPDATE SET revision=excluded.revision, updated_at=excluded.updated_at",
                    (training_id, expected_revision + 1, int(time.time())),
                )
            await db.commit()
            return changed
        except BaseException:
            await db.rollback()
            raise


async def list_tournament_notification_jobs() -> list[aiosqlite.Row]:
    return await _fetchall("SELECT training_id, revision FROM tournament_notification_jobs ORDER BY updated_at, training_id")


async def complete_tournament_notification_job(training_id: int, revision: int) -> None:
    await _execute("DELETE FROM tournament_notification_jobs WHERE training_id = ? AND revision = ?", (training_id, revision))


async def list_training_notification_jobs() -> list[aiosqlite.Row]:
    return await _fetchall("SELECT kind, training_id, user_id, revision FROM training_notification_jobs ORDER BY updated_at, training_id")


async def complete_training_notification_job(kind: str, training_id: int, user_id: int, revision: int) -> None:
    await _execute("DELETE FROM training_notification_jobs WHERE kind = ? AND training_id = ? AND user_id = ? AND revision = ?", (kind, training_id, user_id, revision))


async def get_training_room(training_id: int) -> str | None:
    """Код комнаты турнира для занятия (None, если ещё не создавали)."""
    row = await _fetchone(
        "SELECT room_code FROM trainings WHERE id = ?", (training_id,)
    )
    return row["room_code"] if row is not None else None


async def set_training_room(training_id: int, code: str) -> None:
    """Записывает код, только если его ещё нет: два тапа не создадут два кода."""
    await _execute_rowcount(
        "UPDATE trainings SET room_code = ? WHERE id = ? AND room_code IS NULL",
        (code, training_id),
    )


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
                 WHERE b.training_id = t.id AND b.status = 'booked') AS booked_count,
               EXISTS(SELECT 1 FROM training_tournaments st
                       WHERE st.training_id = t.id AND st.started = 1) AS tournament_started
          FROM trainings t
         WHERE t.start_at + t.duration_min * 60 > ? AND t.start_at <= ? AND t.deleted_at IS NULL
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
         WHERE t.start_at + t.duration_min * 60 > ? AND t.deleted_at IS NULL
           AND (
               t.created_by = ?
               OR EXISTS (
                   SELECT 1 FROM event_coauthors c
                    WHERE c.event_type = 'training' AND c.event_id = t.id AND c.user_id = ?
               )
           )
         ORDER BY t.start_at ASC
        """,
        (now, owner_id, owner_id),
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
                 WHERE b.training_id = t.id AND b.status = 'waitlist') AS waitlist_count,
               EXISTS(SELECT 1 FROM training_tournaments st WHERE st.training_id = t.id AND st.started = 1) AS tournament_started
          FROM trainings t
         WHERE t.start_at + t.duration_min * 60 <= ? AND t.deleted_at IS NULL AND (t.created_by = ? OR EXISTS (SELECT 1 FROM event_coauthors c WHERE c.event_type = 'training' AND c.event_id = t.id AND c.user_id = ?))
         ORDER BY t.start_at DESC
         LIMIT ?
        """,
        (now, owner_id, owner_id, limit),
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
         WHERE t.start_at + t.duration_min * 60 > ? AND t.deleted_at IS NULL AND t.created_by != ?
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
        await db.execute("DELETE FROM reminder_deliveries WHERE training_id = ?", (training_id,))
        await db.execute("DELETE FROM bookings WHERE training_id = ?", (training_id,))
        await db.execute("DELETE FROM training_tournaments WHERE training_id = ?", (training_id,))
        await db.execute("DELETE FROM trainings WHERE id = ?", (training_id,))
        await db.execute(
            """
            DELETE FROM event_notifications
             WHERE entity_id = ?
               AND kind IN ('training', 'training_booked',
                            'training_waitlist', 'training_rejected')
            """,
            (training_id,),
        )
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
        SELECT b.status, b.position, b.created_at, b.needs_approval,
               u.telegram_id, u.first_name, u.username, u.phone,
               u.avatar_file_id, u.display_mode
          FROM bookings b
          JOIN users u ON u.telegram_id = b.user_id
         WHERE b.training_id = ? AND b.status IN ('pending', 'booked', 'waitlist')
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
        SELECT b.training_id, b.status, b.position, b.created_at, b.needs_approval,
               u.telegram_id, u.first_name, u.username, u.phone,
               u.avatar_file_id, u.display_mode
          FROM bookings b
          JOIN users u ON u.telegram_id = b.user_id
         WHERE b.training_id IN (SELECT value FROM json_each(?))
           AND b.status IN ('pending', 'booked', 'waitlist')
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
        SELECT b.status, b.position, b.created_at, b.needs_approval,
               u.telegram_id, u.first_name, u.username, u.show_username, u.phone,
               u.avatar_file_id, u.about, u.display_mode, u.skill_level
          FROM bookings b
          JOIN users u ON u.telegram_id = b.user_id
         WHERE b.training_id = ?
         ORDER BY CASE b.status
                    WHEN 'booked' THEN 0 WHEN 'waitlist' THEN 1 ELSE 2 END,
                  b.position ASC, b.created_at ASC
        """,
        (training_id,),
    )


async def training_vacancy_recipients(training_id: int, excluded_user: int) -> list[aiosqlite.Row]:
    """Подходящие игроки, только пока есть место и очередь полностью пуста."""
    return await _fetchall(
        """
        SELECT u.telegram_id FROM users u JOIN trainings t ON t.id = ?
         WHERE t.deleted_at IS NULL AND t.start_at > ? AND t.level <> ''
           AND u.skill_level = t.level AND u.notifications_enabled = 1 AND u.notify_trainings = 1
           AND u.telegram_id != ? AND u.telegram_id != COALESCE(t.created_by, 0)
           AND NOT EXISTS (SELECT 1 FROM bookings b WHERE b.training_id = t.id AND b.user_id = u.telegram_id
                           AND b.status IN ('booked', 'pending', 'waitlist'))
           AND NOT EXISTS (SELECT 1 FROM bookings b WHERE b.training_id = t.id
                           AND (b.status = 'waitlist' OR (b.status = 'pending' AND b.position > 0 AND b.needs_approval = 1)))
           AND (SELECT COUNT(*) FROM bookings b WHERE b.training_id = t.id AND b.status = 'booked') < t.capacity
        """, (training_id, int(time.time()), excluded_user),
    )


async def _queue_pending_if_full(db: aiosqlite.Connection, training_id: int) -> list[int]:
    """Переводит неподтверждённые заявки в конец очереди в той же транзакции."""
    async with db.execute(
        "SELECT capacity, (SELECT COUNT(*) FROM bookings b WHERE b.training_id = t.id AND b.status = 'booked') "
        "FROM trainings t WHERE id = ? AND deleted_at IS NULL AND start_at > ?",
        (training_id, int(time.time())),
    ) as cur:
        training = await cur.fetchone()
    if training is None or int(training[1]) < int(training[0]):
        return []
    async with db.execute(
        "SELECT COALESCE(MAX(position), 0) FROM bookings WHERE training_id = ? AND status IN ('pending', 'waitlist')",
        (training_id,),
    ) as cur:
        result = await cur.fetchone()
        assert result is not None
        max_position = int(result[0])
    async with db.execute(
        "SELECT user_id, position FROM bookings WHERE training_id = ? AND status = 'pending' "
        "ORDER BY CASE WHEN position > 0 THEN 0 ELSE 1 END, position, created_at, id",
        (training_id,),
    ) as cur:
        rows = await cur.fetchall()
    moved = []
    for row in rows:
        position = int(row["position"])
        if not position:
            max_position += 1
            position = max_position
        await db.execute(
            "UPDATE bookings SET status = 'waitlist', position = ?, needs_approval = 1 "
            "WHERE training_id = ? AND user_id = ? AND status = 'pending'",
            (position, training_id, row["user_id"]),
        )
        moved.append(int(row["user_id"]))
    return moved


async def _advance_training_waitlist(db: aiosqlite.Connection, training_id: int) -> int | None:
    """Выдаёт место одобренному либо отправляет первого неОдобренного на решение."""
    async with db.execute(
        "SELECT capacity, (SELECT COUNT(*) FROM bookings b WHERE b.training_id = t.id AND b.status = 'booked'), "
        "(SELECT COUNT(*) FROM bookings b WHERE b.training_id = t.id AND b.status = 'pending' "
        "AND b.position > 0 AND b.needs_approval = 1) FROM trainings t WHERE id = ? AND deleted_at IS NULL AND start_at > ?",
        (training_id, int(time.time())),
    ) as cur:
        row = await cur.fetchone()
    if row is None or int(row[1]) + int(row[2]) >= int(row[0]):
        return None
    async with db.execute(
        "SELECT user_id, needs_approval FROM bookings WHERE training_id = ? AND status = 'waitlist' "
        "ORDER BY position ASC, created_at ASC, id ASC LIMIT 1", (training_id,),
    ) as cur:
        player = await cur.fetchone()
    if player is None:
        return None
    user_id = int(player["user_id"])
    if player["needs_approval"]:
        await db.execute(
            "UPDATE bookings SET status = 'pending' WHERE training_id = ? AND user_id = ?",
            (training_id, user_id),
        )
    else:
        await db.execute(
            "UPDATE bookings SET status = 'booked', position = 0 WHERE training_id = ? AND user_id = ?",
            (training_id, user_id),
        )
    return user_id


async def reconcile_full_training_waitlists() -> list[tuple[int, int]]:
    """Находит ранее заполненные будущие группы, где ещё остались заявки."""
    db = _db()
    async with _lock:
        try:
            async with db.execute(
                "SELECT DISTINCT t.id FROM trainings t JOIN bookings b ON b.training_id = t.id "
                "WHERE t.deleted_at IS NULL AND t.start_at > ? AND b.status = 'pending' "
                "AND (SELECT COUNT(*) FROM bookings p WHERE p.training_id = t.id AND p.status = 'booked') >= t.capacity",
                (int(time.time()),),
            ) as cur:
                trainings = await cur.fetchall()
            moved: list[tuple[int, int]] = []
            for row in trainings:
                tid = int(row[0])
                moved.extend((tid, uid) for uid in await _queue_pending_if_full(db, tid))
            await db.commit()
            return moved
        except BaseException:
            await db.rollback()
            raise


async def book_training(
    training_id: int, user_id: int, capacity: int, *,
    requires_approval: bool = False, respect_waitlist: bool = False,
    moved_to_waitlist: list[int] | None = None,
) -> str:
    """Создаёт заявку или запись, сохраняя порядок очереди."""
    db = _db()
    now = int(time.time())
    async with _lock:
        try:
            async with db.execute(
                "SELECT status FROM bookings WHERE training_id = ? AND user_id = ?", (training_id, user_id),
            ) as cur:
                existing = await cur.fetchone()
            if existing is not None and existing["status"] in {"pending", "booked", "waitlist"}:
                return existing["status"]
            async with db.execute(
                "SELECT COUNT(*) FROM bookings WHERE training_id = ? AND (status = 'booked' "
                "OR (status = 'pending' AND position > 0 AND needs_approval = 1))", (training_id,),
            ) as cur:
                result = await cur.fetchone()
                assert result is not None
                booked = int(result[0])
            async with db.execute(
                "SELECT 1 FROM bookings WHERE training_id = ? AND status = 'waitlist' LIMIT 1", (training_id,),
            ) as cur:
                has_waitlist = await cur.fetchone() is not None
            needs_approval = 0
            if requires_approval and booked < capacity:
                status, position = 'pending', 0
            elif booked < capacity and not (respect_waitlist and has_waitlist):
                status, position = 'booked', 0
            else:
                status = 'waitlist'
                needs_approval = int(requires_approval)
                async with db.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 FROM bookings WHERE training_id = ? "
                    "AND status IN ('waitlist', 'pending')", (training_id,),
                ) as cur:
                    result = await cur.fetchone()
                    assert result is not None
                    position = int(result[0])
            await db.execute(
                "INSERT INTO bookings (training_id, user_id, status, position, needs_approval, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(training_id, user_id) DO UPDATE SET "
                "status = excluded.status, position = excluded.position, needs_approval = excluded.needs_approval, "
                "created_at = excluded.created_at",
                (training_id, user_id, status, position, needs_approval, now),
            )
            moved = await _queue_pending_if_full(db, training_id) if status == 'booked' else []
            await db.commit()
            if moved_to_waitlist is not None:
                moved_to_waitlist.extend(moved)
            return status
        except BaseException:
            await db.rollback()
            raise


async def approve_booking(
    training_id: int, user_id: int, *, moved_to_waitlist: list[int] | None = None,
) -> str:
    """Одобряет заявку и выдаёт место либо ставит в одобренную очередь."""
    db = _db()
    async with _lock:
        try:
            async with db.execute(
                "SELECT status, position FROM bookings WHERE training_id = ? AND user_id = ?", (training_id, user_id),
            ) as cur:
                row = await cur.fetchone()
            if row is None or row['status'] != 'pending':
                return row['status'] if row is not None else ''
            async with db.execute(
                "SELECT capacity, (SELECT COUNT(*) FROM bookings b WHERE b.training_id = t.id AND "
                "(b.status = 'booked' OR (b.status = 'pending' AND b.position > 0 AND b.needs_approval = 1 "
                "AND b.user_id != ?))) FROM trainings t WHERE id = ?", (user_id, training_id),
            ) as cur:
                capacity_row = await cur.fetchone()
            capacity = int(capacity_row[0]) if capacity_row else 0
            booked = int(capacity_row[1]) if capacity_row else 0
            if booked < capacity:
                status, position = 'booked', 0
            else:
                status = 'waitlist'
                position = int(row['position'])
                if not position:
                    async with db.execute(
                        "SELECT COALESCE(MAX(position), 0) + 1 FROM bookings WHERE training_id = ? "
                        "AND status IN ('waitlist', 'pending')", (training_id,),
                    ) as cur:
                        result = await cur.fetchone()
                    assert result is not None
                    position = int(result[0])
            await db.execute(
                "UPDATE bookings SET status = ?, position = ?, needs_approval = 0 WHERE training_id = ? AND user_id = ?",
                (status, position, training_id, user_id),
            )
            moved = await _queue_pending_if_full(db, training_id) if status == 'booked' else []
            await db.commit()
            if moved_to_waitlist is not None:
                moved_to_waitlist.extend(moved)
            return status
        except BaseException:
            await db.rollback()
            raise


async def reject_booking(training_id: int, user_id: int, *, advanced: list[int] | None = None) -> bool:
    """Отклоняет заявку и при свободном месте обращается к следующему в очереди."""
    db = _db()
    async with _lock:
        try:
            cursor = await db.execute(
                "UPDATE bookings SET status = 'rejected', position = 0, needs_approval = 0 "
                "WHERE training_id = ? AND user_id = ? AND status = 'pending'", (training_id, user_id),
            )
            changed = bool(cursor.rowcount)
            promoted = await _advance_training_waitlist(db, training_id) if changed else None
            await db.commit()
            if promoted is not None and advanced is not None:
                advanced.append(promoted)
            return changed
        except BaseException:
            await db.rollback()
            raise


async def cancel_booking(
    training_id: int, user_id: int, *, moved_to_waitlist: list[int] | None = None,
) -> str:
    """Отменяет запись и предлагает свободное место первому в очереди."""
    db = _db()
    async with _lock:
        try:
            cursor = await db.execute(
                "UPDATE bookings SET status = 'cancelled', position = 0, needs_approval = 0 "
                "WHERE training_id = ? AND user_id = ? AND status IN ('pending', 'booked', 'waitlist')",
                (training_id, user_id),
            )
            if cursor.rowcount == 0:
                await db.commit()
                return ''
            promoted = await _advance_training_waitlist(db, training_id)
            moved = await _queue_pending_if_full(db, training_id) if promoted is not None else []
            await db.commit()
            if moved_to_waitlist is not None:
                moved_to_waitlist.extend(moved)
            return str(promoted) if promoted is not None else ''
        except BaseException:
            await db.rollback()
            raise


async def restore_booking(
    training_id: int, user_id: int, capacity: int, *, moved_to_waitlist: list[int] | None = None,
) -> str:
    """Возвращает снятого тренером игрока, не обходя очередь."""
    return await book_training(
        training_id, user_id, capacity, respect_waitlist=True, moved_to_waitlist=moved_to_waitlist,
    )


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
           AND NOT EXISTS (SELECT 1 FROM reminder_deliveries d WHERE d.training_id = b.training_id AND d.user_id = b.user_id)
        """,
        (training_id,),
    )


async def mark_reminder_delivered(training_id: int, user_id: int) -> None:
    await _execute(
        "INSERT OR IGNORE INTO reminder_deliveries (training_id, user_id) VALUES (?, ?)",
        (training_id, user_id),
    )


# --------------------------------------------------------------------------- #
# Личные сообщения (игрок -> тренер)
# --------------------------------------------------------------------------- #

async def add_message(from_user: int, to_user: int, text: str, client_id: str = "") -> int:
    """Сохраняет личное сообщение игрока тренеру."""
    return await _execute(
        "INSERT INTO messages (from_user, to_user, text, created_at, client_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (from_user, to_user, text, int(time.time()), client_id),
    )


async def add_message_once(
    from_user: int, to_user: int, text: str, client_id: str = ""
) -> tuple[int, bool]:
    """
    Идемпотентно сохраняет сообщение.

    Повторная отправка с тем же client_id (двойной тап, ретрай после
    таймаута, повторный POST) не создаёт дубль. Возвращает (id, created):
    created=False означает, что сообщение уже было сохранено раньше.
    Пустой client_id отключает идемпотентность — как обычный add_message.

    Если тот же client_id пришёл с другим текстом, это ошибка клиента:
    поднимаем MessageConflict, чтобы вызывающий код вернул 409, а не
    «успешно» проглотил потерянное сообщение.
    """
    if not client_id:
        return await add_message(from_user, to_user, text), True
    try:
        return await add_message(from_user, to_user, text, client_id), True
    except aiosqlite.IntegrityError:
        # Гонка двух параллельных отправок: второй INSERT упал на UNIQUE.
        # Обязательно откатываем транзакцию, иначе соединение остаётся
        # в состоянии in_transaction и блокирует последующие записи.
        await _db().rollback()
        row = await _fetchone(
            "SELECT id, text FROM messages "
            "WHERE from_user = ? AND to_user = ? AND client_id = ?",
            (from_user, to_user, client_id),
        )
        if row is None:
            raise
        if row["text"] != text:
            raise MessageConflict(client_id)
        return int(row["id"]), False


async def list_user_thread(
    user_id: int,
    peer_id: int | None = None,
    limit: int = 50,
    before_id: int | None = None,
    after_id: int | None = None,
) -> tuple[list[aiosqlite.Row], bool]:
    """
    Страница переписки пользователя; has_more — есть ли более старые.

    peer_id сужает выборку до одного собеседника (у игрока может быть
    несколько тренеров). after_id отдаёт только новые сообщения по
    возрастанию — для опроса открытого чата.
    """
    rows = await _fetchall(
        """
        SELECT m.id, m.from_user, m.to_user, m.text, m.created_at
          FROM messages m
         WHERE (? IS NULL
                OR (m.from_user = ? AND m.to_user = ?)
                OR (m.from_user = ? AND m.to_user = ?))
           AND (? IS NULL OR m.id < ?)
           AND (? IS NULL OR m.id > ?)
         ORDER BY m.id DESC
         LIMIT ?
        """,
        (
            peer_id, user_id, peer_id, peer_id, user_id,
            before_id, before_id, after_id, after_id,
            limit + 1,
        ),
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    if after_id is None:
        page.reverse()
    return page, has_more


async def list_coach_thread(
    coach_id: int,
    user_id: int,
    limit: int = 50,
    before_id: int | None = None,
    after_id: int | None = None,
) -> tuple[list[aiosqlite.Row], bool]:
    rows, has_more = await list_user_thread(
        coach_id,
        peer_id=user_id,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )
    return rows, has_more


async def mark_chat_read(
    user_id: int, peer_id: int, last_read_id: int | None = None
) -> int:
    """Запоминает, до какого сообщения прочитана переписка с собеседником."""
    if last_read_id is None:
        row = await _fetchone(
            "SELECT COALESCE(MAX(id), 0) AS last_id FROM messages "
            "WHERE (from_user = ? AND to_user = ?) "
            "   OR (from_user = ? AND to_user = ?)",
            (peer_id, user_id, user_id, peer_id),
        )
        last_read_id = int(row["last_id"]) if row is not None else 0
    await _execute(
        "INSERT INTO chat_reads (user_id, peer_id, last_read_id, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, peer_id) DO UPDATE SET "
        "  last_read_id = MAX(chat_reads.last_read_id, excluded.last_read_id), "
        "  updated_at = excluded.updated_at",
        (user_id, peer_id, max(0, int(last_read_id)), int(time.time())),
    )
    return max(0, int(last_read_id))


async def unread_by_peer(user_id: int) -> dict[int, int]:
    """Сколько входящих сообщений не прочитано от каждого собеседника."""
    rows = await _fetchall(
        """
        SELECT m.from_user AS peer_id, COUNT(*) AS unread
          FROM messages m
          LEFT JOIN chat_reads r
            ON r.user_id = ? AND r.peer_id = m.from_user
         WHERE m.to_user = ? AND m.id > COALESCE(r.last_read_id, 0)
         GROUP BY m.from_user
        """,
        (user_id, user_id),
    )
    return {int(row["peer_id"]): int(row["unread"]) for row in rows}


async def list_coach_conversations(
    coach_id: int, limit: int = 100
) -> list[dict[str, Any]]:
    """
    Последнее сообщение по каждому игроку для панели тренера.

    Один запрос вместо выгрузки всей переписки в память: MAX(id) по
    собеседникам + JOIN на само сообщение и карточку игрока.
    """
    rows = await _fetchall(
        """
        WITH peers AS (
            SELECT CASE WHEN from_user = ? THEN to_user ELSE from_user END AS peer_id,
                   MAX(id) AS last_id
              FROM messages
             WHERE from_user = ? OR to_user = ?
             GROUP BY peer_id
        )
        SELECT m.id, m.from_user, m.to_user, m.text, m.created_at,
               p.peer_id AS other_id,
               u.first_name, u.display_mode, u.avatar_file_id
          FROM peers p
          JOIN messages m ON m.id = p.last_id
          LEFT JOIN users u ON u.telegram_id = p.peer_id
         ORDER BY p.last_id DESC
         LIMIT ?
        """,
        (coach_id, coach_id, coach_id, limit),
    )
    unread = await unread_by_peer(coach_id)
    return [
        {**dict(row), "unread": unread.get(int(row["other_id"]), 0)}
        for row in rows
    ]



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
    inline_direct: bool = False,
    inline_draft: bool = False,
    notify_all: bool = False,
) -> int:
    return await _execute(
        """
        INSERT INTO matches (title, description, location, start_at, duration_min,
                             max_players, level, created_by, created_at, inline_direct, published, notify_all)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title, description, location, start_at, duration_min,
            max_players, level, created_by, int(time.time()), int(inline_direct), int(not inline_draft), int(notify_all),
        ),
    )


async def list_upcoming_matches(now: int, until: int) -> list[aiosqlite.Row]:
    return await _fetchall(
        """
        SELECT m.*,
               (SELECT COUNT(*) FROM match_players mp
                 WHERE mp.match_id = m.id AND mp.status = 'booked') AS players_count
          FROM matches m
         WHERE m.start_at >= ? AND m.start_at <= ? AND m.deleted_at IS NULL AND m.published = 1
         ORDER BY m.start_at ASC
        """,
        (now, until),
    )


async def list_owned_match_drafts(user_id: int, now: int, until: int) -> list[aiosqlite.Row]:
    return await _fetchall(
        "SELECT m.*, (SELECT COUNT(*) FROM match_players mp WHERE mp.match_id=m.id AND mp.status='booked') AS players_count "
        "FROM matches m WHERE m.created_by=? AND m.published=0 AND m.deleted_at IS NULL "
        "AND m.start_at>=? AND m.start_at<=? ORDER BY m.start_at", (user_id, now, until),
    )


async def publish_match_draft(match_id: int) -> bool:
    return bool(await _execute_rowcount(
        "UPDATE matches SET published=1 WHERE id=? AND published=0 AND deleted_at IS NULL AND start_at>?",
        (match_id, int(time.time())),
    ))


async def get_match(match_id: int) -> aiosqlite.Row | None:
    return await _fetchone(
        """
        SELECT m.*,
               (SELECT COUNT(*) FROM match_players mp
                 WHERE mp.match_id = m.id AND mp.status = 'booked') AS players_count
          FROM matches m
         WHERE m.id = ?
        """,
        (match_id,),
    )


async def save_match_inline_message(match_id: int, inline_message_id: str) -> None:
    """Запоминает копию опубликованной карточки; повторный update идемпотентен."""
    await _execute(
        "INSERT INTO match_inline_messages (match_id, inline_message_id, created_at) "
        "VALUES (?, ?, ?) ON CONFLICT(inline_message_id) DO NOTHING",
        (match_id, inline_message_id, int(time.time())),
    )


async def list_match_inline_messages(match_id: int) -> list[aiosqlite.Row]:
    return await _fetchall(
        "SELECT inline_message_id FROM match_inline_messages WHERE match_id = ? ORDER BY created_at, inline_message_id",
        (match_id,),
    )


async def list_recent_inline_match_ids(since: int) -> list[aiosqlite.Row]:
    """Матчи с опубликованными карточками для повторной синхронизации."""
    return await _fetchall(
        "SELECT DISTINCT m.id FROM matches m JOIN match_inline_messages i ON i.match_id = m.id "
        "WHERE m.start_at >= ? ORDER BY m.id", (since,)
    )


async def delete_match_inline_message(inline_message_id: str) -> None:
    await _execute_rowcount(
        "DELETE FROM match_inline_messages WHERE inline_message_id = ?", (inline_message_id,)
    )


async def get_match_room(match_id: int) -> str | None:
    """Код комнаты турнира для матча."""
    row = await _fetchone(
        "SELECT room_code FROM matches WHERE id = ?", (match_id,)
    )
    return row["room_code"] if row is not None else None


async def set_match_room(match_id: int, code: str) -> None:
    """Записывает код, только если его ещё нет: два тапа не создадут два кода."""
    await _execute_rowcount(
        "UPDATE matches SET room_code = ? WHERE id = ? AND room_code IS NULL",
        (code, match_id),
    )


async def get_match_players(match_id: int) -> list[aiosqlite.Row]:
    return await _fetchall(
        """
        SELECT mp.created_at, mp.status, u.telegram_id, u.first_name, u.username,
               u.show_username, u.avatar_file_id, u.about, u.display_mode
          FROM match_players mp
          JOIN users u ON u.telegram_id = mp.user_id
         WHERE mp.match_id = ?
         ORDER BY mp.created_at ASC
        """,
        (match_id,),
    )


async def get_match_player_status(match_id: int, user_id: int) -> str | None:
    row = await _fetchone(
        "SELECT status FROM match_players WHERE match_id = ? AND user_id = ?",
        (match_id, user_id),
    )
    return str(row["status"]) if row is not None else None


async def get_match_players_for_matches(
    match_ids: list[int],
) -> dict[int, list[aiosqlite.Row]]:
    if not match_ids:
        return {}
    rows = await _fetchall(
        """
        SELECT mp.match_id, mp.created_at, mp.status, u.telegram_id, u.first_name,
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


async def join_match(
    match_id: int,
    user_id: int,
    max_players: int,
    *,
    approved: bool = False,
    requires_approval: bool = False,
    distinguish_existing: bool = False,
) -> str:
    """Создаёт заявку или сразу добавляет игрока. Повторный вход идемпотентен."""
    db = _db()
    async with _lock:
        async with db.execute(
            "SELECT deleted_at, start_at, max_players, published FROM matches WHERE id = ?",
            (match_id,),
        ) as cur:
            match = await cur.fetchone()
        if match is None or match["deleted_at"]:
            return "unavailable"
        if not match["published"] and not approved:
            return "unavailable"
        if match["start_at"] <= int(time.time()):
            return "started"
        max_players = int(match["max_players"])
        async with db.execute(
            "SELECT status FROM match_players WHERE match_id = ? AND user_id = ?",
            (match_id, user_id),
        ) as cur:
            existing = await cur.fetchone()
        if existing is not None:
            status = existing["status"]
            return f"existing:{status}" if distinguish_existing else status
        if approved:
            status = "booked"
        elif not requires_approval:
            async with db.execute(
                "SELECT COUNT(*) FROM match_players "
                "WHERE match_id = ? AND status = 'booked'",
                (match_id,),
            ) as cur:
                row = await cur.fetchone()
            count = int(row[0]) if row is not None else 0
            if count >= max_players:
                return "full"
            status = "booked"
        else:
            status = "pending"
        await db.execute(
            "INSERT INTO match_players (match_id, user_id, status, created_at) "
            "VALUES (?, ?, ?, ?)",
            (match_id, user_id, status, int(time.time())),
        )
        await db.commit()
    return status


async def approve_match_player(match_id: int, user_id: int, decided_by: int) -> str:
    """Одобряет заявку на матч: место или очередь."""
    db = _db()
    async with _lock:
        async with db.execute(
            "SELECT status FROM match_players WHERE match_id = ? AND user_id = ?",
            (match_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None or row["status"] != "pending":
            return row["status"] if row is not None else ""
        async with db.execute(
            "SELECT max_players FROM matches WHERE id = ?", (match_id,)
        ) as cur:
            cap = await cur.fetchone()
        max_players = int(cap[0]) if cap is not None else 0
        async with db.execute(
            "SELECT COUNT(*) FROM match_players WHERE match_id = ? AND status = 'booked'",
            (match_id,),
        ) as cur:
            booked_row = await cur.fetchone()
        booked = int(booked_row[0]) if booked_row is not None else 0
        status = "booked" if booked < max_players else "waitlist"
        await db.execute(
            "UPDATE match_players SET status = ?, decided_by = ?, decided_at = ? "
            "WHERE match_id = ? AND user_id = ?",
            (status, decided_by, int(time.time()), match_id, user_id),
        )
        await db.commit()
    return status


async def reject_match_player(match_id: int, user_id: int, decided_by: int) -> bool:
    changed = await _execute_rowcount(
        "UPDATE match_players SET status = 'rejected', decided_by = ?, decided_at = ? "
        "WHERE match_id = ? AND user_id = ? AND status = 'pending'",
        (decided_by, int(time.time()), match_id, user_id),
    )
    return bool(changed)


async def leave_match(match_id: int, user_id: int) -> tuple[bool, int | None]:
    """Удаляет участника и атомарно продвигает первого ожидающего."""
    db = _db()
    async with _lock:
        async with db.execute(
            "SELECT status FROM match_players WHERE match_id = ? AND user_id = ?",
            (match_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return False, None
        await db.execute("DELETE FROM match_players WHERE match_id = ? AND user_id = ?", (match_id, user_id))
        promoted = None
        if row["status"] == "booked":
            async with db.execute(
                "SELECT user_id FROM match_players WHERE match_id = ? AND status = 'waitlist' ORDER BY created_at, id LIMIT 1",
                (match_id,),
            ) as cur:
                waiting = await cur.fetchone()
            if waiting:
                promoted = int(waiting["user_id"])
                await db.execute(
                    "UPDATE match_players SET status = 'booked' WHERE match_id = ? AND user_id = ?",
                    (match_id, promoted),
                )
        await db.commit()
        return True, promoted


async def user_matches(user_id: int) -> list[aiosqlite.Row]:
    return await _fetchall("SELECT match_id FROM match_players WHERE user_id = ?", (user_id,))


async def has_upcoming_owned_match(user_id: int, now: int) -> bool:
    return await _fetchone(
        "SELECT id FROM matches WHERE created_by = ? AND deleted_at IS NULL AND published=1 AND start_at > ? LIMIT 1",
        (user_id, now),
    ) is not None


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
    *,
    delete_at: int | None = None,
    phase: str = "",
    sent_at: int | None = None,
    final_at: int | None = None,
) -> None:
    now = int(time.time())
    if delete_at is None and kind != "rules":
        delete_at = now + 86400
        if kind == "match_announcement":
            event = await _fetchone("SELECT start_at FROM matches WHERE id = ?", (entity_id,))
            if event is not None:
                delete_at = min(int(event["start_at"]), delete_at)
        if kind in {"training", "match"}:
            query = (
                "SELECT start_at, duration_min, deleted_at FROM trainings WHERE id = ?"
                if kind == "training" else
                "SELECT start_at, duration_min, deleted_at FROM matches WHERE id = ?"
            )
            event = await _fetchone(query, (entity_id,))
            if event is not None and not event["deleted_at"]:
                delete_at = min(int(event["start_at"]) + int(event["duration_min"]) * 60 + 14400, now + 47 * 3600)
    await _execute(
        """
        INSERT INTO event_notifications
            (kind, entity_id, chat_id, user_id, message_id, text, updated_at,
             delete_at, phase, sent_at, final_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(kind, entity_id, chat_id, user_id) DO UPDATE SET
            message_id = excluded.message_id,
            text = excluded.text,
            updated_at = excluded.updated_at,
            delete_at = CASE
                WHEN event_notifications.message_id = excluded.message_id
                     AND event_notifications.delete_at IS NOT NULL
                     AND excluded.delete_at IS NOT NULL
                THEN MIN(event_notifications.delete_at, excluded.delete_at)
                ELSE excluded.delete_at END,
            phase = excluded.phase,
            sent_at = CASE WHEN event_notifications.message_id = excluded.message_id
                      THEN event_notifications.sent_at ELSE excluded.sent_at END,
            final_at = excluded.final_at
        """,
        (
            kind,
            entity_id,
            chat_id,
            user_id,
            message_id,
            text,
            now,
            delete_at,
            phase,
            now if sent_at is None else sent_at,
            final_at,
        ),
    )


async def update_event_notification_text(
    kind: str, entity_id: int, chat_id: int, user_id: int, text: str
) -> None:
    """Обновляет только текст, сохраняя таймеры и время отправки."""
    await _execute(
        """
        UPDATE event_notifications
           SET text = ?, updated_at = ?
         WHERE kind = ? AND entity_id = ? AND chat_id = ? AND user_id = ?
        """,
        (text, int(time.time()), kind, entity_id, chat_id, user_id),
    )


async def clear_event_notification_message(row_id: int) -> None:
    """Сообщение удалено, но запись нужна для финальной отправки."""
    await _execute(
        "UPDATE event_notifications SET message_id = 0, delete_at = NULL WHERE id = ?",
        (row_id,),
    )


async def delete_event_notification_by_id(row_id: int) -> None:
    await _execute("DELETE FROM event_notifications WHERE id = ?", (row_id,))


async def expire_event_notifications(kind: str, entity_id: int, chat_id: int | None = None) -> None:
    """Помечает уведомления устаревшими для фонового удаления."""
    await _execute(
        "UPDATE event_notifications SET delete_at = ? "
        "WHERE kind = ? AND entity_id = ? AND (? IS NULL OR chat_id = ?)",
        (int(time.time()), kind, entity_id, chat_id, chat_id),
    )


async def expire_training_request_notifications(training_id: int, user_id: int) -> None:
    """Помечает уведомления тренеров об уже рассмотренной заявке."""
    await _execute(
        "UPDATE event_notifications SET delete_at = ? "
        "WHERE kind = 'training_request' AND entity_id = ? AND user_id = ?",
        (int(time.time()), training_id, user_id),
    )


async def list_match_feed_notifications() -> list[aiosqlite.Row]:
    return await _fetchall("SELECT * FROM event_notifications WHERE kind = 'match_feed'")


async def list_event_notifications_for_training(
    training_id: int,
) -> list[aiosqlite.Row]:
    return await _fetchall(
        "SELECT * FROM event_notifications WHERE entity_id = ?",
        (training_id,),
    )


async def list_event_notifications_for_player(
    training_id: int, user_id: int
) -> list[aiosqlite.Row]:
    return await _fetchall(
        """
        SELECT * FROM event_notifications
         WHERE entity_id = ? AND (chat_id = ? OR user_id = ?)
        """,
        (training_id, user_id, user_id),
    )


async def list_due_event_notifications(now: int) -> list[aiosqlite.Row]:
    return await _fetchall(
        """
        SELECT * FROM event_notifications
         WHERE delete_at IS NOT NULL AND delete_at <= ?
         ORDER BY delete_at ASC
        """,
        (now,),
    )


async def list_due_final_enrollments(now: int) -> list[aiosqlite.Row]:
    """Предварительные подтверждения, которым пора стать финальными."""
    return await _fetchall(
        """
        SELECT n.*, t.start_at, t.duration_min, t.training_type, t.level,
               t.created_by, t.capacity, t.deleted_at
          FROM event_notifications n
          JOIN trainings t ON t.id = n.entity_id
         WHERE n.kind = 'training_booked'
           AND n.phase = 'preliminary'
           AND n.final_at IS NOT NULL
           AND n.final_at <= ?
           AND t.deleted_at IS NULL
           AND t.start_at > ?
         ORDER BY n.final_at ASC
        """,
        (now, now),
    )


async def list_stale_preliminary_enrollments(now: int) -> list[aiosqlite.Row]:
    """Предварительные записи, чьё занятие уже прошло или удалено."""
    return await _fetchall(
        """
        SELECT n.* FROM event_notifications n
          LEFT JOIN trainings t ON t.id = n.entity_id
         WHERE n.kind = 'training_booked'
           AND n.phase = 'preliminary'
           AND n.final_at IS NOT NULL
           AND n.final_at <= ?
           AND (t.id IS NULL OR t.deleted_at IS NOT NULL OR t.start_at <= ?)
        """,
        (now, now),
    )


async def list_match_feed_recipients() -> list[aiosqlite.Row]:
    """Кому отправлять обновляемую ленту опубликованных матчей."""
    return await _fetchall(
        """
        SELECT telegram_id FROM users
         WHERE notifications_enabled = 1 AND notify_matches = 1
        """
    )
