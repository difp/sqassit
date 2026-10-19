"""Тесты валидации конфигурации (в первую очередь webhook)."""

from __future__ import annotations

import pytest

from app.config import validate_webhook_settings


def test_polling_does_not_require_webhook_settings():
    validate_webhook_settings("polling", "", "")


def test_webhook_requires_base_url():
    with pytest.raises(RuntimeError, match="WEBHOOK_BASE_URL"):
        validate_webhook_settings("webhook", "", "a" * 32)


def test_webhook_requires_secret():
    with pytest.raises(RuntimeError, match="WEBHOOK_SECRET"):
        validate_webhook_settings("webhook", "https://example.com", "")


def test_webhook_rejects_placeholder_and_short_secret():
    with pytest.raises(RuntimeError, match="слабый"):
        validate_webhook_settings(
            "webhook", "https://example.com", "change-me-to-random-string"
        )
    with pytest.raises(RuntimeError, match="слабый"):
        validate_webhook_settings("webhook", "https://example.com", "short")


def test_webhook_accepts_strong_secret():
    validate_webhook_settings("webhook", "https://example.com", "a" * 32)
    validate_webhook_settings("webhook", "https://example.com", "f" * 64)
