# 🏸 Squash-Bot

Telegram Web App для сквош-клуба: расписание, запись в один тап, лист ожидания,
несколько тренеров и CRM — без отдельного мобильного приложения.

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.146-009688?logo=fastapi&logoColor=white)
![aiogram](https://img.shields.io/badge/aiogram-3.31-2CA5E0)
![SQLite](https://img.shields.io/badge/SQLite-WAL-003B57?logo=sqlite&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

<p align="center">
  <img src="docs/screenshots/player.png" alt="Расписание игрока" width="44%" style="vertical-align:top">
  <img src="docs/screenshots/coach.png" alt="Панель тренера" width="44%" style="vertical-align:top">
</p>
<p align="center"><sub>Игрок: расписание и запись · Тренер: занятия, очередь и напоминания</sub></p>

## Возможности

- **Расписание на 7 дней** и запись в один тап прямо в Telegram.
- **Лист ожидания FIFO** с автоподъёмом при отмене и push-уведомлениями.
- **Несколько тренеров**: заявки, роли, «Клуб» только для чтения, передача занятий.
- **Напоминания** с настраиваемым смещением, экспорт в `.ics` и Google Календарь.
- **CRM**: заметки по игрокам и личные сообщения тренеру через бота.
- **История и корзина**: мягкое удаление, восстановление, безвозвратная очистка.

## Быстрый старт

```bash
git clone https://github.com/difp/sqassit.git
cd sqassit

make install
cp .env.example .env
# заполните BOT_TOKEN, COACH_TELEGRAM_ID и WEBHOOK_BASE_URL

mkdir -p ./data
make run          # http://127.0.0.1:8000
```

Для production: `make install-prod`, systemd-сервис с `uvicorn` и HTTPS
reverse-proxy — см. [`docs/DEPLOY.md`](docs/DEPLOY.md). На Ubuntu 22.04 нужен
Python 3.11+ (deadsnakes); на 24.04 подходит системный Python 3.12.

Минимальный `.env`:

```ini
BOT_TOKEN=123456789:AA...
COACH_TELEGRAM_ID=123456789
WEBHOOK_BASE_URL=https://app.example.com
DB_PATH=./data/squash.db

BOT_MODE=polling
ALLOW_DEV_INIT_DATA=false
ENABLE_API_DOCS=false
```

> Telegram Web App работает только по **HTTPS**. Поставьте reverse-proxy
> (Caddy/nginx) или туннель и укажите публичный адрес в `WEBHOOK_BASE_URL`.
> Подробно — в [`docs/DEPLOY.md`](docs/DEPLOY.md).

## Роли

| Роль | Возможности |
|---|---|
| **Игрок** | расписание, запись, лист ожидания, календарь, сообщение тренеру |
| **Тренер** | свои занятия, участники, напоминания, CRM; чужие занятия — в «Клубе» только для чтения |
| **Менеджер** | заявки в тренеры и реестр тренеров |
| **Администратор** (`COACH_TELEGRAM_ID`) | всё выше + назначение менеджеров и передача любых занятий |

## Безопасность

- Подпись Telegram `initData` — HMAC-SHA256, проверка `auth_date` и постоянное сравнение.
- Webhook активен только в webhook-режиме; секрет обязателен и проверяется.
- Аватарки и `.ics` используют узкие токены с TTL — полный `initData` не попадает в URL.
- Rate limiting, CSP и security-заголовки; приложение не требует root-прав.
- Зависимости проверяются `pip-audit`; в CI — ruff, mypy, bandit и pytest.

## Разработка

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

DB_PATH=./data/squash.db ALLOW_DEV_INIT_DATA=true ENABLE_API_DOCS=true \
  uvicorn app.main:app --reload

make test       # pytest
make lint       # ruff
make typecheck  # mypy
make audit      # pip-audit
```

## Документация

- [`docs/DEPLOY.md`](docs/DEPLOY.md) — VPS, systemd, HTTPS, туннели, бэкапы.
- [`docs/RENDER.md`](docs/RENDER.md) — деплой на Render.
- [`docs/COACHES.md`](docs/COACHES.md) — роли, заявки, передача занятий.
- [`docs/PRIVACY.md`](docs/PRIVACY.md) — данные, экспорт и удаление.
- [`SECURITY.md`](SECURITY.md) — как сообщить об уязвимости.
- [`CHANGELOG.md`](CHANGELOG.md) — история изменений.

## Стек

FastAPI · aiogram 3 · SQLite (WAL) · Jinja2 · Python 3.11+

## Лицензия

[MIT](LICENSE)
