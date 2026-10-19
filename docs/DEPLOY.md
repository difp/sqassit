# Пошаговый деплой

## TL;DR — быстрый деплой на сервер

```bash
# 1. Код
git clone https://github.com/difp/sqassit.git ~/sqassit && cd ~/sqassit

# 2. Конфиг
cp .env.example .env
nano .env
#   BOT_TOKEN=...
#   COACH_TELEGRAM_ID=...
#   TIMEZONE=Europe/Moscow
#   WEBHOOK_BASE_URL=https://app.example.com
#   BOT_MODE=polling
#   ALLOW_DEV_INIT_DATA=false
#   ENABLE_API_DOCS=false

# 3. Данные (UID контейнера = 1000)
mkdir -p ./data && sudo chown -R 1000:1000 ./data

# 4. Запуск
docker compose up -d --build
docker compose ps                 # healthy?
curl -fsS http://127.0.0.1:8000/readyz
```

Снаружи ставьте HTTPS reverse-proxy на `127.0.0.1:8000`. Пример Caddy:

```caddyfile
app.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Если нужен `BOT_MODE=webhook` — добавьте в `.env`:

```bash
WEBHOOK_SECRET=$(openssl rand -hex 32)
BOT_MODE=webhook
```

и перезапустите `docker compose up -d`. Для polling публичный HTTPS нужен
только для Web App (кнопка бота использует `WEBHOOK_BASE_URL`).

Дальше: открыть бота → `/start` → проверить расписание; настроить бэкап
`make backup` по cron. Подробности и туннели — ниже.

---

Инструкция для запуска на своём железе (Raspberry Pi, мини-ПК, ноутбук, NAS)
без арендованного сервера. Нужна одна вещь извне — **публичный HTTPS-адрес**
для Web App (даётся бесплатным туннелем, см. шаг 6).

Ориентир по ресурсам: 1 ядро, ~250 МБ RAM, ~300 МБ диска, 64-битная ОС.

---

## 0. Что понадобится

- Устройство с Linux (Raspberry Pi OS / Ubuntu / Debian и т.п.), 64-bit.
- Docker + Docker Compose v2.
- Telegram-аккаунт тренера (для `COACH_TELEGRAM_ID`).
- Бот в Telegram (@BotFather) → токен.
- Туннель для HTTPS (Cloudflare Tunnel или Tailscale Funnel) — шаг 6.

---

## 1. Установить Docker

```bash
# официальный скрипт (Ubuntu/Debian/Raspberry Pi OS)
curl -fsSL https://get.docker.com | sh

# разрешить запуск без sudo (перелогиньтесь после)
sudo usermod -aG docker "$USER"
newgrp docker

# проверить
docker run --rm hello-world
docker compose version
```

---

## 2. Получить токен бота и id тренера

1. **@BotFather** → `/newbot` → скопируйте `BOT_TOKEN` (вида `123456789:AA…`).
2. Тренер открывает **@userinfobot** → `/start` → присылает число — это
   `COACH_TELEGRAM_ID` (администратор, личный аккаунт).

---

## 3. Доставить проект на устройство

```bash
# вариант А: из архива релиза
scp squash-bot-1.2.3.tar.gz user@device:~/
ssh user@device
mkdir -p ~/squash-bot && tar -xzf squash-bot-1.2.3.tar.gz -C ~/squash-bot
cd ~/squash-bot

# вариант Б: из git (если репозиторий есть)
git clone <URL> ~/squash-bot && cd ~/squash-bot
```

---

## 4. Настроить `.env`

```bash
cp .env.example .env
nano .env
```

Минимум:

```ini
BOT_TOKEN=123456789:AA...your...token
COACH_TELEGRAM_ID=123456789
TIMEZONE=Europe/Moscow
DB_PATH=/app/data/squash.db
BOT_MODE=polling
WEBHOOK_BASE_URL=https://ВАШ-ПУБЛИЧНЫЙ-АДРЕС   # из шага 6
# WEBHOOK_SECRET нужен только при BOT_MODE=webhook (см. шаг 7)
ALLOW_DEV_INIT_DATA=false
ENABLE_API_DOCS=false
```

> `WEBHOOK_BASE_URL` используется и как адрес Web App в кнопке бота, поэтому он
> нужен даже при `BOT_MODE=polling`. Временно можно оставить пустым и заполнить
> после шага 6.

---

## 5. Подготовить папку данных и запустить

```bash
mkdir -p ./data
sudo chown -R 1000:1000 ./data     # контейнер работает от UID 1000

make up          # = docker compose up -d --build
make ps          # статус; должен стать healthy
make logs        # логи (Ctrl+C — выйти, контейнер продолжит работать)
```

Проверка внутри устройства:

```bash
curl -s http://127.0.0.1:8000/healthz      # liveness: {"ok":true}
curl -s http://127.0.0.1:8000/readyz       # readiness: БД + Telegram Bot API
```

---

## 6. Публичный HTTPS (без сервера)

### Вариант A — Cloudflare Tunnel с доменом (рекомендуется, стабильный URL)

```bash
# 1) залогиниться (откроет браузер/даст ссылку)
docker run --rm -it -v ~/.cloudflared:/home/nonroot/.cloudflared cloudflare/cloudflared:latest tunnel login

# 2) создать туннель
docker run --rm -v ~/.cloudflared:/home/nonroot/.cloudflared cloudflare/cloudflared:latest tunnel create squashbot

# 3) привязать домен (DNS создастся автоматически)
docker run --rm -v ~/.cloudflared:/home/nonroot/.cloudflared cloudflare/cloudflared:latest tunnel route dns squashbot app.example.com

# 4) запустить туннель к приложению
docker run -d --name cloudflared --restart always --network host \
  -v ~/.cloudflared:/home/nonroot/.cloudflared \
  cloudflare/cloudflared:latest tunnel --no-autoupdate run --url http://127.0.0.1:8000 squashbot
```

Получите `https://app.example.com` → `curl -s https://app.example.com/readyz`.

### Вариант B — Cloudflare быстрый туннель (без домена, URL меняется при перезапуске)

```bash
docker run -d --name cloudflared --restart always --network host \
  cloudflare/cloudflared:latest tunnel --no-autoupdate --url http://127.0.0.1:8000
docker logs cloudflared 2>&1 | grep -o 'https://[a-z0-9.-]*trycloudflare.com' | head -1
```

### Вариант C — Tailscale Funnel

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale funnel 8000          # даст https://<host>.<tailnet>.ts.net
```

### Вариант D — свой домен + «белый» IP

Caddy как reverse-proxy (сам получит Let's Encrypt):

```
# Caddyfile
app.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

---

## 7. Прописать URL и применить

Впишите полученный HTTPS-адрес в `.env` → `WEBHOOK_BASE_URL=https://…` и перезапустите:

```bash
make up
```

Режим доставки апдейтов:
- **`BOT_MODE=polling`** (по умолчанию) — проще, публичный URL нужен только для Web App.
- **`BOT_MODE=webhook`** — Telegram сам шлёт апдейты на `/telegram/webhook`;
  задайте случайный `WEBHOOK_SECRET` (не короче 32 символов). При пустом,
  коротком или placeholder-значении приложение **не стартует** — это защита от
  подделки апдейтов:

  ```bash
  WEBHOOK_SECRET=$(openssl rand -hex 32)
  ```

---

## 8. Проверка

1. Открыть бота в Telegram → **/start** → кнопка **«🏸 Открыть тренировки»**.
2. Появляется Web App: расписание (игрок) и вкладка «Тренер».
3. Тренер создаёт занятие → оно видно игрокам; «Добавить в календарь» отдаёт `.ics`.
4. Проверить контейнер: `make ps` (healthy), логов ошибок нет: `make logs`.

---

## 9. Резервные копии

```bash
make backup     # создаст ./data/backup-<дата>.db
```

В cron (ежедневно в 03:30):

```bash
crontab -e
# 30 3 * * * cd /home/user/squash-bot && make backup >>/tmp/squash-backup.log 2>&1
```

Восстановление: остановить контейнер, положить нужный `backup-*.db` как
`./data/squash.db` (удалить `squash.db-wal`/`squash.db-shm`), `make up`.

---

## 10. Обновление без потери данных

```bash
# распаковать новый релиз поверх, либо:
git pull
make up          # пересборка образа; БД в ./data не трогается
```

Схема БД мигрирует автоматически (идемпотентно).

---

## 11. Частые проблемы

| Симптом | Решение |
|---|---|
| `Unhealthy` в `make ps` | смотреть `make logs`; `/readyz` проверяет БД и `BOT_TOKEN` (частые причины: неверный токен, нет сети до Telegram, проблемы с volume) |
| «Не задана … COACH_TELEGRAM_ID» | заполнить `.env` |
| Web App не открывается / «Ошибка 401» | `WEBHOOK_BASE_URL` не тот или нет HTTPS; откройте именно по https |
| Вкладка «Тренер» без занятий | пользователь не тренер — администратор одобряет заявку |
| Кнопка бота ведёт не туда | обновите `WEBHOOK_BASE_URL` и `make up` |
| Туннель даёт новый URL после перезапуска | используйте named-tunnel (Вариант A) или Tailscale |
| Напоминания не пришли | не было интернета в окне отправки; окно задаётся в карточке |

---

## Мини-шпаргалка

```bash
make up        # запустить/обновить
make ps        # статус и health
make logs      # логи
make backup    # бэкап БД
make down      # остановить (данные в ./data сохраняются)
make release   # чек-лист релиза
```
