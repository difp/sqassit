"""Guardrails for Mini App recovery after temporary network failures."""

from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "app" / "templates" / "base.html"


def test_get_requests_retry_but_mutations_do_not() -> None:
    html = BASE.read_text()

    assert "const GET_RETRY_DELAYS_MS = [0, 900];" in html
    assert "const retryDelays = method.toUpperCase() === 'GET' ? GET_RETRY_DELAYS_MS : [0];" in html
    assert "isRetryableRequestError" in html
    # Две попытки по 6 с: пользователь не ждёт 45 секунд молчаливого экрана.
    assert "setTimeout(() => controller.abort(), 6000)" in html


def test_connection_error_can_recover_without_webview_reload() -> None:
    html = BASE.read_text()

    assert "Нет соединения с приложением" in html
    assert "retryAppConnection" in html
    assert "window.addEventListener('online', recoverConnection);" in html
    assert "document.addEventListener('visibilitychange'" in html
    assert "window.PAGE_REFRESH" in html
    assert 'id="app-auth-reload"' in html


def test_stuck_bfcache_page_reloads_once() -> None:
    html = BASE.read_text()

    # Перезагрузка из bfcache давала лишний пустой экран; вместо неё
    # стартовый экран снимается watchdog'ом через 4 секунды.
    assert "event.persisted" not in html
    assert "setTimeout(hideAppLoader, 4000);" in html
