"""Простой in-memory rate limiter для чувствительных эндпоинтов.

Один процесс — один limiter (проект сознательно работает в один воркер:
внутри процесса живут polling и in-memory-состояние). Для нескольких
воркеров нужен внешний limiter (Redis/nginx).
"""

from __future__ import annotations

import time
from collections import deque

from fastapi import HTTPException


class RateLimiter:
    """Скользящее окно: не более ``limit`` событий за ``window_seconds``."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = max(1, int(limit))
        self.window_seconds = max(1.0, float(window_seconds))
        self._events: dict[str, deque[float]] = {}

    def check(self, key: str) -> None:
        now = time.monotonic()
        events = self._events.setdefault(key, deque())

        # Удаляем события за пределами окна.
        while events and now - events[0] >= self.window_seconds:
            events.popleft()

        if len(events) >= self.limit:
            retry_after = max(1, int(self.window_seconds - (now - events[0])))
            raise HTTPException(
                status_code=429,
                detail="Слишком много запросов. Попробуйте позже.",
                headers={"Retry-After": str(retry_after)},
            )
        events.append(now)

        # Не даём словарю расти бесконечно при большом числе ключей.
        if len(self._events) > 10_000:
            for stale_key, stale_events in list(self._events.items()):
                if not stale_events or now - stale_events[-1] >= self.window_seconds:
                    self._events.pop(stale_key, None)

    def clear(self) -> None:
        """Сбросить состояние (используется в тестах)."""
        self._events.clear()
