"""Тесты валидации Telegram initData (HMAC-SHA256)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from app.secure_auth import InitDataError, validate_init_data

TOKEN = "123456:TEST-TOKEN"


def _sign(user: dict, token: str, auth_date: int | None = None) -> str:
    pairs = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AA",
        "user": json.dumps(user, separators=(",", ":")),
    }
    dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


def test_valid_signature():
    user = {"id": 42, "first_name": "Иван"}
    result = validate_init_data(_sign(user, TOKEN), TOKEN)
    assert result["user"]["id"] == 42


def test_wrong_token_rejected():
    with pytest.raises(InitDataError):
        validate_init_data(_sign({"id": 1}, "999:OTHER"), TOKEN)


def test_expired_rejected():
    with pytest.raises(InitDataError):
        validate_init_data(_sign({"id": 1}, TOKEN, auth_date=int(time.time()) - 200000), TOKEN)


def test_tampered_user_rejected():
    signed = _sign({"id": 1, "first_name": "A"}, TOKEN)
    parts = dict(pair.split("=", 1) for pair in signed.split("&"))
    parts["user"] = json.dumps({"id": 999, "first_name": "Hacker"}, separators=(",", ":"))
    tampered = urlencode(parts)
    with pytest.raises(InitDataError):
        validate_init_data(tampered, TOKEN)


def test_empty_rejected():
    with pytest.raises(InitDataError):
        validate_init_data("", TOKEN)
