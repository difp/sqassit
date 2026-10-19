"""Тесты чистых helper-функций бота и rate limiter."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import HTTPException

from app import rate_limit as rate_limit_module
from app.bot import _chunk_telegram_messages, format_training_reminder
from app.config import settings
from app.rate_limit import RateLimiter


def test_reminder_text_escapes_html():
    training = {
        "start_at": int(datetime.now(settings.timezone).timestamp()),
        "training_type": "<b>взлом</b>",
        "capacity": 4,
    }
    text = format_training_reminder(training)
    assert "&lt;b&gt;взлом&lt;/b&gt;" in text
    assert "<b>взлом</b>" not in text


def test_inbox_chunks_stay_within_telegram_limit():
    blocks = [f"сообщение {i}\n" + "x" * 900 for i in range(10)]
    chunks = _chunk_telegram_messages(blocks, header="Заголовок")
    assert len(chunks) > 1
    assert all(len(chunk) <= 4000 for chunk in chunks)
    joined = "\n\n".join(chunks)
    for block in blocks:
        assert block in joined


def test_rate_limiter_blocks_and_recovers(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(rate_limit_module.time, "monotonic", lambda: now[0])
    limiter = RateLimiter(limit=2, window_seconds=10)

    limiter.check("user")
    limiter.check("user")
    with pytest.raises(HTTPException) as exc_info:
        limiter.check("user")
    assert exc_info.value.status_code == 429
    assert exc_info.value.headers["Retry-After"] == "10"

    now[0] += 11
    limiter.check("user")  # окно очистилось
