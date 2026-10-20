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

## 6. Автозапуск

Официальный установщик Xray создаёт `xray.service`:

```bash
sudo systemctl enable --now xray
```

Если сервис не создался — проверьте:

```bash
systemctl cat xray
```

## 7. Безопасность

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
