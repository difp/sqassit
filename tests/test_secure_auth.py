"""Тесты валидации Telegram initData (HMAC-SHA256)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from app import secure_auth
from app.secure_auth import (
    InitDataError,
    issue_scoped_token,
    validate_init_data,
    verify_scoped_token,
)

TOKEN = "123456:TEST-TOKEN"


def _sign(
    user: dict, token: str, auth_date: int | None = None, **extra: str
) -> str:
    pairs = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AA",
        "user": json.dumps(user, separators=(",", ":")),
        **extra,
    }
    dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


def _sign_pairs(pairs: dict[str, str], token: str = TOKEN) -> str:
    dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    signed = dict(pairs)
    signed["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(signed)


def test_valid_signature():
    user = {"id": 42, "first_name": "Иван"}
    result = validate_init_data(_sign(user, TOKEN), TOKEN)
    assert result["user"]["id"] == 42


def test_signature_field_is_part_of_hash():
    user = {"id": 42, "first_name": "Иван"}
    signed = _sign(user, TOKEN, signature="telegram-signature")
    assert validate_init_data(signed, TOKEN)["user"]["id"] == 42


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


def test_scoped_token_roundtrip():
    token = issue_scoped_token(TOKEN, "avatar", 555, 555)
    assert verify_scoped_token(TOKEN, token, "avatar", 555) == 555
    assert verify_scoped_token(TOKEN, token, "avatar", 556) is None
    assert verify_scoped_token(TOKEN, token, "ics", 555) is None
    assert verify_scoped_token("999:OTHER", token, "avatar", 555) is None
    assert verify_scoped_token(TOKEN, "", "avatar", 555) is None


def test_scoped_token_tampered():
    token = issue_scoped_token(TOKEN, "ics", 7, 555)
    assert verify_scoped_token(TOKEN, token[:-1] + ("0" if token[-1] != "0" else "1"), "ics", 7) is None
    assert verify_scoped_token(TOKEN, token.replace(":555:", ":556:"), "ics", 7) is None


def test_scoped_token_expired(monkeypatch):
    monkeypatch.setattr(secure_auth.time, "time", lambda: 1_000_000)
    token = issue_scoped_token(TOKEN, "avatar", 1, 2, ttl_seconds=10)
    assert verify_scoped_token(TOKEN, token, "avatar", 1) == 2
    # expires_at = (1000000 // 10 + 2) * 10 = 1000020
    monkeypatch.setattr(secure_auth.time, "time", lambda: 1_000_019)
    assert verify_scoped_token(TOKEN, token, "avatar", 1) == 2
    monkeypatch.setattr(secure_auth.time, "time", lambda: 1_000_020)
    assert verify_scoped_token(TOKEN, token, "avatar", 1) == 2
    monkeypatch.setattr(secure_auth.time, "time", lambda: 1_000_021)
    assert verify_scoped_token(TOKEN, token, "avatar", 1) is None


def test_scoped_token_is_stable_within_period(monkeypatch):
    """URL аватарки не должен меняться каждую секунду."""
    monkeypatch.setattr(secure_auth.time, "time", lambda: 1_000_000)
    first = issue_scoped_token(TOKEN, "avatar", 555, 555, ttl_seconds=3600)
    monkeypatch.setattr(secure_auth.time, "time", lambda: 1_000_799)
    assert issue_scoped_token(TOKEN, "avatar", 555, 555, ttl_seconds=3600) == first

    monkeypatch.setattr(secure_auth.time, "time", lambda: 1_000_800)
    second = issue_scoped_token(TOKEN, "avatar", 555, 555, ttl_seconds=3600)
    assert second != first
    # Первый токен ещё жив: запас до конца следующего периода.
    assert verify_scoped_token(TOKEN, first, "avatar", 555) == 555


def test_empty_token_rejected():
    with pytest.raises(InitDataError, match="Токен"):
        validate_init_data(_sign({"id": 1}, TOKEN), "")


def test_missing_hash_rejected():
    with pytest.raises(InitDataError, match="hash"):
        validate_init_data("auth_date=1&query_id=AA&user=%7B%22id%22%3A1%7D", TOKEN)


def test_invalid_auth_date_rejected():
    with pytest.raises(InitDataError, match="auth_date"):
        validate_init_data(_sign({"id": 1}, TOKEN, auth_date="not-a-number"), TOKEN)


def test_missing_user_rejected():
    signed = _sign_pairs({"auth_date": str(int(time.time())), "query_id": "AA"})
    with pytest.raises(InitDataError, match="пользователь"):
        validate_init_data(signed, TOKEN)


def test_invalid_user_json_rejected():
    signed = _sign_pairs({
        "auth_date": str(int(time.time())),
        "query_id": "AA",
        "user": "{",
    })
    with pytest.raises(InitDataError, match="JSON"):
        validate_init_data(signed, TOKEN)


def test_user_must_be_dict_with_id():
    signed = _sign_pairs({
        "auth_date": str(int(time.time())),
        "query_id": "AA",
        "user": "[]",
    })
    with pytest.raises(InitDataError, match="Некорректные данные"):
        validate_init_data(signed, TOKEN)


def test_scoped_token_malformed_shapes():
    assert verify_scoped_token(TOKEN, "a:b", "avatar", 1) is None
    assert verify_scoped_token(TOKEN, "avatar:x:1:9999999999:abc", "avatar", 1) is None
    assert verify_scoped_token(TOKEN, "x" * 257, "avatar", 1) is None
