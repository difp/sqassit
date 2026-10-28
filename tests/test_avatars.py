"""Юнит-тесты диск-кэша аватарок (app/avatars.py)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from app import avatars
from app.avatars import AvatarCache, avatar_version, sniff_media_type


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Версия URL и MIME
# --------------------------------------------------------------------------- #

def test_avatar_version_is_stable_and_depends_on_file_id():
    first = avatar_version("secret", 555, "file-1")
    assert first == avatar_version("secret", 555, "file-1")
    assert len(first) == 12

    assert first != avatar_version("secret", 555, "file-2")
    assert first != avatar_version("secret", 556, "file-1")
    assert first != avatar_version("other", 555, "file-1")
    # Нет фото — тоже стабильная версия, но другая.
    assert avatar_version("secret", 555, None) == avatar_version("secret", 555, None)
    assert avatar_version("secret", 555, None) != first


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"\xff\xd8\xff\xe0jpeg", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\nrest", "image/png"),
        (b"GIF87a....", "image/gif"),
        (b"GIF89a....", "image/gif"),
        (b"RIFF1234WEBPdata", "image/webp"),
        (b"unknown", "application/octet-stream"),
    ],
)
def test_sniff_media_type(content, expected):
    assert sniff_media_type(content) == expected


# --------------------------------------------------------------------------- #
# Память и диск
# --------------------------------------------------------------------------- #

def test_memory_lru_evicts_least_recently_used(tmp_path, monkeypatch):
    cache = AvatarCache(tmp_path, memory_entries=2)
    # Диск отключаем: проверяем именно вытеснение из памяти.
    monkeypatch.setattr(cache, "_write_file", lambda path, content: None)

    run(cache.put(1, "v", b"first"))
    run(cache.put(2, "v", b"second"))
    assert run(cache.get(1, "v")) == (b"first", "application/octet-stream")
    run(cache.put(3, "v", b"third"))

    assert run(cache.get(2, "v")) is None  # вытеснена как самая старая
    assert run(cache.get(1, "v")) == (b"first", "application/octet-stream")
    assert run(cache.get(3, "v")) == (b"third", "application/octet-stream")


def test_disk_roundtrip_and_missing(tmp_path):
    cache = AvatarCache(tmp_path)
    png = b"\x89PNG\r\n\x1a\npayload"
    run(cache.put(7, "abc", png))

    stored = cache.path(7, "abc")
    assert stored.exists()
    assert stored.parent.name == "7"
    assert stored.name == "abc.img"

    # Новый экземпляр = рестарт приложения: память пуста, читаем с диска.
    fresh = AvatarCache(tmp_path)
    assert run(fresh.get(7, "abc")) == (png, "image/png")
    assert run(fresh.get(7, "other")) is None


def test_put_with_explicit_media_type(tmp_path):
    cache = AvatarCache(tmp_path)
    run(cache.put(2, "v", b"data", "image/webp"))
    assert run(cache.get(2, "v")) == (b"data", "image/webp")


def test_put_write_error_does_not_leave_tmp(tmp_path):
    cache = AvatarCache(tmp_path)
    (tmp_path / "9").write_bytes(b"not a directory")
    with pytest.raises(OSError):
        run(cache.put(9, "v", b"data"))
    assert not list(tmp_path.rglob("*.tmp"))


# --------------------------------------------------------------------------- #
# Negative cache
# --------------------------------------------------------------------------- #

def test_negative_cache_ttl_and_capacity(tmp_path, monkeypatch):
    cache = AvatarCache(tmp_path, negative_ttl=10, negative_entries=1)

    cache.mark_missing(1, "v")
    assert cache.is_missing(1, "v") is True
    assert cache.is_missing(1, "other-version") is False

    # Лимит записей: вторая вытесняет первую.
    cache.mark_missing(2, "v")
    assert cache.is_missing(1, "v") is False
    assert cache.is_missing(2, "v") is True

    monkeypatch.setattr(avatars.time, "monotonic", lambda: 100.0)
    cache.mark_missing(3, "v")
    assert cache.is_missing(3, "v") is True
    monkeypatch.setattr(avatars.time, "monotonic", lambda: 111.0)
    assert cache.is_missing(3, "v") is False


def test_clear_missing_removes_all_versions(tmp_path):
    cache = AvatarCache(tmp_path)
    cache.mark_missing(5, "a")
    cache.mark_missing(5, "b")
    cache.clear_missing(5)
    assert cache.is_missing(5, "a") is False
    assert cache.is_missing(5, "b") is False


# --------------------------------------------------------------------------- #
# Single-flight
# --------------------------------------------------------------------------- #

def test_single_flight_serializes_same_key(tmp_path):
    cache = AvatarCache(tmp_path)
    order: list[str] = []

    async def worker(tag: str) -> None:
        async with cache.single_flight(1, "v"):
            order.append(f"start-{tag}")
            await asyncio.sleep(0)
            order.append(f"end-{tag}")

    async def scenario() -> None:
        await asyncio.gather(worker("a"), worker("b"))

    run(scenario())
    assert order in (
        ["start-a", "end-a", "start-b", "end-b"],
        ["start-b", "end-b", "start-a", "end-a"],
    )


def test_locks_are_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr(avatars, "_MAX_LOCKS", 1)
    cache = AvatarCache(tmp_path)

    async def scenario() -> None:
        async with cache.single_flight(1, "a"):
            pass
        async with cache.single_flight(2, "b"):
            pass

    run(scenario())
    assert len(cache._locks) <= 1


# --------------------------------------------------------------------------- #
# Лимит диска и уборка
# --------------------------------------------------------------------------- #

def test_load_disk_usage_counts_previous_files_and_cleans(tmp_path):
    """После рестарта лимит должен учитывать файлы прошлых запусков."""
    old = tmp_path / "1" / "old.img"
    old.parent.mkdir()
    old.write_bytes(b"a" * 6)
    os.utime(old, (1000, 1000))
    new = tmp_path / "2" / "new.img"
    new.parent.mkdir()
    new.write_bytes(b"b" * 6)

    cache = AvatarCache(tmp_path, max_disk_bytes=20)
    run(cache.load_disk_usage())
    assert cache._disk_bytes == 12

    cache = AvatarCache(tmp_path, max_disk_bytes=10)
    run(cache.load_disk_usage())
    assert cache._disk_bytes == 6
    assert not old.exists()  # самый старый удалён
    assert new.exists()


def test_cleanup_removes_oldest_and_empty_dirs(tmp_path):
    cache = AvatarCache(tmp_path, max_disk_bytes=10)
    run(cache.put(1, "old", b"a" * 6))
    os.utime(cache.path(1, "old"), (1000, 1000))
    run(cache.put(2, "new", b"b" * 6))  # 12 > 10 -> уборка до 8 байт

    files = list(tmp_path.rglob("*.img"))
    assert len(files) == 1
    assert cache.path(2, "new").exists()
    assert not cache.path(1, "old").exists()
    assert not (tmp_path / "1").exists()  # пустая папка тоже удалена


def test_cleanup_survives_vanished_and_duplicate_files(tmp_path, monkeypatch):
    cache = AvatarCache(tmp_path, max_disk_bytes=10)
    run(cache.put(3, "v", b"x" * 10))

    real = cache.path(3, "v")
    gone = tmp_path / "gone.img"
    monkeypatch.setattr(
        Path, "rglob", lambda self, pattern: iter([gone, real, real])
    )

    total = cache._cleanup()
    assert total == 10
    assert not real.exists()


def test_prune_empty_dirs_skips_files_and_non_empty_dirs(tmp_path):
    (tmp_path / "keep").mkdir()
    (tmp_path / "keep" / "file.txt").write_text("x", encoding="utf-8")
    (tmp_path / "plain.txt").write_text("x", encoding="utf-8")

    AvatarCache(tmp_path)._prune_empty_dirs()

    assert (tmp_path / "keep").exists()
    assert (tmp_path / "plain.txt").exists()


def test_delete_user_removes_memory_disk_and_negative(tmp_path):
    cache = AvatarCache(tmp_path)
    run(cache.put(5, "a", b"one"))
    run(cache.put(5, "b", b"two"))
    cache.mark_missing(5, "c")

    run(cache.delete_user(5))
    assert run(cache.get(5, "a")) is None
    assert not (tmp_path / "5").exists()
    assert cache.is_missing(5, "c") is False

    # Повторное удаление — не ошибка.
    run(cache.delete_user(5))


def test_delete_user_keeps_locked_entry(tmp_path):
    cache = AvatarCache(tmp_path)
    run(cache.put(6, "v", b"data"))

    async def scenario() -> None:
        async with cache.single_flight(6, "v"):
            await cache.delete_user(6)
            # Занятый lock не выкидываем: иначе можно создать второй на тот же ключ.
            assert (6, "v") in cache._locks

    run(scenario())
    assert run(cache.get(6, "v")) is None
