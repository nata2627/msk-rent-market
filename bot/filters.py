"""Фильтры подбора: у каждого чата свои, в файле filters.<чат>.

Здесь же экран фильтров с кнопками, /filter текстом и /undo.

Формат файла тот же, что у старого бота (строки ключ=значение, верна
последняя), поэтому после переключения фильтры владелицы и гостей
остаются на месте.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from bot import db
from bot.actions import Ctx, Keyboard, Reply
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


def lines(f: dict[str, str]) -> str:
    """То же, что human, списком: главное содержимое экрана фильтров."""
    out = []
    if "price" in f:
        out.append(f"Цена: до {f['price']} тыс")
    if "area" in f:
        out.append(f"Площадь: от {f['area']} м2")
    if "rooms" in f:
        out.append(f"Комнаты: {f['rooms']}")
    if "metro" in f:
        out.append(f"Метро: до {f['metro']} мин пешком")
    if "entry" in f:
        out.append(f"Нужно сразу: до {f['entry']} тыс")
    if "pets" in f:
        out.append(f"Животные: {pets_human(f['pets'])}")
    if "park" in f:
        out.append("Парк: в пределах километра")
    if "mkad" in f:
        out.append("МКАД: только внутри")
    if "district" in f:
        out.append(f"Район: {f['district']}")
    if "seen" in f:
        out.append(f"Пропущено обходов: до {f['seen']}")
    return "\n".join(out)


# ---------------------------------------------------------------------
# Экран фильтров
# ---------------------------------------------------------------------

def keyboard(f: dict[str, str]) -> Keyboard:
    # В callback_data телеграм пускает не больше 64 байт, поэтому метки
    # короткие: p-10 это «цена минус десять тысяч».
    def mark(flag: bool, label: str) -> str:
        # У телеграма нет состояния «нажато», выбранное помечено точкой.
        return "● " + label if flag else label
    price, rooms, area = f.get("price", ""), f.get("rooms", ""), f.get("area", "")
    metro, pets = f.get("metro", ""), f.get("pets", "")
    pets_label = {"allowed": "можно", "negotiable": "по договоренности",
                  "forbidden": "нельзя"}.get(pets, "не важно")
    return [
        [("Цена -10", "p-10"), ("цена: " + (price + " тыс" if price else "любая"), "noop"),
         ("+10", "p+10")],
        [("Площадь -5", "a-5"), ("от " + (area + " м2" if area else "любой"), "noop"),
         ("+5", "a+5")],
        [(mark(rooms == "1", "1к"), "r1"), (mark(rooms == "2", "2к"), "r2"),
         (mark(rooms == "1,2", "1-2к"), "r12"), (mark(not rooms, "любая"), "r0")],
        [(mark(metro == "10", "метро 10"), "m10"), (mark(metro == "15", "15"), "m15"),
         (mark(metro == "20", "20"), "m20"), (mark(not metro, "любое"), "m0")],
        # Животные одной кнопкой по кругу: тремя кнопками надпись резалась
        # до «договор», а это читается как договор аренды.
        [("Животные: " + pets_label, "e")],
        [(mark("park" in f, "парк в 1 км"), "k"), (mark("mkad" in f, "внутри МКАД"), "d")],
        [("Сбросить все", "x"), ("Показать варианты", "s")],
    ]


def screen_text(f: dict[str, str]) -> str:
    cond, params = where(f)
    found = db.scalar(f"select count(*) from marts.offer_pick where {cond}", params)
    return f"""Фильтры подбора

{lines(f) or '''Пока ничего не задано, поэтому наверх лезет дорогое: в оценке
дороговизна читается как признак качества.'''}

Подходит: {'?' if found is None else found} вариантов

Меняйте кнопками под этим сообщением, оно обновится на месте.
Точные числа и район удобнее текстом, например:
$ /filter цена 90
Весь список текстовых команд: /filter текстом"""


def screen(ctx: Ctx, msg_id: str = "") -> None:
    """Экран фильтров новым сообщением или правкой прежнего на месте."""
    # Начальный отбор ставится и здесь: экран открывают раньше подбора, и
    # «ничего не задано» на нем значит список квартир за три миллиона.
    defaults(ctx.settings, ctx.chat)
    f = load(ctx.settings, ctx.chat)
    ctx.replies.append(Reply(screen_text(f), keyboard(f), msg_id=msg_id))


def _step(f: dict[str, str], key: str, start: int, delta: int, lo: int, hi: int) -> int:
    return min(hi, max(lo, int(f.get(key, start)) + delta))


# Переключатели: метка кнопки, ключ, значение, подсказка.
SETS = {
    "r1": ("rooms", "1", "однокомнатные"),
    "r2": ("rooms", "2", "двухкомнатные"),
    "r12": ("rooms", "1,2", "одно- и двухкомнатные"),
    "r0": ("rooms", "", "комнатность любая"),
    "m10": ("metro", "10", "метро до 10 минут"),
    "m15": ("metro", "15", "метро до 15 минут"),
    "m20": ("metro", "20", "метро до 20 минут"),
    "m0": ("metro", "", "метро не важно"),
}


def button(ctx: Ctx, data: str, msg_id: str) -> bool:
    """Кнопка экрана фильтров; False, если кнопка не отсюда.

    Белый список: в фильтры попадает только перечисленное здесь, что бы ни
    пришло в callback_data.
    """
    s, chat = ctx.settings, ctx.chat
    f = load(s, chat)
    if data in ("p-10", "p+10"):
        cur = _step(f, "price", 100, -10 if data == "p-10" else 10, 10, 999)
        put(s, chat, "price", str(cur)); toast = f"цена до {cur} тыс"
    elif data in ("a-5", "a+5"):
        cur = _step(f, "area", 40, -5 if data == "a-5" else 5, 10, 300)
        put(s, chat, "area", str(cur)); toast = f"площадь от {cur} м2"
    elif data in SETS:
        key, value, toast = SETS[data]
        put(s, chat, key, value)
    elif data == "e":
        # По кругу: не важно, можно, по договоренности. «Нельзя» в круг не
        # входит: отбирать квартиры с запретом незачем, текстом можно.
        pets = f.get("pets", "")
        if not pets:
            put(s, chat, "pets", "allowed"); toast = "животные: можно"
        elif pets == "allowed":
            put(s, chat, "pets", "negotiable"); toast = "животные: по договоренности"
        else:
            put(s, chat, "pets", ""); toast = "животные: не важно"
    elif data == "k":
        put(s, chat, "park", "" if "park" in f else "1")
        toast = "парк не важен" if "park" in f else "парк в пределах километра"
    elif data == "d":
        put(s, chat, "mkad", "" if "mkad" in f else "1")
        toast = "МКАД не важен" if "mkad" in f else "только внутри МКАД"
    elif data == "x":
        path(s, chat).unlink(missing_ok=True); toast = "фильтры сброшены"
    elif data == "filters":
        ctx.answer("фильтры")
        screen(ctx)
        return True
    else:
        return False
    ctx.answer(toast)
    screen(ctx, msg_id)
    return True


# ---------------------------------------------------------------------
# /filter текстом и /undo
# ---------------------------------------------------------------------

TEXT_HELP = """Фильтры текстом

/filter цена 90          потолок в тысячах
/filter площадь 35       от скольких м2
/filter комнаты 1,2      комнатность
/filter метро 15         минут пешком
/filter сразу 250        сколько нужно денег сразу
/filter животные можно   можно / договор / нельзя
/filter парк да          парк от 10 га в километре
/filter мкад да          только внутри
/filter район Хамовники  сверяется со справочником базы
/filter обходы 2         насколько старую выдачу пускать
/filter цена -           убрать один
/filter сброс            убрать все

Кнопками: /filter
Колонки и смысл те же, что у фильтров дашборда."""

# Ключ: (имя в файле, подсказка, нижняя граница, верхняя).
NUMBERS = {
    "цена": ("price", "цена в тысячах, от 10 до 999", 10, 999),
    "price": ("price", "цена в тысячах, от 10 до 999", 10, 999),
    "площадь": ("area", "площадь в м2, от 10 до 300", 10, 300),
    "area": ("area", "площадь в м2, от 10 до 300", 10, 300),
    "метро": ("metro", "минут пешком, от 1 до 60", 1, 60),
    "metro": ("metro", "минут пешком, от 1 до 60", 1, 60),
    "сразу": ("entry", "сумма в тысячах, от 10 до 9999", 10, 9999),
    "entry": ("entry", "сумма в тысячах, от 10 до 9999", 10, 9999),
    "обходы": ("seen", "пропущено обходов, от 0 до 10", 0, 10),
    "seen": ("seen", "пропущено обходов, от 0 до 10", 0, 10),
}

ROOMS = re.compile(r"[1-5](,[1-5]){0,2}")

DISTRICTS_SQL = ("select district from core.offer where source='cian' and district is not null "
                 "group by 1 having count(*) >= 3")


def prev_path(settings: Settings, chat: str) -> Path:
    return settings.state_dir / f"filters.{chat}.prev"


def _now(ctx: Ctx) -> str:
    return f"Сейчас отбор: {human(load(ctx.settings, ctx.chat))}"


def _number(ctx: Ctx, key: str, val: str, hint: str, lo: int, hi: int) -> None:
    # Значение проверяется образцом и границами и только потом попадает в
    # файл. Цена в рублях вместо тысяч частая опечатка: молча она дала бы
    # пустой список.
    s, chat = ctx.settings, ctx.chat
    if val == "-":
        put(s, chat, key, "")
        ctx.reply(f"Фильтр «{key}» убран.\n{_now(ctx)}")
    elif not val or not val.isascii() or not val.isdigit():
        ctx.reply(f"Нужно число: {hint}")
    elif not lo <= int(val) <= hi:
        ctx.reply(f"Не похоже на правду: {hint}")
    else:
        put(s, chat, key, val)
        ctx.reply(f"Принято: {key} = {val}\n{_now(ctx)}")


def cmd_undo(ctx: Ctx, p=None) -> None:
    prev = prev_path(ctx.settings, ctx.chat)
    if prev.exists():
        prev.replace(path(ctx.settings, ctx.chat))
        ctx.reply(f"Вернул прежние фильтры: {human(load(ctx.settings, ctx.chat))}")
    else:
        ctx.reply("Прежних фильтров нет: фразой фильтры еще не менялись.")


def cmd_filter(ctx: Ctx, key: str, val: str) -> None:
    s, chat = ctx.settings, ctx.chat
    if not key:
        # Экран с кнопками. Текст остается единственным путем для района и
        # точных чисел: кнопками удобно крутить, текстом задавать.
        screen(ctx)
    elif key in ("текстом", "help", "справка"):
        ctx.reply(TEXT_HELP)
    elif key in ("вернуть", "undo"):
        cmd_undo(ctx)
    elif key in ("сброс", "reset"):
        path(s, chat).unlink(missing_ok=True)
        ctx.reply("""Фильтры сброшены. Остался только один, несбрасываемый:
показываются объявления, встреченные в последнем обходе — иначе каждое
восьмое ведёт на снятую публикацию.""")
    elif key in NUMBERS:
        name, hint, lo, hi = NUMBERS[key]
        _number(ctx, name, val, hint, lo, hi)
    elif key in ("комнаты", "rooms"):
        if val == "-":
            put(s, chat, "rooms", ""); ctx.reply("Фильтр по комнатам убран.")
        elif ROOMS.fullmatch(val) and val.isascii():
            put(s, chat, "rooms", val); ctx.reply(f"Комнат: {val}\n{_now(ctx)}")
        else:
            ctx.reply("Комнатность — числа от 1 до 5 через запятую. Например: /filter комнаты 1,2")
    elif key in ("животные", "pets"):
        if val == "-":
            put(s, chat, "pets", ""); ctx.reply("Фильтр по животным убран.")
        elif val == "можно":
            put(s, chat, "pets", "allowed"); ctx.reply(f"Животные: можно\n{_now(ctx)}")
        elif val in ("договор", "договорённость", "по договорённости"):
            put(s, chat, "pets", "negotiable")
            ctx.reply(f"Животные: по договорённости\n{_now(ctx)}")
        elif val == "нельзя":
            put(s, chat, "pets", "forbidden"); ctx.reply(f"Животные: нельзя\n{_now(ctx)}")
        else:
            ctx.reply("Животные: можно, договор или нельзя. Например: /filter животные можно")
    elif key in ("парк", "park"):
        if val in ("-", "нет"):
            put(s, chat, "park", ""); ctx.reply("Фильтр по парку убран.")
        elif val == "да":
            put(s, chat, "park", "1"); ctx.reply(f"Парк: в пределах километра\n{_now(ctx)}")
        else:
            ctx.reply("Парк: да или нет. Рабочий порог — парк от 10 га в километре.")
    elif key in ("мкад", "mkad"):
        if val in ("-", "нет"):
            put(s, chat, "mkad", ""); ctx.reply("Фильтр по МКАД убран.")
        elif val == "да":
            put(s, chat, "mkad", "1"); ctx.reply(f"Только внутри МКАД\n{_now(ctx)}")
        else:
            ctx.reply("МКАД: да или нет.")
    elif key in ("район", "district"):
        if val == "-":
            put(s, chat, "district", ""); ctx.reply("Фильтр по району убран.")
            return
        # Район не берется как пришел: в файл идет только название,
        # найденное в справочнике базы.
        known = {r[0] for r in db.query(DISTRICTS_SQL) or []}
        if val in known:
            put(s, chat, "district", val); ctx.reply(f"Район: {val}\n{_now(ctx)}")
        else:
            ctx.reply(f"""Района «{val}» не знаю. Названия как на ЦИАНе, например: Хамовники,
Раменки, Южнопортовый. Убрать фильтр: /filter район -""")
    else:
        ctx.reply("Такого фильтра нет. Список — /filter")
