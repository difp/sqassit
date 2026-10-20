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
from urllib.parse import urlencode

import pytest
from starlette.testclient import TestClient

import app.bot as botmod
import app.main as mainmod
from app.config import TRAINING_LEVELS, settings


# --- Заглушки сети Telegram (в тестах наружу не ходим) --- #
@asynccontextmanager
async def _fake_polling(bot, dp):
    yield


async def _fake_loop(bot, interval_seconds=60):
    while True:
        await asyncio.sleep(3600)


botmod.polling_context = _fake_polling
botmod.reminder_loop = _fake_loop
mainmod.bot_module.polling_context = _fake_polling
mainmod.bot_module.reminder_loop = _fake_loop


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
def client():
    # Каждый тест получает чистую БД и чистые in-memory-состояния.
    for suffix in ("", "-wal", "-shm"):
        Path(f"{settings.db_path}{suffix}").unlink(missing_ok=True)
    mainmod._bot_check_cache = (0.0, False)
    mainmod._message_limiter.clear()
    mainmod._apply_limiter.clear()
    mainmod._booking_limiter.clear()

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
