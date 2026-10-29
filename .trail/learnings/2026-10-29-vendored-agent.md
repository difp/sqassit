---
title: "Встроенный турнир: состояние по комнате и патчи vendored-сборки"
author: agent
date: 2026-10-29
branch: main
id: l-b3c315fe
scope: repo
status: active
src: agent
touches:
  - "app/main.py#api_training_room"
  - "app/main.py#api_match_room"
  - app/templates/base.html
---

## Decided
В 2.10.0 сборка squashtabs лежит в `app/static/squashtabs/index.html` и
патчится точечно: `role`/`names` из hash и ключ localStorage на каждую
комнату (`lsKey`). Турнир открывается в iframe `/tabs` с нашего origin.

## Why
Первый же тест показал: состояние хранилось под одним ключом, и в турнире
матча (4 игрока) оказался состав занятия (2 игрока) — данные прошлой
комнаты. Ключ по `sync.room` разводит комнаты, а `names` заполняют состав
только в пустую комнату, поэтому свой прежний состав не затирается.

## Watch out
- При обновлении index.html с difp.github.io патчи надо перенести: искать
  комментарии «SQASSIT» (roleLocked, names, lsKey). Если их нет — тест
  test_coach_opens_tournament_room упадёт.
- `role=viewer|judge` — это UX-замок, а не авторизация: hash можно
  отредактировать. Настоящая защита потребует проверки токена на сервере.
- Комнаты синхронизируются через публичный ntfy.sh: имена участников уходят
  в топик, код комнаты — единственный секрет.
