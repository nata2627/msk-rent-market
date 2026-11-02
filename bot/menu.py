"""Меню, которое телеграм показывает по нажатию «/»."""
from __future__ import annotations

import time
from pathlib import Path

# Список и порядок из box_tg.py: сверху то, чем пользуются в дороге
# каждый день.
COMMANDS = [
    ("status", "что сейчас с панелью и машиной"),
    ("flats", "подбор карточками, листается кнопками"),
    ("saved", "отложенные варианты"),
    ("filter", "фильтры подбора, с кнопками"),
    ("market", "числа рынка за сегодня"),
    ("log", "хвост журнала прогона"),
    ("pulse", "итог дня прямо сейчас"),
    ("clean", "освободить место на диске"),
    ("unlock", "снять заклинивший замок"),
    ("catchup", "догнать сегодняшний день"),
    ("dbup", "поднять базу, если упала"),
    ("help", "список команд"),
]

# Меню меняется редко, поэтому обновляется раз в сутки, а не на каждом
# старте: launchd может перезапускать процесс часто.
REFRESH_S = 24 * 3600


def due(stamp: Path, now: float | None = None) -> bool:
    try:
        return (now or time.time()) - stamp.stat().st_mtime > REFRESH_S
    except FileNotFoundError:
        return True
