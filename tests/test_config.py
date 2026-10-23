"""Тесты валидации конфигурации (в первую очередь webhook)."""

from __future__ import annotations

import pytest

from app.config import _require, load_settings, validate_webhook_settings


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


def test_require_rejects_missing_value(monkeypatch):
    monkeypatch.delenv("TEST_REQUIRED_VALUE", raising=False)
    with pytest.raises(RuntimeError, match="TEST_REQUIRED_VALUE"):
        _require("TEST_REQUIRED_VALUE")


def test_load_settings_rejects_non_numeric_coach(monkeypatch):
    monkeypatch.setenv("COACH_TELEGRAM_ID", "not-a-number")
    with pytest.raises(RuntimeError, match="COACH_TELEGRAM_ID"):
        load_settings()


def test_load_settings_rejects_unknown_timezone(monkeypatch):
    monkeypatch.setenv("COACH_TELEGRAM_ID", "123")
    monkeypatch.setenv("TIMEZONE", "Nowhere/Unknown")
    with pytest.raises(RuntimeError, match="TIMEZONE"):
        load_settings()


def test_load_settings_rejects_unknown_bot_mode(monkeypatch):
    monkeypatch.setenv("COACH_TELEGRAM_ID", "123")
    monkeypatch.setenv("TIMEZONE", "Europe/Moscow")
    monkeypatch.setenv("BOT_MODE", "carrier-pigeon")
    with pytest.raises(RuntimeError, match="BOT_MODE"):
        load_settings()


def test_load_settings_rejects_bad_phone_cutoff(monkeypatch):
    monkeypatch.setenv("COACH_TELEGRAM_ID", "123")
    monkeypatch.setenv("TIMEZONE", "Europe/Moscow")
    monkeypatch.setenv("PHONE_REQUIRED_FROM", "not-a-date")
    with pytest.raises(RuntimeError, match="PHONE_REQUIRED_FROM"):
        load_settings()


def test_load_settings_accepts_naive_phone_cutoff(monkeypatch):
    monkeypatch.setenv("COACH_TELEGRAM_ID", "123")
    monkeypatch.setenv("TIMEZONE", "Europe/Moscow")
    monkeypatch.setenv("PHONE_REQUIRED_FROM", "2026-10-24T00:00:00")
    loaded = load_settings()
    assert loaded.phone_required_from is not None
    assert loaded.phone_required_from.tzinfo is not None
