# Деплой на Ubuntu

Приложение — обычный Python-процесс: `uvicorn` + SQLite. Docker не нужен.
Нужны Ubuntu 22.04/24.04+, Python 3.11+, домен и HTTPS reverse-proxy (nginx).

## 0. Проверить Python

```bash
python3 --version
```

Нужен **Python 3.11+**. Если на Ubuntu 22.04 стоит 3.10:

```bash
sudo apt update
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.11 python3.11-venv
```

На Ubuntu 24.04 системный Python 3.12 уже подходит.

## 1. Пакеты

```bash
sudo apt update
sudo apt install -y git python3-venv python3-pip nginx certbot python3-certbot-nginx ufw
```

## 2. Пользователь и код

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin sqassit
sudo mkdir -p /opt/sqassit && sudo chown sqassit:sqassit /opt/sqassit

sudo -u sqassit git clone https://github.com/difp/sqassit.git /opt/sqassit
cd /opt/sqassit
```

## 3. Зависимости

```bash
sudo -u sqassit make install-prod
```

Makefile сам выберет Python 3.11 → 3.12 → python3 и создаст `.venv`.
Если хотите явно:

```bash
sudo -u sqassit python3.11 -m venv .venv
sudo -u sqassit .venv/bin/pip install -r requirements.txt
```

## 4. Конфиг

```bash
sudo -u sqassit cp .env.example .env
sudo -u sqassit nano .env
sudo chmod 600 .env
sudo -u sqassit mkdir -p data
```

Минимум:

```ini
BOT_TOKEN=123456789:AA...
COACH_TELEGRAM_ID=123456789
TIMEZONE=Europe/Moscow
DB_PATH=./data/squash.db

BOT_MODE=polling
WEBHOOK_BASE_URL=https://app.example.com
WEBHOOK_SECRET=
BOT_PROXY=

ALLOW_DEV_INIT_DATA=false
ENABLE_API_DOCS=false
```

- `COACH_TELEGRAM_ID` — числовой Telegram ID администратора (`@userinfobot`).
- `WEBHOOK_BASE_URL` нужен даже при `polling`: это адрес кнопки Web App в боте.
- `BOT_PROXY` — опционально, если `api.telegram.org` недоступен напрямую:
  `socks5://127.0.0.1:1080` или `http://user:pass@proxy:3128`.
  VLESS/Xray и локальный SOCKS — [`XRAY.md`](XRAY.md).
- `.env` должен быть доступен только сервису (`chmod 600`).

## 5. systemd

```bash
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

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/opt/sqassit/data

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now sqassit
sudo systemctl status sqassit --no-pager
curl -fsS http://127.0.0.1:8000/healthz
```

## 6. nginx + HTTPS

DNS-запись `app.example.com` должна указывать на IP сервера. Если домена нет,
используйте `sslip.io`: имя `<IP-с-точками>.sslip.io` автоматически указывает
на этот IP, например `<IP-с-точками>.sslip.io`. Сертификат Let's Encrypt для
него выпускается так же.

```bash
sudo tee /etc/nginx/sites-available/sqassit >/dev/null <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name app.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/sqassit /etc/nginx/sites-enabled/sqassit
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx

# сертификат Let's Encrypt + редирект на HTTPS
sudo certbot --nginx -d app.example.com
```

Проверка:

```bash
curl -fsS https://app.example.com/readyz
```

`/healthz` — liveness, `/readyz` — БД + доступность Telegram Bot API.

## 7. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw --force enable
sudo ufw status
```

Порт `8000` наружу не открывайте — он слушает только `127.0.0.1`.

## 8. Бот

Откройте бота → `/start` → «🏸 Открыть тренировки».

Если хотите webhook вместо polling:

```bash
openssl rand -hex 32
```

В `.env`:

```ini
BOT_MODE=webhook
WEBHOOK_SECRET=<сгенерированное значение>
```

```bash
sudo systemctl restart sqassit
```

Без секрета приложение в webhook-режиме не стартует — это защита от подделки
апдейтов.

## 9. Бэкапы

```bash
cd /opt/sqassit
sudo -u sqassit make backup
```

Cron от пользователя `sqassit` (ежедневно в 03:30):

```cron
30 3 * * * cd /opt/sqassit && make backup >> /opt/sqassit/backup.log 2>&1
```

Бэкапы появятся в `data/backup-*.db`. Восстановление: остановить сервис,
положить файл как `data/squash.db`, удалить `squash.db-wal`/`squash.db-shm`,
запустить сервис.

## 10. Обновление

```bash
cd /opt/sqassit
sudo -u sqassit git pull
sudo -u sqassit make install-prod
sudo systemctl restart sqassit
curl -fsS http://127.0.0.1:8000/readyz
```

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

## Публичный адрес без своего домена

Не используйте быстрый туннель Cloudflare для постоянной работы: его URL
меняется при каждом перезапуске. Надёжнее `sslip.io` + nginx + Let's Encrypt:

```bash
PUBLIC_HOST="$(curl -4 -s https://api.ipify.org).sslip.io"
echo "$PUBLIC_HOST"
```

Укажите это имя в `server_name` nginx и выпустите сертификат:

```bash
sudo certbot --nginx -d "$PUBLIC_HOST"
```

В `.env`:

```ini
WEBHOOK_BASE_URL=https://<IP>.sslip.io
```

```bash
sudo systemctl restart sqassit
curl -fsS https://<IP>.sslip.io/healthz
```
