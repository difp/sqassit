# SQASSIT

Telegram Mini App для сквош-клуба: расписание, запись в один тап, лист ожидания
и CRM тренера — без отдельного мобильного приложения.

Python 3.11+ · FastAPI · aiogram 3 · SQLite

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.146-009688?logo=fastapi&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

<p align="center">
  <a href="docs/screenshots/player-match.png"><img src="docs/screenshots/player-match.png" alt="Игрок · расписание и запись; Матч · свои игровые встречи" width="100%"></a>
</p>
<p align="center">
  <a href="docs/screenshots/profile-coach.png"><img src="docs/screenshots/profile-coach.png" alt="Профиль · публичная карточка игрока; Тренер · занятия и участники" width="100%"></a>
</p>

## Что умеет

- запись на тренировки, лист ожидания FIFO, напоминания и `.ics`;
- создание серии тренировок по выбранным дням недели в периоде;
- вкладка «Матч» для своих игровых встреч и поиска игроков;
- публичные профили игроков и настройки уведомлений: матчи, тренировки, сообщения;
- обратная связь: проблема или предложение уходит администратору прямо из приложения;
- несколько тренеров, роли, заявки и передача занятий;
- CRM, заметки и личные сообщения через бота;
- приватность: имя и фото можно скрыть под животным, username виден только по согласию, Telegram ID не показываются в чатах;
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

Telegram Web App работает только по HTTPS. Продакшен:
`make install-prod` + systemd + reverse-proxy — см. [`docs/DEPLOY.md`](docs/DEPLOY.md).

## Документация

- [`docs/DEPLOY.md`](docs/DEPLOY.md) — VPS, systemd, HTTPS, бэкапы.
- [`docs/COACHES.md`](docs/COACHES.md) — роли, заявки, передача занятий.
- [`docs/PRIVACY.md`](docs/PRIVACY.md) — данные, приватность, экспорт и удаление.
- [`SECURITY.md`](SECURITY.md) — как сообщить об уязвимости.
- [`CHANGELOG.md`](CHANGELOG.md) — история изменений.

## Лицензия

[MIT](LICENSE)
