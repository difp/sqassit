"""Версия приложения для URL Mini App и версионирования статики."""

from __future__ import annotations

from pathlib import Path

from app import config


def test_app_version_prefers_build_sha(monkeypatch):
    monkeypatch.setenv("BUILD_SHA", "abc123def4567890")
    assert config._app_version() == "abc123def456"


def test_app_version_uses_version_file(monkeypatch):
    monkeypatch.delenv("BUILD_SHA", raising=False)
    expected = (
        Path(config.__file__).resolve().parent.parent / "VERSION"
    ).read_text(encoding="utf-8").strip()
    assert config._app_version() == expected


def test_app_version_falls_back_without_version_file(monkeypatch, tmp_path):
    monkeypatch.delenv("BUILD_SHA", raising=False)
    monkeypatch.setattr(config, "__file__", str(tmp_path / "app" / "config.py"))
    assert config._app_version() == "0"


def test_app_version_treats_empty_version_as_zero(monkeypatch, tmp_path):
    monkeypatch.delenv("BUILD_SHA", raising=False)
    (tmp_path / "VERSION").write_text("\n", encoding="utf-8")
    monkeypatch.setattr(config, "__file__", str(tmp_path / "app" / "config.py"))
    assert config._app_version() == "0"
