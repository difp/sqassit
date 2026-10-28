"""
Кэш аватарок: версия URL, диск, память и защита Telegram.

Аватарки — расходник: источник истины остаётся в Telegram (``file_id`` в
``users``), поэтому кэш можно удалить в любой момент. ``make backup`` копирует
только SQLite, так что кэш не попадает в бэкапы.

Схема:
  * версия URL считается от ``file_id`` и меняется только при смене фото;
  * файл лежит в ``<root>/<telegram_id>/<version>.img``;
  * поверх диска — маленький LRU в памяти;
  * отсутствие фото запоминается на сутки (negative cache);
  * одновременные запросы одной картинки схлопываются через lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import shutil
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

# Срок жизни ссылки на аватарку. Стабильный URL — основа браузерного кэша.
AVATAR_URL_TTL_SECONDS: Final = 7 * 24 * 60 * 60

_MAX_MEMORY_ENTRIES: Final = 128
_MAX_DISK_BYTES: Final = 200 * 1024 * 1024
_CLEANUP_TARGET_RATIO: Final = 0.8
_NEGATIVE_TTL_SECONDS: Final = 24 * 60 * 60
_NEGATIVE_MAX_ENTRIES: Final = 1024
_MAX_LOCKS: Final = 1024


def avatar_version(secret: str, telegram_id: int, file_id: str | None) -> str:
    """Короткая версия аватарки: меняется только вместе с ``file_id``."""
    payload = f"avatar-version:{int(telegram_id)}:{file_id or 'none'}"
    return hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:12]


def sniff_media_type(content: bytes) -> str:
    """MIME по сигнатуре файла, а не по расширению из Telegram."""
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


class AvatarCache:
    """Диск + память + negative cache + single-flight для аватарок."""

    def __init__(
        self,
        root: Path,
        *,
        memory_entries: int = _MAX_MEMORY_ENTRIES,
        max_disk_bytes: int = _MAX_DISK_BYTES,
        negative_ttl: float = _NEGATIVE_TTL_SECONDS,
        negative_entries: int = _NEGATIVE_MAX_ENTRIES,
    ) -> None:
        self.root = Path(root)
        self.memory_entries = max(1, int(memory_entries))
        self.max_disk_bytes = max(1, int(max_disk_bytes))
        self.negative_ttl = float(negative_ttl)
        self.negative_entries = max(1, int(negative_entries))
        self._memory: OrderedDict[tuple[int, str], tuple[bytes, str]] = OrderedDict()
        self._missing: OrderedDict[tuple[int, str], float] = OrderedDict()
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}
        self._disk_bytes = 0

    # ------------------------------------------------------------------ #
    # Память
    # ------------------------------------------------------------------ #

    def _memory_get(self, key: tuple[int, str]) -> tuple[bytes, str] | None:
        entry = self._memory.get(key)
        if entry is None:
            return None
        self._memory.move_to_end(key)
        return entry

    def _memory_put(self, key: tuple[int, str], content: bytes, media_type: str) -> None:
        self._memory[key] = (content, media_type)
        self._memory.move_to_end(key)
        while len(self._memory) > self.memory_entries:
            self._memory.popitem(last=False)

    # ------------------------------------------------------------------ #
    # Negative cache
    # ------------------------------------------------------------------ #

    def mark_missing(self, telegram_id: int, version: str) -> None:
        """Запоминает, что фото нет (на ``negative_ttl``, привязано к версии)."""
        key = (int(telegram_id), version)
        self._missing[key] = time.monotonic()
        self._missing.move_to_end(key)
        while len(self._missing) > self.negative_entries:
            self._missing.popitem(last=False)

    def is_missing(self, telegram_id: int, version: str) -> bool:
        key = (int(telegram_id), version)
        marked_at = self._missing.get(key)
        if marked_at is None:
            return False
        if time.monotonic() - marked_at >= self.negative_ttl:
            self._missing.pop(key, None)
            return False
        return True

    def clear_missing(self, telegram_id: int) -> None:
        key_id = int(telegram_id)
        for key in [k for k in self._missing if k[0] == key_id]:
            self._missing.pop(key, None)

    # ------------------------------------------------------------------ #
    # Single-flight
    # ------------------------------------------------------------------ #

    @asynccontextmanager
    async def single_flight(self, telegram_id: int, version: str) -> AsyncIterator[None]:
        """Не даёт параллельным запросам качать одну и ту же картинку."""
        key = (int(telegram_id), version)
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        try:
            async with lock:
                yield
        finally:
            self._prune_locks()

    def _prune_locks(self) -> None:
        if len(self._locks) <= _MAX_LOCKS:
            return
        for key in [k for k, lock in self._locks.items() if not lock.locked()]:
            self._locks.pop(key, None)

    # ------------------------------------------------------------------ #
    # Диск
    # ------------------------------------------------------------------ #

    def path(self, telegram_id: int, version: str) -> Path:
        return self.root / str(int(telegram_id)) / f"{version}.img"

    async def load_disk_usage(self) -> None:
        """Считает занятое место при старте и убирает лишнее после рестарта."""
        self._disk_bytes = await asyncio.to_thread(self._disk_usage)
        if self._disk_bytes > self.max_disk_bytes:
            self._disk_bytes = await asyncio.to_thread(self._cleanup)

    async def get(self, telegram_id: int, version: str) -> tuple[bytes, str] | None:
        """Возвращает ``(content, media_type)`` из памяти или с диска."""
        key = (int(telegram_id), version)
        cached = self._memory_get(key)
        if cached is not None:
            return cached
        content = await asyncio.to_thread(self._read_file, self.path(*key))
        if content is None:
            return None
        result = (content, sniff_media_type(content))
        self._memory_put(key, *result)
        return result

    @staticmethod
    def _read_file(path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    async def put(
        self,
        telegram_id: int,
        version: str,
        content: bytes,
        media_type: str | None = None,
    ) -> None:
        """Пишет картинку на диск (атомарно) и в память."""
        key = (int(telegram_id), version)
        resolved_type = media_type or sniff_media_type(content)
        self._memory_put(key, content, resolved_type)
        self.clear_missing(telegram_id)
        await asyncio.to_thread(self._write_file, self.path(*key), content)
        self._disk_bytes += len(content)
        if self._disk_bytes > self.max_disk_bytes:
            self._disk_bytes = await asyncio.to_thread(self._cleanup)

    def _write_file(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_bytes(content)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)

    def _disk_usage(self) -> int:
        total = 0
        for path in self.root.rglob("*.img"):
            with contextlib.suppress(OSError):
                total += path.stat().st_size
        return total

    def _cleanup(self) -> int:
        """Удаляет самые старые файлы, пока кэш не станет 80% от лимита."""
        files: list[tuple[float, int, Path]] = []
        total = 0
        for path in self.root.rglob("*.img"):
            with contextlib.suppress(OSError):
                stat = path.stat()
                files.append((stat.st_mtime, stat.st_size, path))
                total += stat.st_size

        target = int(self.max_disk_bytes * _CLEANUP_TARGET_RATIO)
        if total > target:
            for _, size, path in sorted(files):
                with contextlib.suppress(OSError):
                    path.unlink()
                    total -= size
                if total <= target:
                    break
        self._prune_empty_dirs()
        return total

    def _prune_empty_dirs(self) -> None:
        for directory in sorted(self.root.glob("*"), reverse=True):
            if not directory.is_dir():
                continue
            with contextlib.suppress(OSError):
                directory.rmdir()

    async def delete_user(self, telegram_id: int) -> None:
        """Удаляет все версии аватарки пользователя (``DELETE /api/me``)."""
        key_id = int(telegram_id)
        for key in [k for k in self._memory if k[0] == key_id]:
            self._memory.pop(key, None)
        self.clear_missing(key_id)
        for key in [k for k in self._locks if k[0] == key_id]:
            if not self._locks[key].locked():
                self._locks.pop(key, None)
        removed = await asyncio.to_thread(self._remove_dir, key_id)
        self._disk_bytes = max(0, self._disk_bytes - removed)

    def _remove_dir(self, telegram_id: int) -> int:
        directory = self.root / str(int(telegram_id))
        total = 0
        if directory.exists():
            for path in directory.rglob("*"):
                with contextlib.suppress(OSError):
                    if path.is_file():
                        total += path.stat().st_size
            shutil.rmtree(directory, ignore_errors=True)
        return total
