---
title: "Кто видит @username: тренер всегда, игроки — по согласию"
author: agent
date: 2026-10-29
branch: main
id: l-cb62756f
scope: repo
status: active
src: agent
touches:
  - "app/main.py#username_for_viewer"
  - app/templates/coach.html
  - docs/PRIVACY.md
---

## Decided
Согласие «Показывать @username» управляет только показом другим игрокам.
Тренер видит username всегда: до 2.8.0 согласие скрывало ник и от него,
поэтому поиск по @username не находил игрока, а в CRM стояло «username скрыт».

## Watch out
- Режим «животное» скрывает имя и фото от всех, включая тренера, но username
  тренеру виден и там: иначе в анонимном режиме человека не найти для
  модерации. Это осознанное исключение.
- Новый эндпоинт, который отдаёт username, должен звать
  username_for_viewer(row, coach_view=...): повтор условия по show_username
  снова спрячет ник от тренера и сломает поиск.
- Поиск в database.list_players больше не фильтрует по show_username:
  это запрос только из панели тренера (require_coach).
