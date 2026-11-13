"""Guardrails for Mini App recovery after temporary network failures."""

from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "app" / "templates" / "base.html"
INDEX = Path(__file__).resolve().parents[1] / "app" / "templates" / "index.html"


def test_get_requests_retry_but_mutations_do_not() -> None:
    html = BASE.read_text()

    assert "const GET_RETRY_DELAYS_MS = [0, 900];" in html
    assert "const retryDelays = method.toUpperCase() === 'GET' ? GET_RETRY_DELAYS_MS : [0];" in html
    assert "isRetryableRequestError" in html
    # Фоновые запросы — 6 с, первичная загрузка — 12 с.
    assert "setTimeout(() => controller.abort(), timeoutMs)" in html
    assert "const REQUEST_TIMEOUT_MS = 6000;" in html


def test_connection_error_can_recover_without_webview_reload() -> None:
    html = BASE.read_text()

    assert "Нет соединения с приложением" in html
    assert "retryAppConnection" in html
    assert "window.addEventListener('online', recoverConnection);" in html
    assert "document.addEventListener('visibilitychange'" in html
    assert "window.PAGE_REFRESH" in html
    assert 'id="app-auth-reload"' in html


def test_bfcache_restore_refreshes_without_reload() -> None:
    html = BASE.read_text()

    # Перезагрузка из bfcache сохраняет старый initData и даёт пустой экран;
    # вместо неё перезапрашиваем профиль/данные.
    assert "if (!event.persisted || typeof window.PAGE_REFRESH !== 'function') return;" in html
    assert "APP.refresh()" in html
    assert "setTimeout(hideAppLoader, 4000);" in html


def test_expired_session_offers_reopen() -> None:
    html = BASE.read_text()

    assert 'id="app-auth-close"' in html
    assert "Сессия устарела" in html
    assert "tg.close()" in html
    assert "canReopen" in html


def test_session_token_is_kept_in_session_storage() -> None:
    html = BASE.read_text()

    assert "SESSION_TOKEN_KEY" in html
    assert "sessionStorage.setItem(SESSION_TOKEN_KEY" in html
    assert "headers['X-Session-Token'] = SESSION_TOKEN" in html
    assert "res.headers.get('X-Session-Token')" in html


def test_initial_requests_have_longer_timeout() -> None:
    html = BASE.read_text()

    assert "const REQUEST_TIMEOUT_MS = 6000;" in html
    assert "const INITIAL_REQUEST_TIMEOUT_MS = 12000;" in html
    assert "timeout: INITIAL_REQUEST_TIMEOUT_MS" in html


def test_telegram_script_is_versioned() -> None:
    html = BASE.read_text()

    assert "/static/telegram-web-app.js?v={{ asset_version }}" in html


def test_background_refresh_does_not_supersede_initial_schedule() -> None:
    html = INDEX.read_text()

    # Иначе ответ первичной загрузки выбрасывается, а ошибка фонового
    # запроса молча скрывается — остаётся пустой экран без объяснения.
    assert "if (background && (scheduleLoading || !initialScheduleDone)) return;" in html
    assert "showScheduleError" in html
    assert 'id="schedule-error"' in html
