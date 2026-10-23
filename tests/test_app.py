"""Интеграционные тесты API и бизнес-правил (через TestClient)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient

import app.bot as botmod
import app.main as mainmod
from app.config import TRAINING_LEVELS, settings
from app.secure_auth import issue_scoped_token


# --- Заглушки сети Telegram (в тестах наружу не ходим) --- #
@asynccontextmanager
async def _fake_polling(bot, dp):
    yield


async def _fake_loop(bot, interval_seconds=60):
    while True:
        await asyncio.sleep(3600)


def _init(user: dict) -> str:
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "TEST",
        "user": json.dumps(user, separators=(",", ":")),
    }
    dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


COACH = {"X-Init-Data": _init({"id": 123, "first_name": "Тренер"})}
COACH2 = {"X-Init-Data": _init({"id": 777, "first_name": "Второй тренер"})}
PLAYER = {"X-Init-Data": _init({"id": 555, "first_name": "Иван"})}
PLAYER2 = {"X-Init-Data": _init({"id": 556, "first_name": "Мария"})}
APPLICANT = {"X-Init-Data": _init({"id": 888, "first_name": "Заявитель"})}


@pytest.fixture()
def client(monkeypatch):
    # Каждый тест получает чистую БД и чистые in-memory-состояния.
    for suffix in ("", "-wal", "-shm"):
        Path(f"{settings.db_path}{suffix}").unlink(missing_ok=True)
    mainmod._bot_check_cache = (0.0, False)
    mainmod._message_limiter.clear()
    mainmod._apply_limiter.clear()
    mainmod._booking_limiter.clear()
    mainmod._match_create_limiter.clear()
    mainmod._match_join_limiter.clear()
    mainmod._feedback_limiter.clear()
    # Не поднимаем реальные polling/reminder во время интеграционных тестов.
    monkeypatch.setattr(botmod, "polling_context", _fake_polling)
    monkeypatch.setattr(botmod, "reminder_loop", _fake_loop)

    async def _no_rules_broadcast(bot):
        return None

    monkeypatch.setattr(botmod, "ensure_rules_for_all", _no_rules_broadcast)

    with TestClient(mainmod.app) as c:
        # Заглушка отправки в Telegram: тесты не ходят в сеть.
        async def _noop_send(*args, **kwargs):
            return None

        mainmod.app.state.bot.send_message = _noop_send
        yield c


def _future(days: int, time_str: str = "19:00") -> dict:
    d = datetime.now(settings.timezone) + timedelta(days=days)
    return {"date": d.strftime("%Y-%m-%d"), "time": time_str}


def test_auth_required(client):
    assert client.get("/api/me").status_code == 401


def test_onboarding_seen_flow(client):
    me = client.get("/api/me", headers=PLAYER).json()["user"]
    assert me["onboarding_seen"] is False

    assert client.post("/api/me/onboarding", headers=PLAYER).json()["ok"] is True
    assert client.get("/api/me", headers=PLAYER).json()["user"]["onboarding_seen"] is True

    exported = client.get("/api/me/export", headers=PLAYER).json()
    assert exported["user"]["onboarding_seen"] is True


def test_coach_role_enforced(client):
    assert client.get("/api/me", headers=COACH).json()["user"]["is_coach"] is True
    r = client.post("/api/coach/trainings", headers=PLAYER, json=_future(3))
    assert r.status_code == 403


def test_multi_coach_isolation(client):
    # до добавления coach2 не тренер и не владелец
    assert client.get("/api/coach/coaches", headers=COACH2).status_code == 403
    assert client.get("/api/me", headers=COACH2).json()["user"]["is_coach"] is False

    # владелец добавляет второго тренера
    r = client.post("/api/coach/coaches", headers=COACH, json={"telegram_id": 777})
    assert r.json() == {"ok": True, "created": True, "telegram_id": 777}
    # повторно — created False
    assert client.post("/api/coach/coaches", headers=COACH, json={"telegram_id": 777}).json()["created"] is False
    # теперь coach2 — тренер, реестр доступен любому тренеру (для передачи занятий)
    assert client.get("/api/me", headers=COACH2).json()["user"]["is_coach"] is True
    assert client.get("/api/coach/coaches", headers=COACH2).status_code == 200
    # но управление реестром — только владелец/менеджер
    assert client.post("/api/coach/coaches", headers=COACH2, json={"telegram_id": 999}).status_code == 403
    ids = [c["telegram_id"] for c in client.get("/api/coach/coaches", headers=COACH).json()["coaches"]]
    assert 123 in ids and 777 in ids

    # изоляция: каждый видит только свои занятия
    t1 = client.post("/api/coach/trainings", headers=COACH, json={
        **_future(4, "15:00"), "training_type": "Тренер-1", "capacity": 2}).json()["id"]
    t2 = client.post("/api/coach/trainings", headers=COACH2, json={
        **_future(4, "16:00"), "training_type": "Тренер-2", "capacity": 2}).json()["id"]
    list1 = [t["id"] for t in client.get("/api/coach/trainings", headers=COACH).json()["trainings"]]
    list2 = [t["id"] for t in client.get("/api/coach/trainings", headers=COACH2).json()["trainings"]]
    assert t1 in list1 and t2 not in list1
    assert t2 in list2 and t1 not in list2

    # чужое занятие нельзя менять (404), но участников видно (клубный просмотр)
    assert client.delete(f"/api/coach/trainings/{t2}", headers=COACH).status_code == 404
    assert client.get(f"/api/coach/trainings/{t2}/participants", headers=COACH).status_code == 200
    assert client.patch(f"/api/coach/trainings/{t2}/reminder", headers=COACH,
                        json={"enabled": True, "offset_min": 270}).status_code == 404
    # своё — можно
    assert client.delete(f"/api/coach/trainings/{t1}", headers=COACH).json()["ok"]

    # игрок видит занятия обоих тренеров
    player = client.get("/api/trainings?days=31", headers=PLAYER).json()["trainings"]
    assert any(t["id"] == t2 for t in player)

    # владельца снять нельзя
    assert client.delete("/api/coach/coaches/123", headers=COACH).status_code == 400


def test_message_routes_to_training_coach(client):
    assert client.post(
        "/api/coach/coaches", headers=COACH, json={"telegram_id": 777}
    ).json()["ok"]
    tid = client.post("/api/coach/trainings", headers=COACH2, json={
        **_future(7, "11:00"), "training_type": "Сообщение", "capacity": 2}).json()["id"]
    captured = {}

    async def fake_send(chat_id, text, **kw):
        captured["to"] = chat_id

    mainmod.app.state.bot.send_message = fake_send
    r = client.post("/api/messages/coach", headers=PLAYER,
                    json={"text": "Здравствуйте", "training_id": tid})
    assert r.json()["ok"] is True
    for _ in range(20):
        if "to" in captured:
            break
        time.sleep(0.05)
    assert captured.get("to") == 777  # тренер этого занятия, а не владелец


def test_levels_reference(client):
    levels = client.get("/api/levels", headers=PLAYER).json()["levels"]
    assert levels == list(TRAINING_LEVELS)


def test_create_duplicate_and_booking(client):
    body = {**_future(3, "18:00"), "training_type": "Группа", "capacity": 2, "level": TRAINING_LEVELS[0]}
    tid = client.post("/api/coach/trainings", headers=COACH, json=body).json()["id"]
    # дубль -> 409, с force -> создаётся
    assert client.post("/api/coach/trainings", headers=COACH, json=body).status_code == 409
    assert client.post("/api/coach/trainings", headers=COACH, json={**body, "force": True}).json()["ok"]

    assert client.post(f"/api/trainings/{tid}/book", headers=PLAYER).json()["status"] == "booked"
    assert client.post(f"/api/trainings/{tid}/book", headers=PLAYER2).json()["status"] == "booked"

    card = next(
        t for t in client.get("/api/trainings?days=31", headers=PLAYER).json()["trainings"]
        if t["id"] == tid
    )
    assert card["level"] == TRAINING_LEVELS[0] and card["is_booked"] is True
    assert card["coach"] and "links" not in card["coach"]


def test_create_trainings_by_weekdays(client):
    start = (datetime.now(settings.timezone) + timedelta(days=7)).date()
    end = start + timedelta(days=6)
    payload = {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "weekdays": [0, 2, 4],  # Пн / Ср / Пт
        "time": "19:00",
        "training_type": "Пакет Пн-Ср-Пт",
        "capacity": 2,
    }
    response = client.post(
        "/api/coach/trainings/batch", headers=COACH, json=payload
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["created_count"] == 3
    assert data["skipped_count"] == 0
    assert len(set(data["created"])) == 3

    expected_dates = {
        (start + timedelta(days=offset)).isoformat()
        for offset in range(7)
        if (start + timedelta(days=offset)).weekday() in {0, 2, 4}
    }
    trainings = client.get("/api/coach/trainings", headers=COACH).json()["trainings"]
    created = [
        t for t in trainings
        if t["id"] in data["created"] and t["type"] == payload["training_type"]
    ]
    assert {t["date"] for t in created} == expected_dates
    assert all(t["time"] == "19:00" for t in created)

    # Повторный запуск идемпотентен: существующие даты пропускаются.
    again = client.post(
        "/api/coach/trainings/batch", headers=COACH, json=payload
    ).json()
    assert again["created_count"] == 0
    assert again["skipped_count"] == 3
    assert {item["date"] for item in again["skipped"]} == expected_dates

    # Игрок не может создавать пакет.
    assert client.post(
        "/api/coach/trainings/batch", headers=PLAYER, json=payload
    ).status_code == 403


def test_create_trainings_batch_validation(client):
    start = (datetime.now(settings.timezone) + timedelta(days=7)).date()
    base = {
        "period_start": start.isoformat(),
        "period_end": (start + timedelta(days=6)).isoformat(),
        "weekdays": [0],
        "time": "19:00",
        "training_type": "Пакет",
    }
    assert client.post(
        "/api/coach/trainings/batch",
        headers=COACH,
        json={**base, "weekdays": [7]},
    ).status_code == 422
    assert client.post(
        "/api/coach/trainings/batch",
        headers=COACH,
        json={**base, "period_end": (start - timedelta(days=1)).isoformat()},
    ).status_code == 422
    assert client.post(
        "/api/coach/trainings/batch",
        headers=COACH,
        json={**base, "period_end": (start + timedelta(days=100)).isoformat()},
    ).status_code == 422


def test_cancel_rule(client):
    # ближе 4.5 ч — отмена запрещена
    soon = datetime.now(settings.timezone) + timedelta(hours=1)
    tid = client.post("/api/coach/trainings", headers=COACH, json={
        "date": soon.strftime("%Y-%m-%d"), "time": soon.strftime("%H:%M"),
        "training_type": "Скоро", "capacity": 2}).json()["id"]
    client.post(f"/api/trainings/{tid}/book", headers=PLAYER)
    assert client.post(f"/api/trainings/{tid}/cancel", headers=PLAYER).status_code == 403


def test_soft_delete_and_restore(client):
    tid = client.post("/api/coach/trainings", headers=COACH, json={
        **_future(5, "20:00"), "training_type": "Удаляемая", "capacity": 2}).json()["id"]
    client.post(f"/api/trainings/{tid}/book", headers=PLAYER)

    assert client.delete(f"/api/coach/trainings/{tid}", headers=COACH).json()["restorable"] is True
    assert tid not in [t["id"] for t in client.get("/api/coach/trainings", headers=COACH).json()["trainings"]]
    assert tid in [t["id"] for t in client.get("/api/coach/deleted", headers=COACH).json()["trainings"]]
    assert all(t["id"] != tid for t in client.get("/api/trainings?days=31", headers=PLAYER).json()["trainings"])

    assert client.post(f"/api/coach/trainings/{tid}/restore", headers=COACH).json()["ok"]
    bookers = [p for p in client.get(f"/api/coach/trainings/{tid}/participants", headers=COACH).json()["participants"]
               if p["status"] == "booked"]
    assert len(bookers) == 1  # записи сохранились


def test_calendar_ics(client):
    tid = client.post("/api/coach/trainings", headers=COACH, json={
        **_future(6, "12:00"), "training_type": "Календарь", "capacity": 2, "duration_min": 60}).json()["id"]
    assert client.get(f"/calendar/{tid}.ics").status_code == 401
    r = client.get(f"/calendar/{tid}.ics", params={"initData": PLAYER["X-Init-Data"]})
    assert r.status_code == 200 and "text/calendar" in r.headers["content-type"]
    assert "BEGIN:VCALENDAR" in r.text and "DTSTART:" in r.text
    assert r.headers["content-disposition"].endswith(".ics\"")


def test_coach_profile_bio_only(client):
    r = client.put("/api/coach/profile", headers=COACH,
                   json={"bio": "Тренер с опытом", "links": [{"label": "x", "url": "https://x.ru"}]})
    prof = r.json()["profile"]
    assert prof["bio"] == "Тренер с опытом" and "links" not in prof


def test_message_endpoint_requires_auth(client):
    assert client.post("/api/messages/coach", json={"text": "hi"}).status_code == 401
    assert client.post("/api/messages/coach", headers=PLAYER, json={"text": "   "}).status_code == 422


def test_pages_render(client):
    assert client.get("/").status_code == 200
    assert client.get("/coach").status_code == 200
    assert client.get("/healthz").json()["ok"] is True


def test_apply_and_approve_flow(client):
    # обычный пользователь может подать заявку
    assert client.get("/api/me", headers=APPLICANT).json()["user"]["is_coach"] is False
    assert client.post("/api/coach/apply", headers=APPLICANT,
                       json={"message": "Хочу тренировать сквош"}).json()["ok"]
    status = client.get("/api/coach/apply", headers=APPLICANT).json()
    assert status["application"]["status"] == "pending" and status["is_coach"] is False

    # заявку видит владелец, не видит посторонний тренер без прав
    apps = client.get("/api/coach/applications", headers=COACH).json()["applications"]
    assert any(a["telegram_id"] == 888 for a in apps)

    # одобрение владельцем -> пользователь становится тренером
    assert client.post("/api/coach/applications/888/approve", headers=COACH).json()["ok"]
    assert client.get("/api/me", headers=APPLICANT).json()["user"]["is_coach"] is True
    assert client.post("/api/coach/trainings", headers=APPLICANT, json={
        **_future(9, "10:00"), "training_type": "Новичка", "capacity": 2}).json()["ok"]


def test_manager_rights_owner_only(client):
    # оба тренера должны существовать в чистой БД
    for tg_id in (777, 888):
        assert client.post(
            "/api/coach/coaches", headers=COACH, json={"telegram_id": tg_id}
        ).json()["ok"]
    # выдать права менеджера может только владелец
    assert client.patch("/api/coach/coaches/777/manager", headers=COACH,
                        json={"enabled": True}).json()["ok"]
    # менеджер видит заявки
    assert client.get("/api/coach/applications", headers=COACH2).status_code == 200
    # но менять права менеджера — только владелец
    assert client.patch("/api/coach/coaches/888/manager", headers=COACH2,
                        json={"enabled": True}).status_code == 403


def test_club_readonly_and_transfer(client):
    assert client.post(
        "/api/coach/coaches", headers=COACH, json={"telegram_id": 777}
    ).json()["ok"]
    # занятие владельца
    t = client.post("/api/coach/trainings", headers=COACH, json={
        **_future(10, "18:00"), "training_type": "Владельца", "capacity": 2}).json()["id"]

    # другой тренер видит его в «Клубе» и может читать участников
    club = [x["id"] for x in client.get("/api/coach/club/trainings", headers=COACH2).json()["trainings"]]
    assert t in club
    assert client.get(f"/api/coach/trainings/{t}/participants", headers=COACH2).status_code == 200
    # но не может менять чужое
    assert client.patch(f"/api/coach/trainings/{t}/reminder", headers=COACH2,
                        json={"enabled": True, "offset_min": 270}).status_code == 404

    # передача занятия владельцем другому тренеру
    assert client.post(f"/api/coach/trainings/{t}/transfer", headers=COACH,
                       json={"telegram_id": 777}).json()["ok"]
    assert t in [x["id"] for x in client.get("/api/coach/trainings", headers=COACH2).json()["trainings"]]
    assert t not in [x["id"] for x in client.get("/api/coach/trainings", headers=COACH).json()["trainings"]]

    # тренер не может передать чужое занятие, которое ему не принадлежит
    t2 = client.post("/api/coach/trainings", headers=COACH, json={
        **_future(11, "18:00"), "training_type": "Ещё", "capacity": 2}).json()["id"]
    assert client.post(f"/api/coach/trainings/{t2}/transfer", headers=COACH2,
                       json={"telegram_id": 888}).status_code == 404


def test_webhook_disabled_in_polling(client):
    """В polling-режиме маршрут вебхука не должен принимать апдейты."""
    r = client.post("/telegram/webhook", json={"update_id": 1})
    assert r.status_code == 404


def test_webhook_requires_secret_when_enabled(client, monkeypatch):
    """В webhook-режиме без правильного секрета апдейты не принимаются."""
    from dataclasses import replace

    secret = "a" * 32
    monkeypatch.setattr(
        mainmod,
        "settings",
        replace(
            settings,
            bot_mode="webhook",
            webhook_base_url="https://example.com",
            webhook_secret=secret,
        ),
    )
    # Валидный update без подходящего хендлера: проверяем только секрет.
    update = {
        "update_id": 1,
        "callback_query": {
            "id": "cb",
            "from": {"id": 123, "is_bot": False, "first_name": "Тренер"},
            "chat_instance": "ci",
            "data": "unknown",
        },
    }
    assert client.post("/telegram/webhook", json=update).status_code == 403
    assert client.post(
        "/telegram/webhook",
        json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    ).status_code == 403
    r = client.post(
        "/telegram/webhook",
        json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": secret},
    )
    assert r.status_code == 200 and r.json()["ok"] is True


def test_booking_is_idempotent(client):
    """Повторный /book не должен превращать booked в waitlist и менять очередь."""
    player3 = {"X-Init-Data": _init({"id": 557, "first_name": "Пётр"})}
    body = {
        **_future(12, "14:00"),
        "training_type": "Идемпотентность",
        "capacity": 1,
    }
    tid = client.post("/api/coach/trainings", headers=COACH, json=body).json()["id"]

    # первый игрок занимает единственное место; повторный вызов — тот же статус
    assert client.post(f"/api/trainings/{tid}/book", headers=PLAYER).json()["status"] == "booked"
    assert client.post(f"/api/trainings/{tid}/book", headers=PLAYER).json()["status"] == "booked"

    # второй и третий — в очередь
    assert client.post(f"/api/trainings/{tid}/book", headers=PLAYER2).json()["status"] == "waitlist"
    assert client.post(f"/api/trainings/{tid}/book", headers=player3).json()["status"] == "waitlist"

    # повторная запись второго не двигает его в конец очереди
    assert client.post(f"/api/trainings/{tid}/book", headers=PLAYER2).json()["status"] == "waitlist"

    participants = client.get(
        f"/api/coach/trainings/{tid}/participants", headers=COACH
    ).json()["participants"]
    assert [(p["telegram_id"], p["status"]) for p in participants] == [
        (555, "booked"),
        (556, "waitlist"),
        (557, "waitlist"),
    ]


def test_avatar_requires_scoped_token(client):
    """<img> не может передать X-Init-Data, поэтому аватар авторизуется токеном."""
    avatar_url = client.get("/api/me", headers=PLAYER).json()["user"]["avatar_url"]
    assert "token=" in avatar_url

    # без токена — 401 (именно так раньше ломались все аватарки)
    assert client.get("/api/avatar/555").status_code == 401

    # с токеном авторизация проходит; файла в тестовой БД нет -> 404, а не 401
    assert client.get(avatar_url).status_code == 404

    # подделанный токен не принимается
    tampered = avatar_url[:-1] + ("0" if avatar_url[-1] != "0" else "1")
    assert client.get(tampered).status_code == 401


def test_calendar_uses_scoped_token(client):
    tid = client.post("/api/coach/trainings", headers=COACH, json={
        **_future(13, "16:00"), "training_type": "Календарь-токен", "capacity": 2}).json()["id"]
    card = next(
        t for t in client.get("/api/trainings?days=31", headers=PLAYER).json()["trainings"]
        if t["id"] == tid
    )
    assert card["ics_token"]

    # Ссылка-скачивание без заголовка X-Init-Data.
    r = client.get(f"/calendar/{tid}.ics", params={"token": card["ics_token"]})
    assert r.status_code == 200 and "BEGIN:VCALENDAR" in r.text

    # Обычный заголовок тоже продолжает работать (для API-клиентов).
    assert client.get(f"/calendar/{tid}.ics", headers=PLAYER).status_code == 200

    # Чужой/подделанный токен не подходит.
    assert client.get(f"/calendar/{tid}.ics", params={"token": card["ics_token"] + "x"}).status_code == 401
    assert client.get("/calendar/999999.ics", params={"token": card["ics_token"]}).status_code == 401


def test_calendar_dev_query_disabled_when_flag_false(client, monkeypatch):
    """Полный initData в query разрешён только при ALLOW_DEV_INIT_DATA=true."""
    from dataclasses import replace

    tid = client.post("/api/coach/trainings", headers=COACH, json={
        **_future(14, "16:00"), "training_type": "Календарь-dev", "capacity": 2}).json()["id"]

    monkeypatch.setattr(mainmod, "settings", replace(settings, allow_dev_init_data=False))
    r = client.get(f"/calendar/{tid}.ics", params={"initData": PLAYER["X-Init-Data"]})
    assert r.status_code == 401

    # Узкий токен продолжает работать и при выключенном dev-флаге.
    card = next(
        t for t in client.get("/api/trainings?days=31", headers=PLAYER).json()["trainings"]
        if t["id"] == tid
    )
    assert client.get(
        f"/calendar/{tid}.ics", params={"token": card["ics_token"]}
    ).status_code == 200


def _create_training(client, *, day: int, time_str: str, **extra) -> int:
    body = {
        **_future(day, time_str),
        "training_type": f"Тест-{day}-{time_str}",
        "capacity": 2,
        **extra,
    }
    response = client.post("/api/coach/trainings", headers=COACH, json=body)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_coach_api_includes_waitlist_count(client):
    tid = _create_training(client, day=15, time_str="10:00", capacity=1)
    client.post(f"/api/trainings/{tid}/book", headers=PLAYER)
    client.post(f"/api/trainings/{tid}/book", headers=PLAYER2)

    row = next(
        t for t in client.get("/api/coach/trainings", headers=COACH).json()["trainings"]
        if t["id"] == tid
    )
    assert row["waitlist_count"] == 1


def test_deleted_training_rejects_mutations(client):
    tid = _create_training(client, day=16, time_str="11:00", capacity=2)
    client.post(f"/api/trainings/{tid}/book", headers=PLAYER)
    assert client.delete(f"/api/coach/trainings/{tid}", headers=COACH).status_code == 200

    assert client.post(f"/api/trainings/{tid}/book", headers=PLAYER2).status_code == 404
    assert client.post(f"/api/trainings/{tid}/cancel", headers=PLAYER).status_code == 404
    assert client.patch(
        f"/api/coach/trainings/{tid}/reminder",
        headers=COACH,
        json={"enabled": True, "offset_min": 270},
    ).status_code == 404
    assert client.post(
        f"/api/coach/trainings/{tid}/transfer",
        headers=COACH,
        json={"telegram_id": 123},
    ).status_code == 404
    assert client.post(
        f"/api/coach/trainings/{tid}/participants/555/cancel", headers=COACH
    ).status_code == 404
    assert client.post(
        f"/api/coach/trainings/{tid}/participants/555/restore", headers=COACH
    ).status_code == 404


def test_cancel_requires_existing_booking(client):
    tid = _create_training(client, day=16, time_str="12:00")
    assert client.post(f"/api/trainings/{tid}/cancel", headers=PLAYER).status_code == 400


def test_restore_does_not_jump_waitlist(client):
    tid = _create_training(client, day=16, time_str="13:00", capacity=1)
    assert client.post(f"/api/trainings/{tid}/book", headers=PLAYER).json()["status"] == "booked"
    assert client.post(f"/api/trainings/{tid}/book", headers=PLAYER2).json()["status"] == "waitlist"

    # PLAYER снят -> PLAYER2 повышен; возврат PLAYER не должен обгонять очередь.
    assert client.post(
        f"/api/coach/trainings/{tid}/participants/555/cancel", headers=COACH
    ).json()["promoted"] == "556"
    assert client.post(
        f"/api/coach/trainings/{tid}/participants/555/restore", headers=COACH
    ).json()["status"] == "waitlist"

    participants = client.get(
        f"/api/coach/trainings/{tid}/participants", headers=COACH
    ).json()["participants"]
    assert [(p["telegram_id"], p["status"]) for p in participants] == [
        (556, "booked"),
        (555, "waitlist"),
    ]


def test_demoted_coach_returns_to_crm(client):
    assert client.post(
        "/api/coach/coaches", headers=COACH, json={"telegram_id": 777}
    ).json()["ok"]
    # Тренер должен хотя бы раз открыть приложение, чтобы появилась карточка users.
    assert client.get("/api/me", headers=COACH2).json()["user"]["is_coach"] is True
    players = client.get("/api/coach/players", headers=COACH).json()["players"]
    assert all(p["telegram_id"] != 777 for p in players)

    assert client.delete("/api/coach/coaches/777", headers=COACH).status_code == 200
    players = client.get("/api/coach/players", headers=COACH).json()["players"]
    assert any(p["telegram_id"] == 777 for p in players)
    assert client.get("/api/me", headers=COACH2).json()["user"]["is_coach"] is False


def test_invalid_date_and_time_return_422(client):
    body = {
        **_future(16, "14:00"),
        "training_type": "Некорректная дата",
        "date": "2026-02-31",
    }
    assert client.post("/api/coach/trainings", headers=COACH, json=body).status_code == 422

    body = {**_future(16, "14:00"), "training_type": "Некорректное время", "time": "25:00"}
    assert client.post("/api/coach/trainings", headers=COACH, json=body).status_code == 422


def test_readyz_docs_and_security_headers(client, monkeypatch):
    async def fake_get_me():
        return object()

    monkeypatch.setattr(mainmod.app.state.bot, "get_me", fake_get_me)
    mainmod._bot_check_cache = (0.0, False)

    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404

    page = client.get("/")
    assert page.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors" in page.headers["content-security-policy"]
    assert "cdn.tailwindcss.com" not in page.text


def test_notifications_on_promotion_and_deletion(client):
    captured: list[tuple[int, str]] = []

    async def fake_send(chat_id, text, **kwargs):
        captured.append((chat_id, text))

    mainmod.app.state.bot.send_message = fake_send

    tid = _create_training(client, day=17, time_str="15:00", capacity=1)
    client.post(f"/api/trainings/{tid}/book", headers=PLAYER)
    client.post(f"/api/trainings/{tid}/book", headers=PLAYER2)
    client.post(f"/api/trainings/{tid}/cancel", headers=PLAYER)

    for _ in range(30):
        if any(chat_id == 556 and "Освободилось место" in text for chat_id, text in captured):
            break
        time.sleep(0.05)
    assert any(chat_id == 556 and "Освободилось место" in text for chat_id, text in captured)

    tid2 = _create_training(client, day=17, time_str="16:00", capacity=2)
    client.post(f"/api/trainings/{tid2}/book", headers=PLAYER)
    client.delete(f"/api/coach/trainings/{tid2}", headers=COACH)

    for _ in range(30):
        if any(chat_id == 555 and "отменена" in text for chat_id, text in captured):
            break
        time.sleep(0.05)
    assert any(chat_id == 555 and "отменена" in text for chat_id, text in captured)


def test_rate_limit_on_apply(client):
    applicant = {"X-Init-Data": _init({"id": 559, "first_name": "Спамер"})}
    for _ in range(5):
        assert client.post(
            "/api/coach/apply", headers=applicant, json={"message": "хочу"}
        ).status_code == 200
    r = client.post("/api/coach/apply", headers=applicant, json={"message": "ещё"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_export_and_delete_account(client):
    tid = _create_training(client, day=19, time_str="18:00", capacity=2)
    client.post(f"/api/trainings/{tid}/book", headers=PLAYER)
    client.post(
        "/api/messages/coach", headers=PLAYER, json={"text": "Моё сообщение"}
    )

    exported = client.get("/api/me/export", headers=PLAYER).json()
    assert exported["user"]["telegram_id"] == 555
    assert any(b["training_id"] == tid for b in exported["bookings"])
    assert any(m["text"] == "Моё сообщение" for m in exported["messages"])

    assert client.delete("/api/me", headers=PLAYER).status_code == 400
    assert client.delete("/api/me?confirm=true", headers=PLAYER).status_code == 200

    players = client.get("/api/coach/players", headers=COACH).json()["players"]
    assert all(p["telegram_id"] != 555 for p in players)

    # Повторный вход создаёт новую карточку, но старые данные уже удалены.
    fresh = client.get("/api/me/export", headers=PLAYER).json()
    assert fresh["messages"] == []
    assert fresh["bookings"] == []


def test_coach_cannot_delete_account(client):
    assert client.delete("/api/me?confirm=true", headers=COACH).status_code == 403


def test_delete_account_promotes_waitlist(client):
    tid = _create_training(client, day=20, time_str="12:00", capacity=1)
    client.post(f"/api/trainings/{tid}/book", headers=PLAYER)
    client.post(f"/api/trainings/{tid}/book", headers=PLAYER2)

    assert client.delete("/api/me?confirm=true", headers=PLAYER).status_code == 200
    participants = client.get(
        f"/api/coach/trainings/{tid}/participants", headers=COACH
    ).json()["participants"]
    assert [(p["telegram_id"], p["status"]) for p in participants] == [
        (556, "booked")
    ]


def test_ics_has_real_booked_count_and_valid_folding(client):
    long_type = "Очень длинное название тренировки " * 4
    tid = _create_training(
        client, day=18, time_str="17:00", training_type=long_type[:100]
    )
    client.post(f"/api/trainings/{tid}/book", headers=PLAYER)
    card = next(
        t for t in client.get("/api/trainings?days=31", headers=PLAYER).json()["trainings"]
        if t["id"] == tid
    )
    response = client.get(
        f"/calendar/{tid}.ics", params={"token": card["ics_token"]}
    )
    assert response.status_code == 200
    text = response.text
    unfolded = text.replace("\r\n ", "")
    assert "записано 1 из 2" in unfolded
    for line in text.split("\r\n"):
        if line:
            assert len(line.encode("utf-8")) <= 75


def test_telegram_webapp_script_is_served_locally(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "/static/telegram-web-app.js" in page.text
    assert "https://telegram.org/js/telegram-web-app.js" not in page.text

    script = client.get("/static/telegram-web-app.js")
    assert script.status_code == 200
    assert "WebView" in script.text[:200]
    assert "max-age=" in script.headers["cache-control"]


def test_player_privacy_about_and_animal_participant(client):
    # Игрок 555 приходит с username, но по умолчанию username скрыт.
    headers = {"X-Init-Data": _init({"id": 555, "first_name": "Иван", "username": "ivan"})}
    assert client.get("/api/me", headers=headers).status_code == 200

    players = client.get("/api/coach/players", headers=COACH).json()["players"]
    player = next(p for p in players if p["telegram_id"] == 555)
    assert player["username"] is None
    assert player["username_visible"] is False
    assert player["about"] == ""

    # Игрок заполняет «о себе» и выбирает анонимного животного.
    r = client.put(
        "/api/me/profile",
        headers=headers,
        json={
            "about": "Люблю подачу и игру у стены",
            "display_mode": "animal",
            "show_username": True,
        },
    )
    assert r.status_code == 200
    me = client.get("/api/me", headers=headers).json()["user"]
    assert me["about"] == "Люблю подачу и игру у стены"
    assert me["display_mode"] == "animal"
    assert me["show_username"] is True
    animal = me["animal"]
    assert animal["emoji"] and animal["name"]

    # Тренер видит username только после согласия и видит «о себе».
    # Имя/фото в режиме «животное» скрыто и от тренера.
    players = client.get("/api/coach/players", headers=COACH).json()["players"]
    player = next(p for p in players if p["telegram_id"] == 555)
    assert player["username"] == "ivan"
    assert player["username_visible"] is True
    assert player["about"] == "Люблю подачу и игру у стены"
    assert player["first_name"] == animal["name"]
    assert player["avatar_emoji"] == animal["emoji"]
    assert player["avatar_url"] == ""
    assert player["anonymous"] is True

    # На занятии и другой игрок, и тренер видят только животное.
    tid = _create_training(client, day=20, time_str="18:00")
    assert client.post(f"/api/trainings/{tid}/book", headers=headers).status_code == 200
    other = client.get("/api/trainings?days=31", headers=PLAYER2).json()["trainings"]
    card = next(t for t in other if t["id"] == tid)
    participant = next(p for p in card["participants"] if p["first_name"] == animal["name"])
    assert participant["anonymous"] is True
    assert participant["avatar_emoji"] == animal["emoji"]
    assert participant["avatar_url"] == ""
    assert "telegram_id" not in participant
    assert "username" not in participant

    coach_view = client.get("/api/trainings?days=31", headers=COACH).json()["trainings"]
    coach_card = next(t for t in coach_view if t["id"] == tid)
    coach_participant = next(
        p for p in coach_card["participants"] if p["first_name"] == animal["name"]
    )
    assert coach_participant["anonymous"] is True
    assert coach_participant["avatar_url"] == ""

    # Отдельный endpoint участников (его использует панель тренера) тоже
    # не должен раскрывать реальное имя/фото в режиме животного.
    participant_endpoint = client.get(
        f"/api/coach/trainings/{tid}/participants", headers=COACH
    ).json()["participants"]
    endpoint_player = next(
        p for p in participant_endpoint if p["telegram_id"] == 555
    )
    assert endpoint_player["first_name"] == animal["name"]
    assert endpoint_player["avatar_emoji"] == animal["emoji"]
    assert endpoint_player["avatar_url"] == ""
    assert endpoint_player["anonymous"] is True

    # В обычном режиме тренер снова видит реальное имя и аватарку.
    assert client.put(
        "/api/me/profile",
        headers=headers,
        json={
            "about": "Люблю подачу и игру у стены",
            "display_mode": "name",
            "show_username": True,
        },
    ).status_code == 200
    coach_view = client.get("/api/trainings?days=31", headers=COACH).json()["trainings"]
    coach_card = next(t for t in coach_view if t["id"] == tid)
    real = next(p for p in coach_card["participants"] if p["first_name"] == "Иван")
    assert real["anonymous"] is False
    assert real["avatar_url"].startswith("/api/avatar/555?token=")


def test_avatar_endpoint_hides_photo_in_animal_mode(client):
    headers = {"X-Init-Data": _init({"id": 555, "first_name": "Иван"})}
    assert client.get("/api/me", headers=headers).status_code == 200
    assert client.put(
        "/api/me/profile",
        headers=headers,
        json={"about": "", "display_mode": "animal", "show_username": False},
    ).status_code == 200

    public_token = issue_scoped_token(settings.bot_token, "avatar", 555, 555)
    response = client.get(f"/api/avatar/555?token={public_token}")
    assert response.status_code == 404
    assert response.json()["detail"] == "Аватарка скрыта."


def test_coach_message_hides_player_id(client, monkeypatch):
    sent = {}

    async def fake_send(chat_id, text, reply_markup=None):
        sent["chat_id"] = chat_id
        sent["text"] = text
        sent["reply_markup"] = reply_markup

    monkeypatch.setattr(mainmod.app.state, "bot", SimpleNamespace(send_message=fake_send))
    asyncio.run(
        mainmod._deliver_to_coach(
            123, 555, "Иван", "Не смогу прийти в субботу"
        )
    )

    assert sent["chat_id"] == 123
    assert "Иван" in sent["text"]
    assert "Не смогу прийти в субботу" in sent["text"]
    assert "555" not in sent["text"]
    assert "id:" not in sent["text"].lower()
    button = sent["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == "reply:555"


def test_inbox_hides_player_id(monkeypatch):
    rows = [
        {
            "from_user": 555,
            "sender_name": "Иван",
            "display_mode": "name",
            "text": "Можно перенести тренировку?",
            "created_at": 1,
        }
    ]

    async def fake_list_inbox(coach_id, limit):
        return rows

    monkeypatch.setattr(botmod.db, "list_inbox", fake_list_inbox)
    answers = []

    async def fake_answer(text, reply_markup=None):
        answers.append((text, reply_markup))

    fake_message = SimpleNamespace(
        from_user=SimpleNamespace(id=settings.coach_telegram_id),
        answer=fake_answer,
    )
    asyncio.run(botmod.cmd_inbox(fake_message))

    assert len(answers) == 1
    text, markup = answers[0]
    assert "Иван" in text
    assert "Можно перенести тренировку?" in text
    assert "555" not in text
    assert "/reply" not in text
    assert markup.inline_keyboard[0][0].callback_data == "reply:555"


def test_recurring_training_ui(client):
    page = client.get("/coach")
    assert page.status_code == 200
    assert 'id="create-mode"' in page.text
    assert 'name="period_start"' in page.text
    assert 'name="period_end"' in page.text
    assert 'data-weekday="0"' in page.text
    assert "api('/api/coach/trainings/batch'" in page.text
    assert "Выбрано: ${count} ${trainingWord(count)}" in page.text


def test_training_time_uses_24_hour_input(client):
    page = client.get("/coach")
    assert page.status_code == 200
    assert 'id="training-time"' in page.text
    assert 'Время · 24 ч' in page.text
    assert 'pattern="(?:[01][0-9]|2[0-3]):[0-5][0-9]"' in page.text


def test_inbox_uses_animal_name_in_anonymous_mode(monkeypatch):
    rows = [
        {
            "from_user": 555,
            "sender_name": "Иван",
            "display_mode": "animal",
            "text": "Привет",
            "created_at": 1,
        }
    ]

    async def fake_list_inbox(coach_id, limit):
        return rows

    monkeypatch.setattr(botmod.db, "list_inbox", fake_list_inbox)
    answers = []

    async def fake_answer(text, reply_markup=None):
        answers.append((text, reply_markup))

    fake_message = SimpleNamespace(
        from_user=SimpleNamespace(id=settings.coach_telegram_id),
        answer=fake_answer,
    )
    asyncio.run(botmod.cmd_inbox(fake_message))

    animal = botmod.animal_identity(555)
    text, markup = answers[0]
    assert animal["name"] in text
    assert "Иван" not in text
    assert "555" not in text
    assert markup.inline_keyboard[0][0].callback_data == "reply:555"


def test_application_privacy_hides_animal_from_managers(client, monkeypatch):
    headers = {
        "X-Init-Data": _init({
            "id": 888,
            "first_name": "Заявитель",
            "username": "applicant",
        })
    }
    assert client.get("/api/me", headers=headers).status_code == 200
    assert client.put(
        "/api/me/profile",
        headers=headers,
        json={
            "about": "Хочу тренировать",
            "display_mode": "animal",
            "show_username": True,
        },
    ).status_code == 200

    sent: list[tuple[int, str]] = []

    async def fake_send(chat_id, text, **kwargs):
        sent.append((chat_id, text))

    monkeypatch.setattr(mainmod.app.state.bot, "send_message", fake_send)
    assert client.post(
        "/api/coach/apply", headers=headers, json={"message": "Заявка"}
    ).json()["ok"] is True

    animal = botmod.animal_identity(888)
    for _ in range(30):
        if sent:
            break
        time.sleep(0.05)
    assert sent
    assert animal["name"] in sent[0][1]
    assert "Заявитель" not in sent[0][1]
    assert "888" not in sent[0][1]

    applications = client.get(
        "/api/coach/applications", headers=COACH
    ).json()["applications"]
    application = next(a for a in applications if a["telegram_id"] == 888)
    assert application["first_name"] == animal["name"]
    assert application["avatar_emoji"] == animal["emoji"]
    assert application["avatar_url"] == ""
    assert application["anonymous"] is True
    assert application["username"] == "applicant"
    assert application["username_visible"] is True  # username виден только по согласию


def test_message_to_coach_uses_animal_name(client, monkeypatch):
    headers = {"X-Init-Data": _init({"id": 555, "first_name": "Иван"})}
    assert client.get("/api/me", headers=headers).status_code == 200
    assert client.put(
        "/api/me/profile",
        headers=headers,
        json={"about": "", "display_mode": "animal", "show_username": False},
    ).status_code == 200
    tid = _create_training(client, day=21, time_str="11:00")

    sent: list[tuple[int, str]] = []

    async def fake_send(chat_id, text, **kwargs):
        sent.append((chat_id, text))

    monkeypatch.setattr(mainmod.app.state.bot, "send_message", fake_send)
    assert client.post(
        "/api/messages/coach",
        headers=headers,
        json={"text": "Не смогу прийти", "training_id": tid},
    ).json()["ok"] is True

    animal = botmod.animal_identity(555)
    for _ in range(30):
        if sent:
            break
        time.sleep(0.05)
    assert sent
    assert animal["name"] in sent[0][1]
    assert "Иван" not in sent[0][1]
    assert "555" not in sent[0][1]


def test_refresh_and_responsive_ui_contract(client):
    player = client.get("/")
    coach = client.get("/coach")
    assert player.status_code == 200 and coach.status_code == 200

    for page in (player, coach):
        assert 'id="refresh-btn"' in page.text
        assert 'aria-label="Обновить данные"' in page.text
        assert 'window.PAGE_REFRESH' in page.text
        assert "APP.refresh()" in page.text
        assert "maximum-scale" not in page.text  # зум не блокируем
        assert "max-width: 340px" in page.text
        assert "max-width: 220px" in page.text
        assert "prefers-reduced-motion" in page.text
        assert "squash-loader__ball" in page.text
        assert 'id="app-loader"' in page.text
        assert "hideAppLoader" in page.text

    assert "TRAINING_QUOTES" in player.text
    assert "Nicol David" in player.text
    assert 'id="training-quote"' in player.text

    # Обновление расписания должно убирать старые карточки и empty-state.
    assert "Object.keys(ITEMS).forEach" in player.text
    assert "schedule.innerHTML = ''" in player.text
    assert "empty.hidden = true" in player.text
    # Контейнер аватара сохраняет id после повторного рендера.
    assert "'me-avatar'" in player.text
    # Первый запуск: памятка и переход к приватности.
    assert 'id="onboarding-sheet"' in player.text
    assert "Настроить приватность" in player.text
    assert "maybeShowOnboarding" in player.text
    # Обновление панели тренера перечитывает роль.
    assert "applyRoleUi" in coach.text
    # Реестр тренеров не ждёт IntersectionObserver: иначе раздел выглядит
    # пустым, пока пользователь не проскроллит в него.
    assert "whenVisible('coaches-section', loadCoaches)" not in coach.text
    assert "loadCoaches();" in coach.text
    # Заявки в режиме животного показывают emoji, а не инициал.
    assert "avatarHtml(a.first_name, a.avatar_url, true, a.avatar_emoji)" in coach.text


def test_readyz_cache_and_failures(client, monkeypatch):
    async def fake_get_me():
        return object()

    monkeypatch.setattr(mainmod.app.state.bot, "get_me", fake_get_me)
    mainmod._bot_check_cache = (0.0, False)
    assert client.get("/readyz").status_code == 200
    assert client.get("/readyz").status_code == 200  # cached branch

    async def failing_get_me():
        raise RuntimeError("telegram down")

    monkeypatch.setattr(mainmod.app.state.bot, "get_me", failing_get_me)
    mainmod._bot_check_cache = (0.0, False)
    assert client.get("/readyz").status_code == 503

    async def db_down():
        return False

    monkeypatch.setattr(mainmod.db, "ping", db_down)
    assert client.get("/readyz").status_code == 503


def test_store_avatar_evicts_expired_and_oldest(monkeypatch):
    mainmod._avatar_cache.clear()
    now = time.time()
    mainmod._avatar_cache[1] = (now - mainmod._AVATAR_TTL_SECONDS - 1, b"old", "image/png")
    for key in range(100, 100 + mainmod._AVATAR_CACHE_MAX_ENTRIES):
        mainmod._avatar_cache[key] = (now, b"", "image/png")
    mainmod._store_avatar(999, b"x", "image/png")
    assert 1 not in mainmod._avatar_cache
    assert 999 in mainmod._avatar_cache
    assert len(mainmod._avatar_cache) <= mainmod._AVATAR_CACHE_MAX_ENTRIES
    mainmod._avatar_cache.clear()


def test_auth_rejects_bad_telegram_id(client):
    headers = {"X-Init-Data": _init({"id": "not-an-int", "first_name": "X"})}
    response = client.get("/api/me", headers=headers)
    assert response.status_code == 401


def test_authenticate_missing_profile_after_upsert(monkeypatch):
    async def no_upsert(**kwargs):
        return None

    async def no_user(telegram_id):
        return None

    async def not_coach(telegram_id):
        return False

    monkeypatch.setattr(mainmod.db, "upsert_user", no_upsert)
    monkeypatch.setattr(mainmod.db, "get_user", no_user)
    monkeypatch.setattr(mainmod.db, "is_coach_id", not_coach)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(mainmod._authenticate(PLAYER["X-Init-Data"]))
    assert exc.value.status_code == 500


def test_serialize_coach_none():
    assert mainmod.serialize_coach(None) is None


def test_player_profile_missing_and_errors(monkeypatch):
    payload = mainmod.PlayerProfileIn(
        about="", display_mode="name", show_username=False
    )
    user = {"telegram_id": 555}

    async def no_set(*args, **kwargs):
        return None

    async def no_user(telegram_id):
        return None

    monkeypatch.setattr(mainmod.db, "set_player_profile", no_set)
    monkeypatch.setattr(mainmod.db, "get_user", no_user)
    with pytest.raises(HTTPException):
        asyncio.run(mainmod.api_set_player_profile(payload, user))

    async def raise_http(*args, **kwargs):
        raise HTTPException(status_code=400, detail="bad")

    monkeypatch.setattr(mainmod.db, "set_player_profile", raise_http)
    with pytest.raises(HTTPException):
        asyncio.run(mainmod.api_set_player_profile(payload, user))

    async def raise_generic(*args, **kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(mainmod.db, "set_player_profile", raise_generic)
    response = asyncio.run(mainmod.api_set_player_profile(payload, user))
    assert response.status_code == 500


def test_update_notifications_success(client):
    response = client.put(
        "/api/me/notifications",
        headers=PLAYER,
        json={
            "enabled": False,
            "notify_matches": False,
            "notify_trainings": False,
            "notify_messages": False,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["notifications_enabled"] is False
    assert body["notify_matches"] is False
    assert body["notify_trainings"] is False
    assert body["notify_messages"] is False

    # Частичное обновление не сбрасывает остальные флаги.
    partial = client.put(
        "/api/me/notifications",
        headers=PLAYER,
        json={"notify_messages": True},
    ).json()
    assert partial["notifications_enabled"] is False
    assert partial["notify_messages"] is True
    assert partial["notify_matches"] is False


def test_update_notifications_missing_profile(monkeypatch):
    async def no_set(*args, **kwargs):
        return None

    async def no_user(telegram_id):
        return None

    monkeypatch.setattr(mainmod.db, "set_notification_settings", no_set)
    monkeypatch.setattr(mainmod.db, "get_user", no_user)
    with pytest.raises(HTTPException):
        asyncio.run(
            mainmod.api_set_notifications(
                mainmod.NotificationsIn(enabled=False), {"telegram_id": 555}
            )
        )


def test_delete_account_skips_inactive_and_deleted_bookings(client, monkeypatch):
    async def fake_bookings(telegram_id):
        return [
            {"status": "cancelled", "deleted_at": None, "training_id": 1},
            {"status": "booked", "deleted_at": 1, "training_id": 2},
        ]

    async def fake_delete(telegram_id):
        return None

    monkeypatch.setattr(mainmod.db, "list_user_bookings", fake_bookings)
    monkeypatch.setattr(mainmod.db, "delete_user_data", fake_delete)
    response = asyncio.run(
        mainmod.api_delete_me({"telegram_id": 555}, confirm=True)
    )
    assert response["ok"] is True


def test_waitlist_status_is_returned_to_user(client):
    tid = _create_training(client, day=22, time_str="18:00", capacity=1)
    assert client.post(f"/api/trainings/{tid}/book", headers=PLAYER).json()["status"] == "booked"
    assert client.post(f"/api/trainings/{tid}/book", headers=PLAYER2).json()["status"] == "waitlist"
    card = next(
        t for t in client.get("/api/trainings?days=31", headers=PLAYER2).json()["trainings"]
        if t["id"] == tid
    )
    assert card["is_waitlisted"] is True


def test_book_started_and_exception(monkeypatch):
    async def past_training(training_id):
        return {"id": training_id, "deleted_at": None, "start_at": 0, "capacity": 2}

    monkeypatch.setattr(mainmod.db, "get_training", past_training)
    response = asyncio.run(mainmod.api_book(1, {"telegram_id": 555}))
    assert response.status_code == 400

    async def broken_training(training_id):
        raise RuntimeError("db")

    monkeypatch.setattr(mainmod.db, "get_training", broken_training)
    response = asyncio.run(mainmod.api_book(1, {"telegram_id": 555}))
    assert response.status_code == 500


def test_cancel_started_and_exception(monkeypatch):
    async def past_training(training_id):
        return {"id": training_id, "deleted_at": None, "start_at": 0, "capacity": 2}

    async def booked(training_id, user_id):
        return {"status": "booked"}

    monkeypatch.setattr(mainmod.db, "get_training", past_training)
    monkeypatch.setattr(mainmod.db, "get_booking", booked)
    response = asyncio.run(mainmod.api_cancel(1, {"telegram_id": 555}))
    assert response.status_code == 403

    async def broken_training(training_id):
        raise RuntimeError("db")

    monkeypatch.setattr(mainmod.db, "get_training", broken_training)
    response = asyncio.run(mainmod.api_cancel(1, {"telegram_id": 555}))
    assert response.status_code == 500


def test_calendar_auth_fallbacks_and_missing_training(client, monkeypatch):
    tid = _create_training(client, day=23, time_str="10:00")
    bad_header = client.get(
        f"/calendar/{tid}.ics", headers={"X-Init-Data": "bad"}
    )
    assert bad_header.status_code == 401

    from dataclasses import replace

    monkeypatch.setattr(
        mainmod, "settings", replace(settings, allow_dev_init_data=True)
    )
    assert client.get(
        f"/calendar/{tid}.ics", params={"initData": "bad"}
    ).status_code == 401

    token = issue_scoped_token(settings.bot_token, "ics", 999, 555)
    assert client.get(
        "/calendar/999.ics", params={"token": token}
    ).status_code == 404


def test_coach_history_and_validation_edges(client, monkeypatch):
    assert client.get("/api/coach/history", headers=COACH).status_code == 200

    past = _future(-1, "10:00")
    assert client.post(
        "/api/coach/trainings", headers=COACH, json=past
    ).status_code == 400
    assert client.post(
        "/api/coach/trainings",
        headers=COACH,
        json={**_future(24, "10:00"), "level": "Неизвестный"},
    ).status_code == 422

    async def broken_duplicate(*args, **kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(mainmod.db, "find_duplicate_training", broken_duplicate)
    assert client.post(
        "/api/coach/trainings",
        headers=COACH,
        json={**_future(24, "11:00"), "training_type": "Сломанная"},
    ).status_code == 500


def test_batch_validation_and_skip_edges(client, monkeypatch):
    start = (datetime.now(settings.timezone) + timedelta(days=7)).date()
    valid = {
        "period_start": start.isoformat(),
        "period_end": (start + timedelta(days=6)).isoformat(),
        "weekdays": [start.weekday()],
        "time": "19:00",
        "training_type": "Batch-edge",
    }
    assert client.post(
        "/api/coach/trainings/batch",
        headers=COACH,
        json={**valid, "period_start": "2026-02-31"},
    ).status_code == 422
    assert client.post(
        "/api/coach/trainings/batch",
        headers=COACH,
        json={**valid, "level": "Неизвестный"},
    ).status_code == 422

    other_day = (start.weekday() + 1) % 7
    assert client.post(
        "/api/coach/trainings/batch",
        headers=COACH,
        json={
            "period_start": start.isoformat(),
            "period_end": start.isoformat(),
            "weekdays": [other_day],
            "time": "19:00",
            "training_type": "No dates",
        },
    ).status_code == 422

    monkeypatch.setattr(mainmod, "_TRAINING_BATCH_MAX_COUNT", 0)
    assert client.post(
        "/api/coach/trainings/batch", headers=COACH, json=valid
    ).status_code == 422
    monkeypatch.setattr(mainmod, "_TRAINING_BATCH_MAX_COUNT", 100)

    yesterday = (datetime.now(settings.timezone) - timedelta(days=1)).date()
    past = client.post(
        "/api/coach/trainings/batch",
        headers=COACH,
        json={
            "period_start": yesterday.isoformat(),
            "period_end": yesterday.isoformat(),
            "weekdays": [yesterday.weekday()],
            "time": "19:00",
            "training_type": "Past batch",
        },
    )
    assert past.status_code == 200
    assert past.json()["created_count"] == 0
    assert past.json()["skipped_count"] == 1

    async def broken_duplicate(*args, **kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(mainmod.db, "find_duplicate_training", broken_duplicate)
    assert client.post(
        "/api/coach/trainings/batch",
        headers=COACH,
        json={**valid, "training_type": "Broken batch"},
    ).status_code == 500


def test_delete_restore_purge_errors(client, monkeypatch):
    tid = _create_training(client, day=25, time_str="10:00")
    async def broken_soft_delete(*args, **kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(mainmod.db, "soft_delete_training", broken_soft_delete)
    assert client.delete(
        f"/api/coach/trainings/{tid}", headers=COACH
    ).status_code == 500
    monkeypatch.undo()

    assert client.post(
        "/api/coach/trainings/999/restore", headers=COACH
    ).status_code == 404

    tid2 = _create_training(client, day=25, time_str="11:00")
    assert client.delete(f"/api/coach/trainings/{tid2}", headers=COACH).status_code == 200

    async def broken_restore(*args, **kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(mainmod.db, "restore_training", broken_restore)
    assert client.post(
        f"/api/coach/trainings/{tid2}/restore", headers=COACH
    ).status_code == 500

    tid3 = _create_training(client, day=25, time_str="12:00")
    assert client.delete(f"/api/coach/trainings/{tid3}", headers=COACH).status_code == 200
    assert client.delete(
        f"/api/coach/trainings/{tid3}/purge", headers=COACH
    ).status_code == 200
    assert client.delete(
        "/api/coach/trainings/999/purge", headers=COACH
    ).status_code == 404

    tid4 = _create_training(client, day=25, time_str="13:00")
    assert client.delete(f"/api/coach/trainings/{tid4}", headers=COACH).status_code == 200

    async def broken_purge(*args, **kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(mainmod.db, "purge_training", broken_purge)
    assert client.delete(
        f"/api/coach/trainings/{tid4}/purge", headers=COACH
    ).status_code == 500


def test_participant_management_edges(client, monkeypatch):
    tid = _create_training(client, day=26, time_str="10:00")
    assert client.get(
        "/api/coach/trainings/999/participants", headers=COACH
    ).status_code == 404

    async def broken_participants(*args, **kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(mainmod.db, "get_participants_full", broken_participants)
    assert client.get(
        f"/api/coach/trainings/{tid}/participants", headers=COACH
    ).status_code == 500

    assert client.post(
        f"/api/coach/trainings/{tid}/participants/999/cancel", headers=COACH
    ).status_code == 404

    client.post(f"/api/trainings/{tid}/book", headers=PLAYER)
    async def broken_cancel(*args, **kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(mainmod.db, "cancel_booking", broken_cancel)
    assert client.post(
        f"/api/coach/trainings/{tid}/participants/555/cancel", headers=COACH
    ).status_code == 500

    monkeypatch.undo()
    assert client.post(
        f"/api/coach/trainings/{tid}/participants/999/restore", headers=COACH
    ).status_code == 404
    assert client.post(
        f"/api/coach/trainings/{tid}/participants/555/restore", headers=COACH
    ).status_code == 400

    assert client.post(
        f"/api/coach/trainings/{tid}/participants/555/cancel", headers=COACH
    ).status_code == 200

    async def broken_restore_booking(*args, **kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(mainmod.db, "restore_booking", broken_restore_booking)
    assert client.post(
        f"/api/coach/trainings/{tid}/participants/555/restore", headers=COACH
    ).status_code == 500


def test_reminder_notes_profile_edges(client, monkeypatch):
    assert client.get("/api/me", headers=PLAYER).status_code == 200
    tid = _create_training(client, day=27, time_str="10:00")
    assert client.patch(
        f"/api/coach/trainings/{tid}/reminder",
        headers=COACH,
        json={"enabled": False, "offset_min": 60},
    ).status_code == 200

    async def broken_reminder(*args, **kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(mainmod.db, "update_reminder_settings", broken_reminder)
    assert client.patch(
        f"/api/coach/trainings/{tid}/reminder",
        headers=COACH,
        json={"enabled": True, "offset_min": 60},
    ).status_code == 500
    monkeypatch.undo()

    assert client.put(
        "/api/coach/players/555/notes", headers=COACH, json={"notes": "note"}
    ).status_code == 200
    assert client.put(
        "/api/coach/players/999/notes", headers=COACH, json={"notes": "note"}
    ).status_code == 404

    async def broken_notes(*args, **kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(mainmod.db, "set_user_notes", broken_notes)
    assert client.put(
        "/api/coach/players/555/notes", headers=COACH, json={"notes": "x"}
    ).status_code == 500
    monkeypatch.undo()

    assert client.get("/api/coach/profile", headers=COACH).status_code == 200

    async def broken_profile(*args, **kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(mainmod.db, "set_trainer_profile", broken_profile)
    assert client.put(
        "/api/coach/profile", headers=COACH, json={"bio": "x"}
    ).status_code == 500


def test_message_training_not_found_and_notification_edges(client, monkeypatch):
    assert client.post(
        "/api/messages/coach",
        headers=PLAYER,
        json={"text": "hello", "training_id": 999},
    ).status_code == 404

    async def failing_send(*args, **kwargs):
        raise RuntimeError("telegram")

    monkeypatch.setattr(mainmod.app.state.bot, "send_message", failing_send)
    asyncio.run(mainmod._deliver_to_coach(123, 555, "Иван", "text"))
    asyncio.run(mainmod._send_user_message(555, "text"))

    training = {
        "id": 1,
        "start_at": int(time.time()) + 3600,
        "training_type": "Тренировка",
        "capacity": 2,
        "created_by": 123,
        "deleted_at": None,
    }
    asyncio.run(mainmod._notify_participant_restored(555, training, "booked"))
    asyncio.run(mainmod._notify_application_decision(555, approved=True))

    sent = []

    async def capture_send(chat_id, text, **kwargs):
        sent.append((chat_id, text))

    monkeypatch.setattr(mainmod.app.state.bot, "send_message", capture_send)
    asyncio.run(mainmod._notify_application_decision(555, approved=True))
    assert sent

    from dataclasses import replace

    monkeypatch.setattr(
        mainmod, "settings", replace(settings, webhook_base_url="https://example.test")
    )
    asyncio.run(mainmod._notify_new_coach(555))
    assert sent
    monkeypatch.setattr(mainmod.app.state.bot, "send_message", failing_send)
    asyncio.run(mainmod._notify_new_coach(555))

    async def broken_managers():
        raise RuntimeError("db")

    monkeypatch.setattr(mainmod.db, "list_managers", broken_managers)
    asyncio.run(mainmod._notify_managers_about_application(555, "Иван"))

    async def empty_managers():
        return []

    monkeypatch.setattr(mainmod.db, "list_managers", empty_managers)
    monkeypatch.setattr(mainmod.app.state.bot, "send_message", failing_send)
    asyncio.run(mainmod._notify_managers_about_application(555, "Иван"))


def test_add_coach_validation_edges(client, monkeypatch):
    headers = {
        "X-Init-Data": _init({
            "id": 555,
            "first_name": "Иван",
            "username": "ivan",
        })
    }
    assert client.get("/api/me", headers=headers).status_code == 200
    assert client.post(
        "/api/coach/coaches", headers=COACH, json={"username": "ivan"}
    ).json()["created"] is True
    assert client.post(
        "/api/coach/coaches", headers=COACH, json={"username": "nobody"}
    ).status_code == 404
    assert client.post(
        "/api/coach/coaches", headers=COACH, json={}
    ).status_code == 422
    assert client.post(
        "/api/coach/coaches", headers=COACH, json={"telegram_id": 123}
    ).status_code == 400

    async def broken_add(*args, **kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(mainmod.db, "add_coach", broken_add)
    assert client.post(
        "/api/coach/coaches", headers=COACH, json={"telegram_id": 999}
    ).status_code == 500


def test_manager_apply_and_approval_edges(client, monkeypatch):
    assert client.patch(
        "/api/coach/coaches/123/manager", headers=COACH, json={"enabled": True}
    ).status_code == 400
    assert client.patch(
        "/api/coach/coaches/999/manager", headers=COACH, json={"enabled": True}
    ).status_code == 404
    assert client.post(
        "/api/coach/apply", headers=COACH, json={"message": "x"}
    ).status_code == 400

    assert client.post(
        "/api/coach/applications/999/approve", headers=COACH
    ).status_code == 404
    assert client.post(
        "/api/coach/applications/999/reject", headers=COACH
    ).status_code == 404

    assert client.post(
        "/api/coach/apply", headers=APPLICANT, json={"message": "x"}
    ).status_code == 200
    assert client.post(
        "/api/coach/applications/888/approve", headers=COACH
    ).status_code == 200
    assert client.post(
        "/api/coach/applications/888/approve", headers=COACH
    ).status_code == 409
    assert client.post(
        "/api/coach/applications/888/reject", headers=COACH
    ).status_code == 409

    applicant2 = {"X-Init-Data": _init({"id": 889, "first_name": "Второй"})}
    assert client.post(
        "/api/coach/apply", headers=applicant2, json={"message": "x"}
    ).status_code == 200
    assert client.post(
        "/api/coach/applications/889/reject", headers=COACH
    ).status_code == 200
    assert client.post(
        "/api/coach/applications/889/reject", headers=COACH
    ).status_code == 409


def test_transfer_validation_edges(client, monkeypatch):
    headers2 = {
        "X-Init-Data": _init({
            "id": 777,
            "first_name": "Второй тренер",
            "username": "coach2",
        })
    }
    assert client.get("/api/me", headers=headers2).status_code == 200
    assert client.post(
        "/api/coach/coaches", headers=COACH, json={"telegram_id": 777}
    ).json()["ok"]
    tid = _create_training(client, day=28, time_str="10:00")
    assert client.post(
        f"/api/coach/trainings/{tid}/transfer",
        headers=COACH,
        json={"username": "coach2"},
    ).status_code == 200

    tid2 = _create_training(client, day=28, time_str="11:00")
    assert client.post(
        f"/api/coach/trainings/{tid2}/transfer", headers=COACH, json={}
    ).status_code == 422
    assert client.post(
        f"/api/coach/trainings/{tid2}/transfer",
        headers=COACH,
        json={"telegram_id": 999},
    ).status_code == 400
    assert client.post(
        f"/api/coach/trainings/{tid2}/transfer",
        headers=COACH,
        json={"telegram_id": 123},
    ).status_code == 400

    async def broken_owner(*args, **kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(mainmod.db, "set_training_owner", broken_owner)
    assert client.post(
        f"/api/coach/trainings/{tid2}/transfer",
        headers=COACH,
        json={"telegram_id": 777},
    ).status_code == 500


def test_webhook_invalid_json_returns_error(client, monkeypatch):
    from dataclasses import replace

    secret = "a" * 32
    monkeypatch.setattr(
        mainmod,
        "settings",
        replace(
            settings,
            bot_mode="webhook",
            webhook_base_url="https://example.com",
            webhook_secret=secret,
        ),
    )
    response = client.post(
        "/telegram/webhook",
        content="not-json",
        headers={"X-Telegram-Bot-Api-Secret-Token": secret},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": False}


def test_lifespan_webhook_startup(tmp_path, monkeypatch):
    from dataclasses import replace

    set_webhook_calls = []
    session_closed = []

    class FakeBot:
        def __init__(self):
            self.session = SimpleNamespace(close=self._close)

        async def _close(self):
            session_closed.append(True)

        async def set_webhook(self, url, **kwargs):
            set_webhook_calls.append((url, kwargs))

    fake_bot = FakeBot()

    async def no_db(*args, **kwargs):
        return None

    async def wait_loop(bot):
        await asyncio.sleep(3600)

    monkeypatch.setattr(
        mainmod,
        "settings",
        replace(
            settings,
            bot_mode="webhook",
            webhook_base_url="https://example.test/",
            webhook_secret="a" * 32,
            db_path=tmp_path / "db.sqlite",
        ),
    )
    monkeypatch.setattr(mainmod.db, "connect", no_db)
    monkeypatch.setattr(mainmod.db, "ensure_coach", no_db)
    monkeypatch.setattr(mainmod.db, "upsert_user", no_db)
    monkeypatch.setattr(mainmod.db, "close", no_db)
    monkeypatch.setattr(mainmod.bot_module, "create_bot", lambda: fake_bot)
    monkeypatch.setattr(mainmod.bot_module, "create_dispatcher", lambda: object())
    monkeypatch.setattr(mainmod.bot_module, "reminder_loop", wait_loop)

    fake_app = SimpleNamespace(state=SimpleNamespace())

    async def scenario():
        async with mainmod.lifespan(fake_app):
            assert fake_app.state.bot is fake_bot
            mainmod._spawn(asyncio.sleep(3600))

    asyncio.run(scenario())
    assert set_webhook_calls == [
        ("https://example.test/telegram/webhook", {
            "secret_token": "a" * 32,
            "drop_pending_updates": True,
        })
    ]
    assert session_closed == [True]


def test_avatar_not_found_cache_and_download(client, monkeypatch):
    assert client.get("/api/me", headers=PLAYER).status_code == 200

    missing_token = issue_scoped_token(settings.bot_token, "avatar", 999, 999)
    assert client.get(
        "/api/avatar/999", params={"token": missing_token}
    ).status_code == 404

    mainmod._avatar_cache[555] = (
        time.time(), b"cached", "image/png"
    )
    token = issue_scoped_token(settings.bot_token, "avatar", 555, 555)
    cached_response = client.get("/api/avatar/555", params={"token": token})
    assert cached_response.status_code == 200
    assert cached_response.content == b"cached"

    mainmod._avatar_cache.pop(555, None)
    asyncio.run(mainmod.db.set_user_avatar(555, "avatar-file"))

    class FakeFile:
        file_path = "photo.jpg"

    class FakeBuffer:
        def getvalue(self):
            return b"downloaded"

    async def fake_get_file(file_id):
        return FakeFile()

    async def fake_download_file(file_path):
        return FakeBuffer()

    monkeypatch.setattr(mainmod.app.state.bot, "get_file", fake_get_file)
    monkeypatch.setattr(
        mainmod.app.state.bot, "download_file", fake_download_file
    )
    downloaded = client.get("/api/avatar/555", params={"token": token})
    assert downloaded.status_code == 200
    assert downloaded.content == b"downloaded"
    assert downloaded.headers["content-type"] == "image/jpeg"

    class NoPathFile:
        file_path = None

    async def no_path(file_id):
        return NoPathFile()

    mainmod._avatar_cache.pop(555, None)
    monkeypatch.setattr(mainmod.app.state.bot, "get_file", no_path)
    assert client.get(
        "/api/avatar/555", params={"token": token}
    ).status_code == 404

    async def broken_file(file_id):
        raise RuntimeError("telegram")

    monkeypatch.setattr(mainmod.app.state.bot, "get_file", broken_file)
    assert client.get(
        "/api/avatar/555", params={"token": token}
    ).status_code == 502
    mainmod._avatar_cache.pop(555, None)


def test_profile_descriptions_allow_450_characters(client):
    text = "я" * 450
    assert client.put(
        "/api/me/profile",
        headers=PLAYER,
        json={"about": text, "display_mode": "name", "show_username": False},
    ).status_code == 200
    assert client.get("/api/me", headers=PLAYER).json()["user"]["about"] == text

    assert client.put(
        "/api/coach/profile", headers=COACH, json={"bio": text}
    ).status_code == 200
    assert client.get("/api/coach/profile", headers=COACH).json()["profile"]["bio"] == text

    assert client.put(
        "/api/me/profile",
        headers=PLAYER,
        json={"about": text + "я", "display_mode": "name", "show_username": False},
    ).status_code == 422
    assert client.put(
        "/api/coach/profile", headers=COACH, json={"bio": text + "я"}
    ).status_code == 422


def test_description_textareas_and_full_display(client):
    player = client.get("/")
    coach = client.get("/coach")
    assert 'id="profile-about" rows="4" maxlength="450"' in player.text
    assert 'id="profile-about-count"' in player.text
    assert 'id="profile-bio" maxlength="450" rows="4"' in coach.text
    assert 'id="profile-bio-count"' in coach.text
    # В карточках больше нет двухстрочного обрезания описания.
    assert "-webkit-line-clamp" not in player.text


def test_public_player_profile_by_token(client):
    assert client.get("/api/me", headers=PLAYER2).status_code == 200
    tid = _create_training(client, day=29, time_str="10:00")
    assert client.post(f"/api/trainings/{tid}/book", headers=PLAYER).status_code == 200
    card = next(
        t for t in client.get("/api/trainings?days=31", headers=PLAYER2).json()["trainings"]
        if t["id"] == tid
    )
    participant = card["participants"][0]
    token = participant["profile_token"]
    profile = client.get(f"/api/players/{token}", headers=PLAYER2)
    assert profile.status_code == 200
    assert profile.json()["profile"]["first_name"] == "Иван"
    assert profile.json()["profile"]["about"] == ""
    assert "telegram_id" not in profile.json()["profile"]

    assert client.get(
        "/api/players/bad-token", headers=PLAYER2
    ).status_code == 401
    missing = issue_scoped_token(settings.bot_token, "profile", 0, 999)
    assert client.get(
        f"/api/players/{missing}", headers=PLAYER2
    ).status_code == 404


def test_public_player_profile_animal_mode(client):
    headers = {"X-Init-Data": _init({"id": 558, "first_name": "Скрытый"})}
    assert client.get("/api/me", headers=headers).status_code == 200
    assert client.put(
        "/api/me/profile",
        headers=headers,
        json={"about": "Люблю игру", "display_mode": "animal", "show_username": False},
    ).status_code == 200
    tid = _create_training(client, day=30, time_str="10:00")
    assert client.post(f"/api/trainings/{tid}/book", headers=headers).status_code == 200
    card = next(
        t for t in client.get("/api/trainings?days=31", headers=PLAYER2).json()["trainings"]
        if t["id"] == tid
    )
    participant = next(
        p for p in card["participants"] if p["first_name"] != "Иван"
    )
    profile = client.get(
        f"/api/players/{participant['profile_token']}", headers=PLAYER2
    ).json()["profile"]
    assert profile["anonymous"] is True
    assert profile["avatar_url"] == ""
    assert profile["avatar_emoji"]
    assert profile["about"] == "Люблю игру"
    assert "telegram_id" not in profile


def test_match_create_list_join_leave_and_delete(client, monkeypatch):
    assert client.get("/api/matches", headers=PLAYER).json()["matches"] == []
    body = {
        **_future(29, "19:00"),
        "title": "Вечерний матч",
        "description": "Ищем игрока",
        "location": "Корт 2",
        "duration_min": 60,
        "max_players": 2,
        "level": TRAINING_LEVELS[0],
    }
    created = client.post("/api/matches", headers=PLAYER, json=body)
    assert created.status_code == 200, created.text
    match_id = created.json()["id"]

    listing = client.get("/api/matches", headers=PLAYER2).json()["matches"]
    match = next(m for m in listing if m["id"] == match_id)
    assert match["title"] == "Вечерний матч"
    assert match["players_count"] == 1
    assert match["participants"][0]["profile_token"]
    assert "telegram_id" not in match["participants"][0]
    assert match["creator"]["first_name"] == "Иван"

    assert client.post(
        f"/api/matches/{match_id}/join", headers=PLAYER2
    ).json()["status"] == "joined"
    assert client.post(
        f"/api/matches/{match_id}/join", headers=PLAYER2
    ).json()["status"] == "joined"

    third = {"X-Init-Data": _init({"id": 559, "first_name": "Третий"})}
    assert client.get("/api/me", headers=third).status_code == 200
    assert client.post(
        f"/api/matches/{match_id}/join", headers=third
    ).status_code == 409

    assert client.post(
        f"/api/matches/{match_id}/leave", headers=PLAYER2
    ).status_code == 200
    assert client.post(
        f"/api/matches/{match_id}/leave", headers=PLAYER2
    ).status_code == 400

    assert client.delete(
        f"/api/matches/{match_id}", headers=PLAYER2
    ).status_code == 404
    assert client.delete(
        f"/api/matches/{match_id}", headers=PLAYER
    ).status_code == 200
    assert client.get(
        f"/api/matches/{match_id}/join", headers=PLAYER2
    ).status_code == 405
    assert client.post(
        f"/api/matches/{match_id}/join", headers=PLAYER2
    ).status_code == 404


def test_match_validation_and_error_branches(client, monkeypatch):
    assert client.post(
        "/api/matches",
        headers=PLAYER,
        json={**_future(-1, "10:00"), "title": "Прошлый"},
    ).status_code == 400
    assert client.post(
        "/api/matches",
        headers=PLAYER,
        json={**_future(31, "10:00"), "level": "Неизвестный"},
    ).status_code == 422

    async def broken_create(**kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(mainmod.db, "create_match", broken_create)
    assert client.post(
        "/api/matches",
        headers=PLAYER,
        json={**_future(31, "11:00"), "title": "Сломанный"},
    ).status_code == 500
    monkeypatch.undo()
    mainmod._match_create_limiter.clear()

    match_id = client.post(
        "/api/matches",
        headers=PLAYER,
        json={**_future(31, "12:00"), "title": "Ошибки"},
    ).json()["id"]
    assert client.post("/api/matches/999/join", headers=PLAYER2).status_code == 404
    assert client.post("/api/matches/999/leave", headers=PLAYER2).status_code == 404
    assert client.delete("/api/matches/999", headers=PLAYER).status_code == 404

    async def broken_match(*args, **kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(mainmod.db, "get_match", broken_match)
    assert client.post(
        f"/api/matches/{match_id}/join", headers=PLAYER2
    ).status_code == 500
    assert client.post(
        f"/api/matches/{match_id}/leave", headers=PLAYER2
    ).status_code == 500
    assert client.delete(
        f"/api/matches/{match_id}", headers=PLAYER
    ).status_code == 500


def test_match_page_renders(client):
    page = client.get("/match")
    assert page.status_code == 200
    assert "Создать матч" in page.text
    assert 'id="match-sheet"' in page.text
    assert 'href="/match"' in page.text
    assert '<option value="Парк">Парк</option>' in page.text
    assert '<option value="Авакс">Авакс</option>' in page.text


def test_event_notification_edit_and_send_branches(client, monkeypatch):
    sent = []
    edited = []
    saves = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append((chat_id, text))
            return SimpleNamespace(message_id=101)

        async def edit_message_text(self, **kwargs):
            edited.append(kwargs)

    monkeypatch.setattr(mainmod.app.state, "bot", FakeBot())

    async def fake_save(*args):
        saves.append(args)

    async def no_record(*args):
        return None

    monkeypatch.setattr(mainmod.db, "save_event_notification", fake_save)
    monkeypatch.setattr(mainmod.db, "get_event_notification", no_record)
    asyncio.run(mainmod._send_event_notification("training", 1, 123, 0, "one"))
    assert sent == [(123, "one")]
    assert saves[-1][4] == 101

    async def same_record(*args):
        return {"message_id": 101, "text": "one"}

    monkeypatch.setattr(mainmod.db, "get_event_notification", same_record)
    asyncio.run(mainmod._send_event_notification("training", 1, 123, 0, "one"))
    assert len(sent) == 1

    async def changed_record(*args):
        return {"message_id": 101, "text": "old"}

    monkeypatch.setattr(mainmod.db, "get_event_notification", changed_record)
    asyncio.run(mainmod._send_event_notification("training", 1, 123, 0, "new"))
    assert edited and edited[-1]["text"] == "new"

    class NotModifiedBot(FakeBot):
        async def edit_message_text(self, **kwargs):
            raise RuntimeError("message is not modified")

    monkeypatch.setattr(mainmod.app.state, "bot", NotModifiedBot())
    asyncio.run(mainmod._send_event_notification("training", 1, 123, 0, "same?"))
    assert len(sent) == 1

    class BrokenEditBot(FakeBot):
        async def edit_message_text(self, **kwargs):
            raise RuntimeError("message deleted")

    monkeypatch.setattr(mainmod.app.state, "bot", BrokenEditBot())
    asyncio.run(mainmod._send_event_notification("training", 1, 123, 0, "resend"))
    assert len(sent) == 2

    class BrokenSendBot(FakeBot):
        async def send_message(self, *args, **kwargs):
            raise RuntimeError("telegram")

    monkeypatch.setattr(mainmod.db, "get_event_notification", no_record)
    monkeypatch.setattr(mainmod.app.state, "bot", BrokenSendBot())
    asyncio.run(mainmod._send_event_notification("training", 1, 123, 0, "fail"))


def test_training_notification_texts_and_targets(monkeypatch):
    now = int(time.time()) + 3600
    training = {
        "id": 1,
        "training_type": "Тренировка",
        "start_at": now,
        "capacity": 30,
        "created_by": 123,
        "deleted_at": None,
    }
    people = [
        {
            "status": "booked" if i < 26 else "waitlist",
            "telegram_id": 500 + i,
            "first_name": f"Игрок{i}",
            "display_mode": "name",
            "avatar_file_id": None,
        }
        for i in range(27)
    ]
    summary = mainmod._training_participants_text(training, people)
    assert "…" in summary
    assert "В очереди" in summary
    for status in ("promoted", "cancelled", "deleted", "waitlist", "booked"):
        assert mainmod._training_personal_text(training, status)

    deleted = {**training, "deleted_at": 1}
    assert "Тренировка отменена" in mainmod._training_participants_text(deleted, people)

    calls = []

    async def fake_participants(training_id):
        return people

    async def fake_send(kind, entity_id, chat_id, user_id, text):
        calls.append((kind, entity_id, chat_id, user_id, text))

    async def notifications_on(telegram_id, category):
        return True

    monkeypatch.setattr(mainmod.db, "get_participants", fake_participants)
    monkeypatch.setattr(mainmod.db, "notification_enabled", notifications_on)
    monkeypatch.setattr(mainmod, "_send_event_notification", fake_send)
    asyncio.run(mainmod._notify_training_event(training, 555))
    assert any(call[2] == 123 and call[3] == 0 for call in calls)
    assert any(call[2] == 555 and call[3] == 555 for call in calls)

    async def fake_training(training_id):
        return deleted

    monkeypatch.setattr(mainmod.db, "get_training", fake_training)
    calls.clear()
    asyncio.run(mainmod._notify_training_deleted(training))
    assert len(calls) >= 27


def test_match_notification_texts_and_targets(monkeypatch):
    match = {
        "id": 2,
        "title": "Матч",
        "start_at": int(time.time()) + 3600,
        "max_players": 30,
        "location": "Корт",
        "created_by": 123,
        "deleted_at": None,
    }
    players = [
        {
            "telegram_id": 500 + i,
            "first_name": f"Игрок{i}",
            "display_mode": "name",
            "avatar_file_id": None,
            "created_at": 1,
        }
        for i in range(27)
    ]
    summary = mainmod._match_participants_text(match, players)
    assert "…" in summary and "Корт" in summary
    for status in ("joined", "left", "deleted"):
        assert mainmod._match_personal_text(match, status)
    deleted = {**match, "deleted_at": 1}
    assert "Матч отменён" in mainmod._match_participants_text(deleted, players)
    assert "Матч отменён" in mainmod._match_update_text(deleted, players, "deleted")

    calls = []

    async def fake_players(match_id):
        return players

    async def fake_send(kind, entity_id, chat_id, user_id, text):
        calls.append((kind, entity_id, chat_id, user_id, text))

    async def notifications_on(telegram_id, category):
        return True

    monkeypatch.setattr(mainmod.db, "get_match_players", fake_players)
    monkeypatch.setattr(mainmod.db, "notification_enabled", notifications_on)
    monkeypatch.setattr(mainmod, "_send_event_notification", fake_send)
    asyncio.run(mainmod._notify_match_event(match, 555, "joined"))
    assert any(call[3] == 0 for call in calls)
    assert any(call[2] == 555 for call in calls)
    calls.clear()
    asyncio.run(mainmod._notify_match_deleted(deleted))
    assert len(calls) >= 27


def test_match_notification_skips_disabled_participant(monkeypatch):
    match = {
        "id": 2,
        "title": "Матч",
        "start_at": int(time.time()) + 3600,
        "max_players": 4,
        "location": "Парк",
        "created_by": 123,
        "deleted_at": None,
    }
    players = [
        {
            "telegram_id": 123,
            "first_name": "Тренер",
            "display_mode": "name",
            "avatar_file_id": None,
            "created_at": 1,
        },
        {
            "telegram_id": 556,
            "first_name": "Мария",
            "display_mode": "name",
            "avatar_file_id": None,
            "created_at": 1,
        },
    ]
    calls = []

    async def fake_players(match_id):
        return players

    async def allowed(telegram_id, category):
        return telegram_id != 556

    async def fake_send(kind, entity_id, chat_id, user_id, text):
        calls.append((chat_id, user_id))

    monkeypatch.setattr(mainmod.db, "get_match_players", fake_players)
    monkeypatch.setattr(mainmod.db, "notification_enabled", allowed)
    monkeypatch.setattr(mainmod, "_send_event_notification", fake_send)
    asyncio.run(mainmod._notify_match_event(match, 123, "joined"))

    assert (123, 0) in calls
    assert (556, 556) not in calls


def test_match_invalid_date_and_started(client, monkeypatch):
    assert client.post(
        "/api/matches",
        headers=PLAYER,
        json={**_future(1, "10:00"), "date": "2026-02-31"},
    ).status_code == 422

    async def past_match(match_id):
        return {
            "id": match_id,
            "deleted_at": None,
            "start_at": 0,
            "max_players": 4,
            "created_by": 555,
        }

    monkeypatch.setattr(mainmod.db, "get_match", past_match)
    assert client.post("/api/matches/1/join", headers=PLAYER2).status_code == 400
    assert client.post("/api/matches/1/leave", headers=PLAYER2).status_code == 400


def test_match_feed_text_and_recipients(monkeypatch):
    assert "пока нет" in mainmod._match_feed_text([])
    match = {
        "title": "Матч",
        "start_at": int(time.time()) + 3600,
        "location": "Корт",
    }
    text = mainmod._match_feed_text([match])
    assert "Матч" in text and "Корт" in text

    calls = []

    async def fake_matches(now, until):
        return [match]

    async def fake_recipients():
        return [{"telegram_id": 1}, {"telegram_id": 2}]

    async def fake_send(kind, entity_id, chat_id, user_id, text):
        calls.append((kind, entity_id, chat_id, user_id, text))

    monkeypatch.setattr(mainmod.db, "list_upcoming_matches", fake_matches)
    monkeypatch.setattr(mainmod.db, "list_match_feed_recipients", fake_recipients)
    monkeypatch.setattr(mainmod, "_send_event_notification", fake_send)
    asyncio.run(mainmod._notify_match_feed(exclude_user_id=1))
    assert calls == [("match_feed", 0, 2, 2, text)]


def test_match_creation_rate_limit(client):
    for day in (28, 29, 30):
        response = client.post(
            "/api/matches",
            headers=PLAYER,
            json={**_future(day, "10:00"), "title": f"Матч {day}"},
        )
        assert response.status_code == 200
    assert client.post(
        "/api/matches",
        headers=PLAYER,
        json={**_future(31, "10:00"), "title": "Лишний"},
    ).status_code == 429


def test_notification_preferences_api_roundtrip(client):
    me = client.get("/api/me", headers=PLAYER).json()["user"]
    assert me["notifications_enabled"] is True
    assert me["notify_matches"] is True
    assert me["notify_trainings"] is True
    assert me["notify_messages"] is True

    saved = client.put(
        "/api/me/profile",
        headers=PLAYER,
        json={
            "about": "Люблю подачу",
            "display_mode": "name",
            "show_username": False,
            "notifications_enabled": False,
            "notify_matches": False,
            "notify_trainings": False,
            "notify_messages": False,
        },
    )
    assert saved.status_code == 200
    profile = saved.json()["profile"]
    assert profile["notifications_enabled"] is False
    assert profile["notify_matches"] is False
    assert profile["notify_trainings"] is False
    assert profile["notify_messages"] is False

    me = client.get("/api/me", headers=PLAYER).json()["user"]
    assert me["notifications_enabled"] is False
    assert me["notify_matches"] is False
    assert me["notify_trainings"] is False
    assert me["notify_messages"] is False

    exported = client.get("/api/me/export", headers=PLAYER).json()["user"]
    assert exported["notify_matches"] is False
    assert exported["notify_trainings"] is False
    assert exported["notify_messages"] is False

    # Отдельный быстрый переключатель меняет только общий флаг.
    updated = client.put(
        "/api/me/notifications", headers=PLAYER, json={"enabled": True}
    )
    assert updated.status_code == 200
    me = client.get("/api/me", headers=PLAYER).json()["user"]
    assert me["notifications_enabled"] is True
    assert me["notify_matches"] is False


def test_message_delivery_respects_notification_preference(client, monkeypatch):
    client.get("/api/me", headers=COACH)
    assert client.put(
        "/api/me/profile",
        headers=COACH,
        json={
            "about": "",
            "display_mode": "name",
            "show_username": False,
            "notify_messages": False,
        },
    ).status_code == 200

    sent = []

    async def fake_send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    monkeypatch.setattr(mainmod.app.state, "bot", SimpleNamespace(send_message=fake_send))
    asyncio.run(mainmod._deliver_to_coach(123, 555, "Иван", "Привет"))
    assert sent == []

    assert client.put(
        "/api/me/profile",
        headers=COACH,
        json={
            "about": "",
            "display_mode": "name",
            "show_username": False,
            "notify_messages": True,
        },
    ).status_code == 200
    asyncio.run(mainmod._deliver_to_coach(123, 555, "Иван", "Привет"))
    assert sent and sent[0][0] == 123


def test_suppressed_event_notifications(monkeypatch):
    training = {
        "id": 1,
        "training_type": "Тренировка",
        "start_at": int(time.time()) + 3600,
        "capacity": 2,
        "created_by": 123,
        "deleted_at": None,
    }
    match = {
        "id": 2,
        "title": "Матч",
        "start_at": int(time.time()) + 3600,
        "max_players": 4,
        "location": "Парк",
        "created_by": 123,
        "deleted_at": None,
    }

    async def off(telegram_id, category):
        return False

    async def participants(training_id):
        return [{"telegram_id": 123}, {"telegram_id": 555}]

    async def match_players(match_id):
        return [
            {
                "telegram_id": 555,
                "first_name": "Иван",
                "display_mode": "name",
                "avatar_file_id": None,
                "created_at": 1,
            }
        ]

    async def fresh(training_id):
        return training

    async def no_managers():
        return []

    calls = []

    async def fake_send(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(mainmod.db, "notification_enabled", off)
    monkeypatch.setattr(mainmod.db, "get_participants", participants)
    monkeypatch.setattr(mainmod.db, "get_match_players", match_players)
    monkeypatch.setattr(mainmod.db, "get_training", fresh)
    monkeypatch.setattr(mainmod.db, "list_managers", no_managers)
    monkeypatch.setattr(mainmod.app.state, "bot", SimpleNamespace(send_message=fake_send))

    asyncio.run(mainmod._notify_training_deleted(training))
    asyncio.run(mainmod._notify_match_deleted(match))
    asyncio.run(mainmod._notify_training_transferred(777, training))
    asyncio.run(mainmod._notify_managers_about_application(555, "Иван"))
    assert calls == []


def test_feedback_submission_export_and_validation(client):
    response = client.post(
        "/api/feedback",
        headers=PLAYER,
        json={"kind": "bug", "text": "Не работает кнопка записи", "page": "Матч"},
    )
    assert response.status_code == 200
    assert response.json()["message"] == "Спасибо! Сообщение отправлено."

    exported = client.get("/api/me/export", headers=PLAYER).json()
    assert len(exported["feedback"]) == 1
    assert exported["feedback"][0]["kind"] == "bug"
    assert exported["feedback"][0]["page"] == "Матч"

    # Неизвестная страница нормализуется, чтобы в сообщении не было мусора.
    response = client.post(
        "/api/feedback",
        headers=PLAYER,
        json={"kind": "idea", "text": "Добавьте тёмную тему", "page": "unknown"},
    )
    assert response.status_code == 200
    exported = client.get("/api/me/export", headers=PLAYER).json()
    assert [item["page"] for item in exported["feedback"]] == ["Матч", "Приложение"]

    assert client.post(
        "/api/feedback",
        headers=PLAYER,
        json={"kind": "bug", "text": "  "},
    ).status_code == 422
    assert client.post(
        "/api/feedback",
        headers=PLAYER,
        json={"kind": "other", "text": "Текст"},
    ).status_code == 422


def test_feedback_database_error(client, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(mainmod.db, "add_feedback", broken)
    response = client.post(
        "/api/feedback",
        headers=PLAYER,
        json={"kind": "bug", "text": "Ошибка базы"},
    )
    assert response.status_code == 500


def test_feedback_rate_limit(client):
    for index in range(3):
        assert client.post(
            "/api/feedback",
            headers=PLAYER,
            json={"kind": "idea", "text": f"Идея {index}"},
        ).status_code == 200
    response = client.post(
        "/api/feedback",
        headers=PLAYER,
        json={"kind": "idea", "text": "Идея 4"},
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"]


def test_feedback_notification_hides_id_and_supports_reply(monkeypatch):
    sent = {}

    async def fake_send(chat_id, text, reply_markup=None):
        sent["chat_id"] = chat_id
        sent["text"] = text
        sent["reply_markup"] = reply_markup

    monkeypatch.setattr(mainmod.app.state, "bot", SimpleNamespace(send_message=fake_send))
    user = {
        "telegram_id": 555,
        "first_name": "Иван",
        "display_mode": "name",
    }
    asyncio.run(
        mainmod._notify_feedback(user, "bug", "<b>Не работает</b>", "Тренировки")
    )

    assert sent["chat_id"] == 123
    assert "Проблема" in sent["text"]
    assert "Иван" in sent["text"]
    assert "Тренировки" in sent["text"]
    assert "&lt;b&gt;Не работает&lt;/b&gt;" in sent["text"]
    assert "555" not in sent["text"]
    assert sent["reply_markup"].inline_keyboard[0][0].callback_data == "reply:555"

    # В режиме «животное» имя скрыто и в обратной связи.
    animal_user = {
        "telegram_id": 555,
        "first_name": "Иван",
        "display_mode": "animal",
    }
    asyncio.run(mainmod._notify_feedback(animal_user, "idea", "Идея", ""))
    assert "Иван" not in sent["text"]
    assert "Предложение" in sent["text"]

    async def failing_send(*args, **kwargs):
        raise RuntimeError("telegram")

    monkeypatch.setattr(mainmod.app.state, "bot", SimpleNamespace(send_message=failing_send))
    asyncio.run(mainmod._notify_feedback(user, "idea", "Идея", ""))
