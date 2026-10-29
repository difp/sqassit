---
title: "Стенд мини-приложения: initData в URL и тема вне Telegram"
author: agent
date: 2026-10-29
branch: main
id: l-f1b674b3
scope: repo
status: active
src: agent
touches:
  - app/templates/base.html
---

Стенд /tmp/opencode/stand поднимает настоящее приложение (uvicorn + serve.py), но два
места ловят сюрпризом и стоили времени.

1. initData в адресной строке нужно кодировать целиком (quote(init, safe='')).
   Строка initData содержит «&», поэтому вставка как есть обрезает её до
   auth_date=..., сервер отвечает 401, а страница показывает «Telegram не
   передал данные авторизации». Симптом выглядит как поломка авторизации,
   а не как ошибка сборки ссылки.

2. Вне Telegram /static/telegram-web-app.js всё равно определяет
   window.Telegram.WebApp и отдаёт colorScheme = 'light'. Фолбэк на
   prefers-color-scheme не срабатывает, поэтому тёмную тему на стенде
   выставляют вручную: documentElement.dataset.theme = 'dark' после загрузки.
   CSS переключается только по data-theme.

3. Смотреть вёрстку в iframe нельзя: CSP приложения разрешает frame-ancestors
   только доменам Telegram. Для проверки ширин — Playwright (chromium
   headless) с viewport 376/545 и device_scale_factor 2/1.

Проверять нужно обе плотности: на DPR 2.6 телефон и DPR 1 десктоп по-разному
сглаживают зазоры (см. l-8a47c7df про точку у «Участники»).
