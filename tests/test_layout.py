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
    assert "flex-wrap: wrap; gap: 6px 12px" in html
    assert "::-webkit-inner-spin-button" in html
    assert ".t-toolbar .t-meta { flex: 1 1 180px" in html


def test_disclosures_use_aligned_dots() -> None:
    base = BASE.read_text()
    coach = (TEMPLATES / "coach.html").read_text()

    assert ".disclosure > summary" in base
    assert "::-webkit-details-marker" in base
    # Точка нарисована псевдоэлементом и прижата к подписи: отдельного
    # элемента слева от текста нет, поэтому она не выглядит сама по себе.
    # Отступ задан в em, а не в px: при DPR 1 фиксированные 3 px схлопывались
    # и точка слипалась с первой буквой.
    assert "summary::before" in base
    assert ".disclosure[open] > summary::before" in base
    assert 'content: "\\2022"' in base
    assert "font-size: 1.35em" in base
    assert "margin-right: .18em" in base
    assert ".disclosure__dot" not in base
    assert "disclosure__dot" not in coach
    assert '<summary style=' not in coach
    for tag in re.findall(r"<details[^>]*>", coach):
        assert 'class="disclosure"' in tag


def test_coach_participants_open_by_default() -> None:
    coach = (TEMPLATES / "coach.html").read_text()

    # Своё и чужое занятие: список участников раскрыт сразу после загрузки.
    assert coach.count('data-pid="${t.id}" open') == 2
    assert "hydrateParticipants();" in coach
    # Заявки видны на карточке, а не только внутри раскрытого списка.
    assert coach.count('badge badge--warn">Заявки: ${pending}') == 2


def test_coach_tab_has_pending_badge() -> None:
    base = BASE.read_text()
    coach = (TEMPLATES / "coach.html").read_text()

    assert 'id="tab-coach-pin"' in base
    assert ".tab__pin" in base
    assert "width: 9px; height: 9px" in base
    assert "setCoachIndicator" in base
    assert "refreshCoachIndicator" in base
    assert "window.setCoachIndicator(" in coach


def test_coach_players_search_always_visible_list_collapsed() -> None:
    coach = (TEMPLATES / "coach.html").read_text()
    base = BASE.read_text()

    # Полный список свёрнут при загрузке: тело скрыто, кнопка сообщает состояние.
    body = re.search(r'<div[^>]*id="players-body"[^>]*>', coach)
    assert body is not None
    assert "hidden" in body.group(0)
    toggle = re.search(r'<button[^>]*id="players-toggle"[^>]*>', coach)
    assert toggle is not None
    assert 'aria-expanded="false"' in toggle.group(0)
    assert 'aria-controls="players-body"' in toggle.group(0)
    assert 'type="button"' in toggle.group(0)

    # Поиск живёт вне сворачиваемого тела, поэтому виден всегда.
    search = coach.index('id="players-search"')
    body_start = coach.index('id="players-body"')
    assert search < body_start
    assert 'type="search"' in coach
    assert 'enterkeyhint="search"' in coach
    assert "autofocus" not in coach

    # Живой регион существует до обновления текста и не монтируется с ним.
    status = re.search(r'<p[^>]*id="players-status"[^>]*>', coach)
    assert status is not None
    assert 'role="status"' in status.group(0)
    assert "hidden" not in status.group(0)

    # Раскрытие обозначено шевроном, а не только цветом/точкой.
    assert "players-toggle__chevron" in coach
    assert "playersToggle.setAttribute('aria-expanded'" in coach

    # Ленивая загрузка: счётчик сразу, карточки — при раскрытии.
    assert 'id="players-count"' in coach
    assert "loadPlayersCount()" in coach
    assert "/api/coach/players/count" in coach
    assert "/api/coach/players?q=" in coach
    assert "playersBody.hidden = !PLAYERS.open" in coach
    # Ввод в поиске раскрывает список, сворачивание сбрасывает запрос.
    assert "if (next) setPlayersOpen(true);" in coach
    assert "resetPlayersQuery();" in coach

    # Стили секции живут в общей таблице стилей.
    assert "#players-section > .section-title" in base
    assert ".players-toggle__chevron" in base
    assert ".players-toggle__count" in base


def test_coach_can_book_player_manually() -> None:
    coach = (TEMPLATES / "coach.html").read_text()
    base = BASE.read_text()

    # Действие живёт под списком участников занятия, а не в шапке страницы.
    assert "bookButton(id)" in coach
    assert 'class="btn btn--sm btn--block book-add"' in coach
    assert "data-book=" in coach
    assert ".book-add" in base

    # Шторка: поиск по базе на виду, результат — строки-кнопки.
    sheet = re.search(
        r'<div class="sheet" id="book-sheet">(.*?)\n</div>\n\{% endblock', coach, re.DOTALL
    )
    assert sheet is not None
    block = sheet.group(1)
    search = re.search(r'<input[^>]*id="book-search"[^>]*>', block)
    assert search is not None
    assert 'type="search"' in search.group(0)
    assert 'enterkeyhint="search"' in search.group(0)
    assert 'aria-label="Найти игрока в базе"' in search.group(0)
    assert 'id="book-status"' in block and 'role="status"' in block
    assert 'aria-labelledby="book-sheet-title"' in block

    # Подпись говорит, есть ли места: очередь не должна быть сюрпризом.
    assert "Мест нет — игрок встанет в очередь" in coach
    assert "bookSubtitle(t)" in coach

    # Уже записанные не выбираются повторно.
    assert "BOOK.taken" in coach
    assert 'aria-disabled="true"' in coach
    assert "Игрок уже записан на это занятие" in coach

    # Запись идёт в тот же endpoint участников; список и счётчики обновляются
    # с сохранением позиции прокрутки.
    assert "`/api/coach/trainings/${id}/participants`, 'POST'" in coach
    assert "user_id: uid" in coach
    assert "closeBookSheet();" in coach
    assert "await refreshKeepingPlace();" in coach

    # Стили строки поиска игрока.
    assert ".book-row" in base
    assert ".book-row__action" in base
    assert ".book-sheet__sub" in base


def test_sheets_do_not_shift_the_page() -> None:
    base = BASE.read_text()
    coach = (TEMPLATES / "coach.html").read_text()

    # Открытая шторка блокирует прокрутку фона: иначе палец на затемнении
    # уводит страницу, и после закрытия карточка оказывается не на месте.
    assert "html.has-sheet, html.has-sheet body { overflow: hidden; }" in base
    assert "MutationObserver(syncSheetScrollLock)" in base
    assert "document.querySelectorAll('.sheet')" in base
    assert "touch-action: none" in base
    # Позицию запоминаем и возвращаем: iOS сбрасывает её при смене overflow.
    assert "window.scrollTo(0, SHEET_SCROLL_Y)" in base

    # Шторка ограничена видимым вьюпортом, а не 100vh с панелью браузера.
    assert "max-height: 92dvh" in base

    # На телефоне автфокус поднимает клавиатуру и список прыгает.
    assert "bookSearch.focus({ preventScroll: true })" in coach
    assert "matchMedia('(pointer: fine)')" in coach

    # После записи возвращаем страницу туда, где открыли шторку.
    assert "async function refreshKeepingPlace()" in coach
    assert "captureListAnchor()" in coach
    assert "restoreListAnchor(anchor)" in coach
    assert "await loadView();\n  await HYDRATION;\n  restoreListAnchor(anchor);" in coach

    # Любое действие по участникам сохраняет место, а не только запись:
    # «Снять»/«Одобрить»/«Отклонить» перерисовывают список так же.
    for action in ("premove", "papprove", "preject", "prestore", "del", "purge"):
        assert f"if ({action}" in coach or f"closest('[data-{action}]')" in coach
    assert coach.count("await refreshKeepingPlace();") >= 6
    assert "refreshParticipants(tid);\n      cacheClear();\n      loadView();" not in coach

    # Позиция возвращается уже после догрузки участников: иначе высота
    # карточек меняется после восстановления и страница снова уезжает.
    assert "HYDRATION = hydrateParticipants();" in coach
    assert "await Promise.allSettled(jobs);" in coach


def test_safe_areas_and_text_scale_are_pinned() -> None:
    base = BASE.read_text()

    # Полноэкранный режим Telegram отдаёт свои вставки, env() их не знает.
    assert "--tg-safe-area-inset-top" in base
    assert "--tg-safe-area-inset-bottom" in base
    assert "calc(var(--safe-top) + 12px)" in base
    assert "calc(var(--tabbar-h) + 26px + var(--safe-bottom))" in base

    # Старый WebView без env()/max() не должен обнулять отступы шапки:
    # сначала простое значение, расширение — только под @supports.
    assert "--safe-top: 0px;" in base
    assert "--safe-top: env(safe-area-inset-top, 0px);" in base
    assert "@supports (width: max(0px, 0px))" in base

    # Android WebView не должен сам увеличивать шрифт: иначе строки ломаются
    # иначе, чем на стенде.
    assert "-webkit-text-size-adjust: 100%" in base
    assert "text-size-adjust: 100%" in base


def test_page_stays_at_top_while_telegram_expands() -> None:
    base = BASE.read_text()

    # Раскрытие WebView в Telegram меняет высоту после первой отрисовки;
    # без этого страница открывается прокрученной, без верха карточки.
    assert "function pinTopWhileOpening()" in base
    assert "tg.onEvent('viewportChanged', pinTopWhileOpening)" in base
    assert "let USER_SCROLLED = false;" in base
    assert "[0, 120, 300, 700].forEach((ms) => setTimeout(pinTopWhileOpening, ms));" in base

    # Автоматическое «удержание якоря» Chrome конфликтует с ручным возвратом
    # прокрутки после перерисовки списка.
    assert "overflow-anchor: none;" in base
