"""Стабильные вымышленные личности для анонимного режима."""

from __future__ import annotations

ANIMALS: tuple[tuple[str, str], ...] = (
    ("🦊", "Лиса"),
    ("🐻", "Медведь"),
    ("🐼", "Панда"),
    ("🦉", "Сова"),
    ("🦦", "Выдра"),
    ("🦔", "Ёж"),
    ("🦡", "Барсук"),
    ("🐧", "Пингвин"),
    ("🐰", "Кролик"),
    ("🐸", "Лягушка"),
    ("🐯", "Тигр"),
    ("🐨", "Коала"),
)


def animal_identity(telegram_id: int) -> dict[str, str]:
    """Возвращает одно и то же животное для одного telegram_id."""
    emoji, name = ANIMALS[int(telegram_id) % len(ANIMALS)]
    return {"emoji": emoji, "name": name}
