# Деплой на сервер

Приложение — обычный Python-процесс: `uvicorn` + SQLite. Нужны Python 3.11,
публичный HTTPS-адрес и любой reverse-proxy (Caddy/nginx) или Render.

## TL;DR — VPS

```bash
# 1. Пользователь и код
sudo useradd --system --create-home --shell /usr/sbin/nologin sqassit
sudo mkdir -p /opt/sqassit && sudo chown sqassit:sqassit /opt/sqassit
sudo -u sqassit git clone https://github.com/difp/sqassit.git /opt/sqassit
cd /opt/sqassit

# 2. Зависимости
sudo -u sqassit python3.11 -m venv .venv
sudo -u sqassit .venv/bin/pip install -r requirements.txt

# 3. Конфиг
sudo -u sqassit cp .env.example .env
sudo -u sqassit nano .env          # BOT_TOKEN, COACH_TELEGRAM_ID, WEBHOOK_BASE_URL
sudo -u sqassit mkdir -p data

# 4. systemd
sudo tee /etc/systemd/system/sqassit.service >/dev/null <<'EOF'
[Unit]
Description=Squash-Bot Telegram Web App
After=network.target

[Service]
Type=simple
User=sqassit
WorkingDirectory=/opt/sqassit
EnvironmentFile=/opt/sqassit/.env
ExecStart=/opt/sqassit/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now sqassit
curl -fsS http://127.0.0.1:8000/healthz
```

Дальше — HTTPS reverse-proxy. Пример Caddy:

```bash
sudo apt install -y caddy
sudo tee /etc/caddy/Caddyfile >/dev/null <<'EOF'
app.example.com {
    reverse_proxy 127.0.0.1:8000
}
EOF
sudo systemctl reload caddy
```

DNS-запись `app.example.com` → IP сервера. Caddy сам получит Let's Encrypt.
После этого проверьте:

```bash
curl -fsS https://app.example.com/readyz
```

`/healthz` — liveness, `/readyz` — БД + доступность Telegram Bot API.

## `.env`

```ini
BOT_TOKEN=123456789:AA...
COACH_TELEGRAM_ID=123456789
TIMEZONE=Europe/Moscow
DB_PATH=./data/squash.db

BOT_MODE=polling
WEBHOOK_BASE_URL=https://app.example.com
WEBHOOK_SECRET=

ALLOW_DEV_INIT_DATA=false
ENABLE_API_DOCS=false
```

- `COACH_TELEGRAM_ID` — числовой Telegram ID администратора.
- `WEBHOOK_BASE_URL` нужен и для кнопки Web App в боте, даже при `polling`.
- `BOT_MODE=webhook` требует случайный `WEBHOOK_SECRET` (≥32 символов). Без него
  приложение не стартует — это защита от подделки апдейтов:

```bash
openssl rand -hex 32
```

- `ALLOW_DEV_INIT_DATA=true` — только для локальной разработки.
- `.env` должен быть доступен только сервису: `chmod 600 .env`.

## Polling или webhook

| Режим | Когда использовать |
|---|---|
| `polling` | проще, не нужен входящий webhook; процесс должен работать постоянно |
| `webhook` | лучше для Render/always-on VPS: Telegram сам присылает апдейты на `/telegram/webhook` |

При `webhook` после смены домена обновите `WEBHOOK_BASE_URL` и перезапустите сервис:

```bash
sudo systemctl restart sqassit
```

## Обновление

```bash
cd /opt/sqassit
sudo -u sqassit git pull
sudo -u sqassit .venv/bin/pip install -r requirements.txt
sudo systemctl restart sqassit
curl -fsS http://127.0.0.1:8000/readyz
```

## Бэкапы

```bash
cd /opt/sqassit
sudo -u sqassit make backup
```

Cron (ежедневно в 03:30):

```cron
30 3 * * * cd /opt/sqassit && make backup >> /var/log/sqassit-backup.log 2>&1
```

Бэкапы появятся в `data/backup-*.db`. Восстановление: остановить сервис,
положить файл как `data/squash.db`, удалить `squash.db-wal`/`squash.db-shm`,
запустить сервис.

## Логи и диагностика

```bash
sudo systemctl status sqassit
sudo journalctl -u sqassit -f
curl -i http://127.0.0.1:8000/healthz
curl -i http://127.0.0.1:8000/readyz
```

Частые причины `readyz 503`: неверный `BOT_TOKEN`, нет сети до Telegram,
проблемы с правами на `data/`, неверный `DB_PATH`.

## Render

Инструкция для Render (с `render.yaml` и persistent disk):
[`RENDER.md`](RENDER.md).

## Туннель без своего домена

Можно поставить `cloudflared` и получить HTTPS без сервера:

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

Полученный URL впишите в `WEBHOOK_BASE_URL` и перезапустите сервис. Для
стабильного адреса используйте named tunnel или Render.
