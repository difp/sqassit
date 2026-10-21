"""Статические guardrails для общей сетки и стартовой позиции страницы."""

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"
BASE = TEMPLATES / "base.html"


def test_base_layout_guardrails() -> None:
    html = BASE.read_text()
    head = html.split("</head>", 1)[0]

    # Позиция скролла отключается до того, как WebView успеет её восстановить.
    assert "history.scrollRestoration = 'manual'" in head
    assert "scrollAppToTop()" in html

    # Контент никогда не шире окна, а шапка не раздвигается длинным заголовком.
    assert "overflow-x: clip" in html
    assert "width: 100%; max-width: 520px" in html
    assert ".app-title {" in html
    assert "text-overflow: ellipsis" in html
    assert ".app-header__right { flex: 0 0 auto" in html

    # Формы и сетки не создают горизонтальный скролл.
    assert ".row2 > * { min-width: 0; }" in html
    assert "max-width: 100%" in html


def test_disclosures_use_aligned_dots() -> None:
    base = BASE.read_text()
    coach = (TEMPLATES / "coach.html").read_text()

    assert ".disclosure > summary" in base
    assert "::-webkit-details-marker" in base
    assert "border-radius: 50%" in base
    assert '<summary style=' not in coach
    for tag in re.findall(r"<details[^>]*>", coach):
        assert 'class="disclosure"' in tag
