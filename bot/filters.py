"""Фильтры подбора: у каждого чата свои, в файле filters.<чат>.

Формат файла тот же, что у старого бота (строки ключ=значение, верна
последняя), поэтому после переключения фильтры владелицы и гостей
остаются на месте.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from bot import db
from bot.config import Settings

PETS_HUMAN = {
    "allowed": "можно",
    "negotiable": "по договоренности",
    "forbidden": "нельзя",
    "unknown": "не указано",
}

# Значение из файла проверяется еще раз при чтении: файл правят руками,
# а в запрос оно уходит параметром, но число должно быть числом.
VALID = {
    "price": re.compile(r"\d{1,4}"),
    "area": re.compile(r"\d{1,3}"),
    "rooms": re.compile(r"\d(,\d)*"),
    "metro": re.compile(r"\d{1,3}"),
    "entry": re.compile(r"\d{1,5}"),
    "pets": re.compile("|".join(PETS_HUMAN)),
    "park": re.compile(r".+"),
    "mkad": re.compile(r".+"),
    "district": re.compile(r".+"),
    "seen": re.compile(r"\d{1,3}"),
}


def path(settings: Settings, chat: str) -> Path:
    return settings.state_dir / f"filters.{chat}"


def load(settings: Settings, chat: str) -> dict[str, str]:
    try:
        lines = path(settings, chat).read_text().splitlines()
    except FileNotFoundError:
        return {}
    out = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key in VALID and VALID[key].fullmatch(value):
            out[key] = value
    return out


def put(settings: Settings, chat: str, key: str, value: str) -> None:
    """Пустое значение снимает фильтр. Измененный ключ уходит в конец."""
    p = path(settings, chat)
    try:
        lines = [ln for ln in p.read_text().splitlines() if not ln.startswith(f"{key}=")]
    except FileNotFoundError:
        lines = []
    if value:
        lines.append(f"{key}={value}")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text("".join(f"{ln}\n" for ln in lines))
    tmp.replace(p)


def where(f: dict[str, str]) -> tuple[str, list]:
    """Условие к marts.offer_pick: значения только параметрами."""
    w, params = [], []
    if "price" in f:
        w.append("price <= %s"); params.append(int(f["price"]) * 1000)
    if "area" in f:
        w.append("total_area >= %s"); params.append(int(f["area"]))
    if "rooms" in f:
        w.append("rooms_count = any(%s)"); params.append([int(r) for r in f["rooms"].split(",")])
    if "metro" in f:
        w.append("metro_real_walk_min <= %s"); params.append(int(f["metro"]))
    if "entry" in f:
        w.append("entry_cost <= %s"); params.append(int(f["entry"]) * 1000)
    if "pets" in f:
        w.append("pets_state = %s"); params.append(f["pets"])
    if "park" in f:
        w.append("park_band in ('до 500 м, пешком', '500 м – 1 км')")
    if "mkad" in f:
        w.append("inside_mkad")
    if "district" in f:
        w.append("district = %s"); params.append(f["district"])
    # Свежесть: умолчание ноль, «была в последней выдаче». Без него каждое
    # восьмое объявление подбора ведет на снятую публикацию.
    w.append("crawls_missed <= %s"); params.append(int(f.get("seen", 0)))
    w.append("is_active")
    return " and ".join(w), params


def pets_human(state: str) -> str:
    return PETS_HUMAN.get(state, state)


def human(f: dict[str, str]) -> str:
    parts = []
    if "price" in f:
        parts.append(f"цена до {f['price']} тыс")
    if "area" in f:
        parts.append(f"площадь от {f['area']} м2")
    if "rooms" in f:
        parts.append(f"комнаты {f['rooms']}")
    if "metro" in f:
        parts.append(f"метро до {f['metro']} мин")
    if "entry" in f:
        parts.append(f"сразу до {f['entry']} тыс")
    if "pets" in f:
        parts.append(f"животные {pets_human(f['pets'])}")
    if "park" in f:
        parts.append("парк в километре")
    if "mkad" in f:
        parts.append("внутри МКАД")
    if "district" in f:
        parts.append(f"район {f['district']}")
    if "seen" in f:
        parts.append(f"пропущено обходов до {f['seen']}")
    return ", ".join(parts)


MEDIAN_SQL = """
select round(percentile_cont(0.5) within group (order by price) / 1000.0)::int
  from marts.offer_pick
 where is_active and crawls_missed <= 0 and rooms_count in (1, 2)"""


def defaults(settings: Settings, chat: str) -> int | None:
    """Начальный отбор для чата без фильтров; число, если он поставлен.

    Пустой отбор это не «показать все», а «показать самое дорогое»: балл
    за цену в проекте перевернут намеренно. Потолок берется из данных,
    медиана по однушкам и двушкам в сегодняшней выдаче.
    """
    if path(settings, chat).exists():
        return None
    med = db.scalar(MEDIAN_SQL) or 100
    put(settings, chat, "rooms", "1,2")
    put(settings, chat, "price", str(med))
    return med
