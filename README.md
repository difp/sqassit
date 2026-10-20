# 🏸 Squash-Bot

Telegram Mini App для сквош-клуба: расписание, запись в один тап, лист ожидания
и CRM тренера — без отдельного мобильного приложения.

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.146-009688?logo=fastapi&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

<p align="center">
  <img src="docs/screenshots/player.png" alt="Расписание игрока" width="44%">
  <img src="docs/screenshots/coach.png" alt="Панель тренера" width="44%">
</p>

## Что умеет

- запись на тренировки, лист ожидания FIFO, напоминания и `.ics`;
- несколько тренеров, роли, заявки и передача занятий;
- CRM, заметки и личные сообщения через бота;
- приватность: имя/фото или животное, username только по согласию, без ID в чатах;
- история, корзина, экспорт и удаление персональных данных.

## Быстрый старт

```bash
git clone https://github.com/difp/sqassit.git
cd sqassit
make install
cp .env.example .env
# BOT_TOKEN, COACH_TELEGRAM_ID, WEBHOOK_BASE_URL
mkdir -p data
make run
```

Минимальный `.env`:

```ini
BOT_TOKEN=...
COACH_TELEGRAM_ID=...
WEBHOOK_BASE_URL=https://...
```

Telegram Web App работает только по HTTPS. Production-деплой:
`make install-prod` + systemd + reverse-proxy — см. [`docs/DEPLOY.md`](docs/DEPLOY.md).

## Документация

- [`docs/DEPLOY.md`](docs/DEPLOY.md) — VPS, systemd, HTTPS, бэкапы.
- [`docs/COACHES.md`](docs/COACHES.md) — роли, заявки, передача занятий.
- [`docs/PRIVACY.md`](docs/PRIVACY.md) — данные, приватность, экспорт и удаление.
- [`SECURITY.md`](SECURITY.md) — как сообщить об уязвимости.
- [`CHANGELOG.md`](CHANGELOG.md) — история изменений.

## Лицензия

[MIT](LICENSE)
