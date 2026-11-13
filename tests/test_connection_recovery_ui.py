"""Guardrails for Mini App recovery after temporary network failures."""

from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "app" / "templates" / "base.html"


def test_get_requests_retry_but_mutations_do_not() -> None:
    html = BASE.read_text()

    assert "const GET_RETRY_DELAYS_MS = [0, 1000, 2500];" in html
    assert "const retryDelays = method.toUpperCase() === 'GET' ? GET_RETRY_DELAYS_MS : [0];" in html
    assert "isRetryableRequestError" in html


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

    # Telegram iOS может восстановить WebView до окончания загрузки:
    # стартовый экран на месте — перезагружаем страницу.
    assert "event.persisted && document.getElementById('app-loader')" in html
