# VLESS / Xray → локальный SOCKS5

Нужно, когда сервер не может напрямую достучаться до `api.telegram.org`, а есть
VLESS-ключ (Paper VPN, Hiddify и т.п.). Приложение уже умеет ходить через
`BOT_PROXY`.

```text
VLESS-ключ → Xray (outbound) → SOCKS5 127.0.0.1:1080 → BOT_PROXY → Telegram API
```

Hiddify — GUI-клиент над sing-box. На headless-сервере удобнее **Xray**:
тот же VLESS-протокол, без графики.

## 1. Установить Xray

```bash
sudo bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
xray version
```

Конфиг по умолчанию: `/usr/local/etc/xray/config.json`.

## 2. Взять VLESS-ссылку

В Paper VPN/Hiddify скопируйте ключ вида:

```text
vless://UUID@SERVER:443?type=tcp&security=reality&pbk=...&sid=...&sni=...&flow=xtls-rprx-vision#Paper
```

Если это subscription URL, получите ссылки:

```bash
curl -L "https://..." -o /tmp/paper-sub
head -c 300 /tmp/paper-sub

# если содержимое base64:
base64 -d /tmp/paper-sub | grep -m1 '^vless://'
```

Не публикуйте ссылку: в ней UUID и параметры доступа.

## 3. Сгенерировать конфиг

```bash
cd /opt/sqassit
sudo .venv/bin/python scripts/xray_vless.py "$VLESS_URL" \
  --write /usr/local/etc/xray/config.json
```

Или через Makefile:

```bash
cd /opt/sqassit
sudo make xray-config VLESS_URL='vless://....'
```

Скрипт поддерживает:

- Reality (`security=reality`, `pbk`, `sid`);
- TLS (`security=tls`);
- без TLS (`security=none`);
- `type=tcp`, `ws`, `grpc`, `http`;
- `flow=xtls-rprx-vision`;
- `ws` path/host, `grpc` serviceName.

На выходе — SOCKS5 на `127.0.0.1:1080` и VLESS outbound.

## 4. Запустить

```bash
sudo xray run -test -c /usr/local/etc/xray/config.json
sudo systemctl restart xray
sudo systemctl status xray --no-pager
sudo journalctl -u xray -n 50 --no-pager
```

Проверка:

```bash
curl --socks5-hostname 127.0.0.1:1080 -4 -sS \
  -o /dev/null -w '%{http_code}\n' https://api.telegram.org
```

Любой HTTP-код (`200/302/404`) означает, что прокси работает.

## 5. Подключить приложение

```bash
cd /opt/sqassit
sudo -u sqassit git pull
sudo -u sqassit make install-prod
```

В `.env`:

```ini
BOT_PROXY=socks5://127.0.0.1:1080
```

```bash
sudo systemctl restart sqassit
curl -s http://127.0.0.1:8000/readyz
```

Должно вернуться `{"ok":true}`. После этого бот снова получит `/start`.

## 6. Автообновление по subscription URL

Если Paper VPN отдаёт ключ по ссылке (в браузере открывается `vless://...`),
не нужно копировать ключ вручную. Сохраните URL в файл:

```bash
echo 'https://...' | sudo tee /etc/xray-subscription.url >/dev/null
sudo chmod 600 /etc/xray-subscription.url
```

Проверьте обновление один раз:

```bash
cd /opt/sqassit
sudo .venv/bin/python scripts/update_xray_config.py \
  --url-file /etc/xray-subscription.url \
  --write /usr/local/etc/xray/config.json \
  --owner nobody:nogroup \
  --restart
```

`--owner` нужен потому, что Xray обычно запускается systemd от пользователя
`nobody`. Если сервис работает от другого пользователя:

```bash
systemctl show -p User --value xray
id nobody
```

и подставьте нужное значение.

Или:

```bash
cd /opt/sqassit
sudo make xray-update
```

Скрипт:

- скачивает URL с браузерным User-Agent;
- понимает plain text, base64 (обычный/URL-safe), JSON и HTML;
- выбирает первую `vless://`-ссылку;
- атомарно пишет конфиг;
- проверяет его через `xray run -test`;
- перезапускает `xray`;
- при ошибке оставляет старый конфиг.

Установите таймер (обновление каждые 6 часов и через 2 минуты после загрузки):

```bash
sudo cp /opt/sqassit/deploy/xray-subscription.service /etc/systemd/system/
sudo cp /opt/sqassit/deploy/xray-subscription.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now xray-subscription.timer

systemctl list-timers xray-subscription.timer
sudo journalctl -u xray-subscription.service -n 50 --no-pager
```

Если subscription вернул несколько ссылок, можно выбрать нужную:

```bash
sudo .venv/bin/python scripts/update_xray_config.py \
  --url-file /etc/xray-subscription.url \
  --index 1 --write /usr/local/etc/xray/config.json --restart
```

Проверить без записи:

```bash
sudo .venv/bin/python scripts/update_xray_config.py \
  --url-file /etc/xray-subscription.url --dry-run
```

## Если SOCKS не слушает 1080

```bash
sudo systemctl status xray --no-pager
sudo journalctl -u xray -n 80 --no-pager
systemctl show -p User --value xray
ls -l /usr/local/etc/xray/config.json
```

Если в логе `permission denied`:

```bash
sudo chown nobody:nogroup /usr/local/etc/xray/config.json
sudo chmod 600 /usr/local/etc/xray/config.json
sudo systemctl restart xray
ss -ltnp | grep 1080
```

Если Xray работает от другого пользователя — замените `nobody:nogroup`
на `$(systemctl show -p User --value xray)` и его группу.

## 7. Автозапуск

Официальный установщик Xray создаёт `xray.service`:

```bash
sudo systemctl enable --now xray
```

Если сервис не создался — проверьте:

```bash
systemctl cat xray
```

## 8. Безопасность

- `/usr/local/etc/xray/config.json` содержит UUID — `chmod 600`.
- SOCKS слушает только `127.0.0.1:1080`, наружу не открывайте.
- `BOT_PROXY` хранится в `.env`; `.env` — `chmod 600`.
- Проверьте условия Paper VPN: подписка может ограничивать число устройств/серверов.
- Если VLESS-ссылка перестала работать, обновите её в Paper VPN, перегенерируйте
  конфиг и перезапустите `xray`.

## Если ключ в формате VMess

Текущий генератор принимает только `vless://`. Для `vmess://` нужна отдельная
схема outbound (base64 JSON с полями `add`, `port`, `id`, `net`, `tls`).
Пришлите тип ключа — добавим поддержку.
