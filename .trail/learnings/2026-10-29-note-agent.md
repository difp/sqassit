---
title: "Код комнаты турнира: случайный на сервере, не из адреса приложения"
author: agent
date: 2026-10-29
branch: main
id: l-56827a5b
scope: repo
status: active
src: agent
touches:
  - "app/main.py#api_training_room"
  - "app/database.py#set_training_room"
  - app/templates/coach.html
---

## Decided
Код комнаты squashtabs живёт в `trainings.room_code`, генерируется на сервере
(`SQ-` + 8 знаков, около 40 бит) и выдаётся только создателю занятия и
соавторам через `POST /api/coach/trainings/{id}/room`.

## Why
Топик ntfy публичный: кто знает код, тот читает состав и может подменить счёт.
Поэтому код нельзя выводить из адреса приложения или id тренировки — такой код
угадывается за несколько попыток, а 40 бит случайности и выдача только
управляющим закрывают эту дыру.

## Watch out
- Код не кладём в `serialize_training`: игрок получает расписание через
  `/api/trainings`, и код в нём был бы виден всем.
- squashtabs чистит код до `A-Z0-9-` и обрезает до 20 знаков; ссылка идёт
  через `telegram.openLink` с кодом в `#room=`, поэтому хэш не уходит
  на GitHub Pages.
- Повторный тап должен возвращать тот же код: `set_training_room` пишет
  только при `room_code IS NULL`, иначе два судьи получат разные комнаты.
