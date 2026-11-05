"""Текст и изображение общей сетки занятия для уведомлений Telegram."""
from __future__ import annotations

import html
import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


def _name(state: dict[str, Any], index: Any, round_index: int) -> str:
    players = state.get("players", [])
    if not isinstance(index, int) or isinstance(index, bool) or index < 0 or index >= len(players):
        return "Ожидается игрок"
    past = state.get("past") or {}
    segments = past.get(str(index), []) if isinstance(past, dict) else []
    for segment in segments if isinstance(segments, list) else []:
        if isinstance(segment, dict) and isinstance(segment.get("from"), int) and isinstance(segment.get("to"), int) and segment["from"] <= round_index < segment["to"]:
            return str(segment.get("name") or "Игрок")[:80]
    return str(players[index] or "Игрок")[:80]


def _score(state: dict[str, Any], key: str) -> tuple[int | None, int | None, str | None]:
    item = state.get("scores", {}).get(key, {})
    if not isinstance(item, dict):
        return None, None, None
    def num(value: Any) -> int | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 999 and int(value) == value:
            return int(value)
        return None
    a, b = num(item.get("a")), num(item.get("b"))
    end = item.get("end")
    if isinstance(end, dict) and end.get("winner") in ("a", "b"):
        winner = end["winner"]
    elif a is not None and b is not None and max(a, b) >= 11 and abs(a - b) >= 2:
        winner = "a" if a > b else "b"
    else:
        winner = None
    return a, b, winner


def matches(state: dict[str, Any]) -> list[tuple[str, str, str, int | None, int | None, str | None]]:
    """Матчи сетки и плей-офф в порядке показа приложения."""
    out = []
    for ri, round_data in enumerate(state.get("schedule", [])):
        if not isinstance(round_data, dict) or not isinstance(round_data.get("courts"), list):
            continue
        for ci, pair in enumerate(round_data["courts"]):
            if not isinstance(pair, list) or len(pair) != 2:
                continue
            a, b, winner = _score(state, f"{ri}-{ci}")
            out.append((f"Раунд {ri + 1}", _name(state, pair[0], ri), _name(state, pair[1], ri), a, b, winner))
    playoff = state.get("po")
    rounds = playoff.get("rounds", []) if isinstance(playoff, dict) else []
    for ri, games in enumerate(rounds if isinstance(rounds, list) else []):
        if not isinstance(games, list):
            continue
        for ci, pair in enumerate(games):
            if not isinstance(pair, dict):
                continue
            a, b, winner = _score(state, f"p{ri}-{ci}")
            out.append((f"Плей-офф · раунд {ri + 1}", _name(state, pair.get("a"), 0), _name(state, pair.get("b"), 0), a, b, winner))
    return out


def complete(state: dict[str, Any]) -> bool:
    players, schedule = state.get("players"), state.get("schedule")
    if not isinstance(players, list) or not 2 <= len(players) <= 16 or not isinstance(schedule, list) or not schedule:
        return False
    def valid_pair(pair: Any) -> bool:
        return (isinstance(pair, list) and len(pair) == 2 and all(
            isinstance(n, int) and not isinstance(n, bool) and 0 <= n < len(players) for n in pair
        ) and pair[0] != pair[1])
    total = 0
    for ri, round_data in enumerate(schedule):
        if not isinstance(round_data, dict) or not isinstance(round_data.get("courts"), list):
            return False
        for ci, pair in enumerate(round_data["courts"]):
            if pair is None:
                continue
            if not valid_pair(pair) or not _score(state, f"{ri}-{ci}")[2]:
                return False
            total += 1
    playoff = state.get("po")
    if playoff is not None:
        if not isinstance(playoff, dict) or not isinstance(playoff.get("rounds"), list) or not playoff["rounds"]:
            return False
        for ri, round_data in enumerate(playoff["rounds"]):
            if not isinstance(round_data, list) or not round_data:
                return False
            for ci, pair in enumerate(round_data):
                if not isinstance(pair, dict) or not valid_pair([pair.get("a"), pair.get("b")]) or not _score(state, f"p{ri}-{ci}")[2]:
                    return False
    return total > 0


def message(state: dict[str, Any], heading: str) -> str:
    lines = [heading, "", "Сетка матчей"]
    group = ""
    for label, left, right, a, b, _ in matches(state):
        additions = []
        if label != group:
            additions = ["", html.escape(label)]
            group = label
        clean = lambda value: html.escape(" ".join(value.split())[:60])
        additions.append(f"{clean(left)} — {clean(right)}  ·  {a if a is not None else '—'} : {b if b is not None else '—'}")
        if len("\n".join(lines + additions)) > 3700:
            lines.extend(["", "Остальные матчи — по кнопке ниже."])
            break
        lines.extend(additions)
    return "\n".join(lines)


def standings(state: dict[str, Any]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for label, left, right, a, b, winner in matches(state):
        if label.startswith("Плей-офф"):
            continue
        for name in (left, right):
            rows.setdefault(name, {"name": name, "played": 0, "total": 0, "wins": 0, "losses": 0, "sf": 0, "sa": 0})
            rows[name]["total"] += 1
        if winner:
            x, y = rows[left], rows[right]
            x["played"] += 1; y["played"] += 1
            x["sf"] += a or 0; x["sa"] += b or 0
            y["sf"] += b or 0; y["sa"] += a or 0
            x["wins" if winner == "a" else "losses"] += 1
            y["losses" if winner == "a" else "wins"] += 1
    competitive = bool(state.get("competitive", True))
    return sorted(rows.values(), key=lambda r: (-r["wins"], -(r["sf"] - r["sa"]) if competitive else 0, -r["sf"], r["name"].casefold()))


def image(state: dict[str, Any]) -> bytes:
    rows = standings(state)
    font_path = Path(__file__).parent / "static/fonts/DejaVuSans.ttf"
    normal = ImageFont.truetype(str(font_path), 23)
    small = ImageFont.truetype(str(font_path), 18)
    title = ImageFont.truetype(str(font_path), 28)
    canvas = Image.new("RGB", (800, max(220, 165 + 54 * len(rows))), "#121212")
    draw = ImageDraw.Draw(canvas)
    draw.text((28, 22), "Итоговая таблица", fill="#f3f3f3", font=title)
    draw.text((28, 77), "#   ИГРОК", fill="#a8a8a4", font=small)
    for x, name in ((405, "В–П"), (500, "ГОЛЫ"), (625, "±"), (680, "ПРОГРЕСС")):
        draw.text((x, 77), name, fill="#a8a8a4", font=small)
    for i, row in enumerate(rows):
        y = 110 + i * 54
        draw.line((28, y, 772, y), fill="#303030", width=1)
        draw.text((30, y + 12), str(i + 1), fill="#a8a8a4", font=normal)
        name = row["name"]
        while draw.textlength(name, font=normal) > 325:
            name = name[:-2] + "…"
        draw.text((70, y + 12), name, fill="#f4f4f4", font=normal)
        draw.text((405, y + 12), f'{row["wins"]}–{row["losses"]}', fill="#f4f4f4", font=normal)
        draw.text((500, y + 12), f'{row["sf"]}–{row["sa"]}', fill="#a8a8a4", font=normal)
        difference = row["sf"] - row["sa"]
        draw.text((625, y + 12), f'{difference:+d}' if difference else '0', fill="#49db86" if difference > 0 else "#ffad68" if difference < 0 else "#f4f4f4", font=normal)
        draw.rounded_rectangle((680, y + 23, 740, y + 30), radius=3, fill="#303030")
        progress = round(60 * row["played"] / row["total"]) if row["total"] else 0
        if progress:
            draw.rounded_rectangle((680, y + 23, 680 + progress, y + 30), radius=3, fill="#f7d843")
        draw.text((748, y + 15), f'{row["played"]}/{row["total"]}', fill="#a8a8a4", font=small)
    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()
