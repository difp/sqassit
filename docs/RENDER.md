# Деплой на Render

## Почему падало

Render для Python-сервиса по умолчанию запускает:

```bash
gunicorn your_application.wsgi
```

Проект — **ASGI**-приложение на FastAPI, а `gunicorn` в зависимостях нет.
Нужно явно задать Start Command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Не добавляйте `--workers`: приложению нужен один процесс (SQLite, polling и
in-memory-состояние).

## Быстрый способ: Blueprint

В репозитории есть [`render.yaml`](../render.yaml). В Render:

1. **New → Blueprint** → выберите репозиторий.
2. Render прочитает `render.yaml` и запросит:
   - `BOT_TOKEN`;
   - `COACH_TELEGRAM_ID`;
   - `WEBHOOK_BASE_URL` — `https://<имя-сервиса>.onrender.com`.
3. Apply. По умолчанию используется `BOT_MODE=polling` — сервис запустится
   сразу, без webhook-секрета.

Blueprint создаёт web-сервис с Python 3.11, старт-командой `uvicorn`,
persistent disk в `/var/data` и `DB_PATH=/var/data/squash.db`.

## Вручную в Dashboard

**New → Web Service → Python**

| Настройка | Значение |
|---|---|
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Health Check Path | `/healthz` |
| Python version | `3.11.9` (env `PYTHON_VERSION`) |

Переменные:

```ini
BOT_TOKEN=...
COACH_TELEGRAM_ID=...
TIMEZONE=Europe/Moscow
DB_PATH=/var/data/squash.db

BOT_MODE=polling
WEBHOOK_BASE_URL=https://<имя-сервиса>.onrender.com

ALLOW_DEV_INIT_DATA=false
ENABLE_API_DOCS=false
```

Если сервис создан без диска, `DB_PATH` должен указывать на writable-путь
(`./data/squash.db`), но база будет теряться при каждом деплое.

## Persistent disk

SQLite нельзя хранить на эфемерном диске Render. Добавьте **Disk**:

- Name: `squash-data`
- Mount Path: `/var/data`
- Size: 1 GB

и оставьте `DB_PATH=/var/data/squash.db`. Render Disk доступен на платных
инстансах (Starter и выше).

## Webhook (опционально)

Можно оставить `BOT_MODE=polling` — для одного always-on инстанса этого достаточно.
Если хотите webhook:

```bash
openssl rand -hex 32
```

Добавьте в Render env:

```ini
BOT_MODE=webhook
WEBHOOK_SECRET=<сгенерированное значение>
```

`WEBHOOK_BASE_URL` — публичный HTTPS-адрес сервиса, без слэша на конце.
Без `WEBHOOK_SECRET` приложение в webhook-режиме не стартует.

После деплоя:

```bash
curl -fsS https://<имя-сервиса>.onrender.com/readyz
```

Откройте бота → `/start`. Кнопка Web App использует `WEBHOOK_BASE_URL`.

## Ограничения Render

- **Free plan**: сервис засыпает после простоя; напоминания и polling не будут
  работать стабильно, а база теряется при рестарте. Для реального клуба нужен
  платный always-on инстанс + disk.
- **Один инстанс.** Не включайте scaling: SQLite и polling не рассчитаны на
  несколько процессов.
- **Память:** приложению нужно ~190 МБ RSS; минимум 512 МБ, комфортно 1 ГБ.
- **Webhook vs polling:** оба варианта работают на always-on инстансе.
  `webhook` не держит long-polling соединение; `polling` проще в настройке.
  На free-плане сервис засыпает — напоминания в любом режиме будут пропускаться.
