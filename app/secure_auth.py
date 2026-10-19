"""
Валидация Telegram Web App initData.

Реализовано строго по официальной документации Telegram:
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

Алгоритм:
  1. secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
  2. data_check_string = "ключ=значение", отсортированные по ключу, соединённые "\n"
     (всё, кроме поля hash)
  3. hash = HMAC_SHA256(key=secret_key, msg=data_check_string), hex-сравнение
     в постоянном времени (hmac.compare_digest).

Без этой проверки пользователь мог бы подделать свой telegram_id/роль через
подмену initData в браузере.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qsl


class InitDataError(Exception):
    """initData отсутствует, повреждён, просрочен или подпись не совпала."""


def _build_secret_key(bot_token: str) -> bytes:
    """Формирует секретный ключ из токена бота."""
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 86400,
) -> dict[str, Any]:
    """
    Проверяет подпись initData и возвращает распарсенные поля.

    :param init_data: сырая строка из window.Telegram.WebApp.initData.
    :param bot_token: токен бота (из окружения).
    :param max_age_seconds: максимальный возраст initData (защита от replay).
    :raises InitDataError: если проверка не пройдена.
    """
    if not init_data:
        raise InitDataError("Пустой initData.")
    if not bot_token:
        raise InitDataError("Токен бота не задан.")

    # parse_qsl сохраняет все поля как строки; keep_blank_values нужен для подписи.
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise InitDataError("В initData отсутствует поле hash.")

    # Поле signature (Telegram Web App 7.x) не участвует в data_check_string.
    pairs.pop("signature", None)

    # data_check_string: отсортировано по ключу, соединено через \n.
    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))

    secret_key = _build_secret_key(bot_token)
    calculated_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    # Сравнение в постоянном времени — защита от timing-атак.
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise InitDataError("Неверная подпись initData.")

    # Защита от replay: проверяем свежесть auth_date.
    auth_date_raw = pairs.get("auth_date")
    if auth_date_raw is None or not auth_date_raw.isdigit():
        raise InitDataError("Некорректный auth_date.")
    if time.time() - int(auth_date_raw) > max_age_seconds:
        raise InitDataError("initData просрочен.")

    # Разворачиваем вложенный JSON user.
    user_json = pairs.get("user")
    if not user_json:
        raise InitDataError("В initData отсутствует пользователь.")
    try:
        user = json.loads(user_json)
    except json.JSONDecodeError as exc:
        raise InitDataError("Некорректный JSON пользователя.") from exc

    if not isinstance(user, dict) or "id" not in user:
        raise InitDataError("Некорректные данные пользователя.")

    return {"user": user, "auth_date": int(auth_date_raw), "raw": pairs}


# --------------------------------------------------------------------------- #
# Узкие (scoped) токены для ссылок, которые нельзя авторизовать заголовком
# --------------------------------------------------------------------------- #
# initData — это полноценный bearer-токен на 24 часа. Подставлять его в src
# картинки или в ссылку скачивания нельзя: он попадает в access-логи, историю
# браузера и Referer. Вместо этого выдаём отдельный токен, привязанный к
# конкретному ресурсу (аватар/занятие), пользователю и сроку жизни.
#
# Формат: kind:resource_id:user_id:expires_at:hmac_sha256_hex

def _scoped_key(secret: str) -> bytes:
    """Выводит отдельный ключ для scoped-токенов из секрета приложения."""
    return hmac.new(
        secret.encode("utf-8"), b"sqassit-scoped-token-v1", hashlib.sha256
    ).digest()


def issue_scoped_token(
    secret: str,
    kind: str,
    resource_id: int,
    user_id: int,
    *,
    ttl_seconds: int = 86400,
) -> str:
    """Создаёт подписанный токен для конкретного ресурса и пользователя."""
    expires_at = int(time.time()) + max(1, int(ttl_seconds))
    payload = f"{kind}:{int(resource_id)}:{int(user_id)}:{expires_at}"
    signature = hmac.new(
        _scoped_key(secret), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{payload}:{signature}"


def verify_scoped_token(
    secret: str,
    token: str,
    kind: str,
    resource_id: int,
) -> int | None:
    """
    Проверяет scoped-токен и возвращает user_id либо None.

    Проверяются: формат, тип ресурса, id ресурса, срок жизни и HMAC-подпись.
    """
    if not secret or not token or len(token) > 256:
        return None

    parts = token.split(":")
    if len(parts) != 5:
        return None

    token_kind, resource_raw, user_raw, expires_raw, signature = parts
    try:
        resource = int(resource_raw)
        user_id = int(user_raw)
        expires_at = int(expires_raw)
    except ValueError:
        return None

    if token_kind != kind or resource != int(resource_id):
        return None
    if expires_at < int(time.time()):
        return None

    payload = f"{token_kind}:{resource}:{user_id}:{expires_at}"
    expected = hmac.new(
        _scoped_key(secret), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    return user_id
