"""Подбор карусельной карточкой: /flats, /saved и кнопки под карточкой.

Одно сообщение с фото, которое листается кнопками на месте. Раньше подбор
приходил пачкой по пять сообщений, и после двух листаний чат превращался
в ленту. Понравившийся вариант откладывается звездой и живет в /saved.

Состояние карусели свое у каждого чата, в файлах старого бота:
card.<чат> (режим|номер|id сообщения|id объявления) и saved.<чат>.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from bot import db, filters
from bot.actions import Card, Ctx, Keyboard
from bot.config import Settings

# Столбцы карточки, дословно из CARD_COLS старого бота. Запросы с ними
# идут с параметрами, поэтому знак процента удвоен.
CARD_COLS = """o.offer_id, round(o.score)::int, o.district, o.rooms_count, round(o.total_area)::int,
           -- Цена точная, в рублях: округление до тысяч прятало, что 89 500
           -- это не 90 000. Рядом сумма при заселении (месяц, залог,
           -- комиссия), со звездочкой, если залог не указан и предположен.
           replace(to_char(o.price, 'FM999,999,999'), ',', ' '),
           coalesce(replace(to_char(o.entry_cost, 'FM999,999,999'), ',', ' '), ''),
           -- Из чего сложена сумма при заселении, по той же формуле, что
           -- entry_cost в marts. Части через ~, в подписи это строки:
           -- поле с переводом строки разорвало бы разбор ответа psql.
           'оплата за ' || coalesce(o.prepay_months, 1) || ' мес. '
             || replace(to_char(o.price * coalesce(o.prepay_months, 1), 'FM999,999,999'), ',', ' ')
           || '~залог ' || replace(to_char(case
                when o.deposit_rub is not null and o.deposit_rub <= o.price * 6 then o.deposit_rub
                when o.deposit_months is not null then o.price * o.deposit_months
                else o.price end, 'FM999,999,999'), ',', ' ')
             || case when o.deposit_assumed then ' (в объявлении не указан, взят за месяц)' else '' end
           || '~' || case
                when coalesce(o.client_fee_pct, 0) > 0
                  then 'комиссия ' || round(o.client_fee_pct) || '%% '
                       || replace(to_char(o.price * o.client_fee_pct / 100.0, 'FM999,999,999'), ',', ' ')
                when coalesce(o.commission_rub, 0) > 0
                  then 'комиссия ' || replace(to_char(o.commission_rub, 'FM999,999,999'), ',', ' ')
                else 'без комиссии' end,
           coalesce(o.metro_real_walk_min::text, '?'), o.pets_state,
           o.is_active,
           -- Адрес без хвоста: в нем метки показа, вдвое длиннее самой
           -- ссылки и ничего не значащие для человека.
           split_part(o.url, '?', 1),
           coalesce((select p.url from core.offer_photo p
                      where p.source = o.source and p.offer_id = o.offer_id
                      order by p.photo_pos limit 1), '')"""


@dataclass
class Row:
    offer_id: str
    score: str
    district: str
    rooms: str
    area: str
    price: str
    entry: str
    parts: str
    metro: str
    pets: str
    active: bool
    url: str
    photo: str


def to_row(values) -> Row:
    # Пустое поле psql у старого бота печатал пустой строкой.
    v = ["" if x is None else x for x in values]
    return Row(str(v[0]), *(str(x) for x in v[1:10]), bool(v[10]), str(v[11]), str(v[12]))


# ---------------------------------------------------------------------
# Состояние чата
# ---------------------------------------------------------------------

def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(text)
    tmp.replace(p)


def card_path(settings: Settings, chat: str) -> Path:
    return settings.state_dir / f"card.{chat}"


def saved_path(settings: Settings, chat: str) -> Path:
    return settings.state_dir / f"saved.{chat}"


@dataclass
class State:
    mode: str = ""
    idx: int = 0
    msg: str = ""
    offer: str = ""


def state(settings: Settings, chat: str) -> State:
    try:
        parts = card_path(settings, chat).read_text().strip().split("|")
    except FileNotFoundError:
        return State()
    parts += [""] * 4
    idx = int(parts[1]) if parts[1].isdigit() else 0
    return State(parts[0], idx, parts[2], parts[3])


def remember(settings: Settings, chat: str, mode: str, idx: int, offer: str, msg: str) -> None:
    _write(card_path(settings, chat), f"{mode}|{idx}|{msg}|{offer}\n")


def saved(settings: Settings, chat: str) -> list[str]:
    try:
        return [ln for ln in saved_path(settings, chat).read_text().splitlines() if ln]
    except FileNotFoundError:
        return []


def toggle_saved(settings: Settings, chat: str, offer: str) -> str | None:
    """Отложить или вернуть; None, если id не годится.

    Id берется из состояния карточки, а не из кнопки, и все равно
    проверяется: в файл идут только цифры.
    """
    if not offer.isdigit() or not offer.isascii():
        return None
    ids = saved(settings, chat)
    if offer in ids:
        ids = [i for i in ids if i != offer]
        toast = "убрано из отложенных"
    else:
        # Новое сверху: последнее отложенное обычно и нужно первым.
        ids = [offer] + ids
        toast = "отложено, список /saved"
    _write(saved_path(settings, chat), "".join(f"{i}\n" for i in ids))
    return toast


# ---------------------------------------------------------------------
# Карточка
# ---------------------------------------------------------------------

def keyboard(mode: str, is_saved: bool, price: str, rooms: str) -> Keyboard:
    def mark(flag: bool, label: str) -> str:
        return "● " + label if flag else label
    if mode == "pick":
        return [
            [("◀", "c<"), ("★ убрать" if is_saved else "★ отложить", "cs"), ("▶", "c>")],
            [("цена -10", "cp-"), ("до " + price + " тыс" if price else "цена любая", "noop"),
             ("+10", "cp+")],
            [(mark(rooms == "1", "1к"), "cr1"), (mark(rooms == "2", "2к"), "cr2"),
             (mark(rooms == "1,2", "1-2к"), "cr12")],
            [("еще фильтры", "filters"), ("отложенные", "sv")],
        ]
    return [
        [("◀", "v<"), ("★ убрать" if is_saved else "★ вернуть", "vs"), ("▶", "v>")],
        [("к подбору", "s")],
    ]


def caption(head: str, r: Row, is_saved: bool, tail: str, role: str = "owner") -> str:
    # Балл считается по весам владелицы (что важно ей), гостю он ничего
    # не говорит о его квартире.
    score = f" · {r.score} баллов" if role == "owner" else ""
    if not r.active:
        tail += "\n\nСнято с публикации, по ссылке может не открыться."
    entry = ""
    if r.entry:
        entry = f"\n\nПри заселении {r.entry} руб\n" + r.parts.replace("~", "\n")
    star = "\n★ отложено" if is_saved else ""
    return f"""{head}
{r.district}{score}
{r.rooms} комн., {r.area} м2
{r.price} руб/мес
метро {r.metro} мин пешком, животные {filters.pets_human(r.pets)}{entry}{star}{tail}

{r.url}"""


ORDER = "order by o.score desc, o.offer_id"


def show(ctx: Ctx, mode: str, idx: int, msg_id: str = "") -> bool:
    """Карточка номер idx в режиме mode; False, если такого варианта нет.

    Пустой msg_id значит новое сообщение, иначе правка на месте.
    """
    if idx < 0:
        return False
    s, chat = ctx.settings, ctx.chat
    prefetch = ""
    if mode == "pick":
        f = filters.load(s, chat)
        cond, params = filters.where(f)
        # Один запрос вместо трех у старого бота: всего вариантов через
        # окно, а вторая строка дает фото следующей карточки для кеша.
        rows = db.query(f"select {CARD_COLS}, count(*) over () from marts.offer_pick o "
                        f"where {cond} {ORDER} limit 2 offset %s", [*params, idx])
        if not rows:
            return False
        row = to_row(rows[0][:13])
        head = f"Вариант {idx + 1} из {rows[0][13]}"
        tail = f"\n\nОтбор: {filters.human(f)}"
        if len(rows) > 1:
            prefetch = rows[1][12] or ""
    else:
        ids = saved(s, chat)
        if idx >= len(ids):
            return False
        # Отложенное могло уйти из подбора: снято, подорожало, выпало из
        # фильтров. Поэтому ищется без фильтров, по одному id.
        rows = db.query(f"select {CARD_COLS} from marts.offer_pick o "
                        "where o.source = 'cian' and o.offer_id = %s limit 1", [ids[idx]])
        if not rows:
            return False
        row = to_row(rows[0])
        head = f"Отложенное {idx + 1} из {len(ids)}"
        tail = ""
    is_saved = row.offer_id in saved(s, chat)
    f = filters.load(s, chat)
    ctx.replies.append(Card(
        caption=caption(head, row, is_saved, tail, ctx.role),
        keyboard=keyboard(mode, is_saved, f.get("price", ""), f.get("rooms", "")),
        photo=row.photo, msg_id=msg_id, prefetch=prefetch,
        on_sent=partial(remember, s, chat, mode, idx, row.offer_id),
        label=f"карточка {mode} {idx}: {row.district}, {row.price} руб"))
    return True


# ---------------------------------------------------------------------
# Команды и кнопки
# ---------------------------------------------------------------------

def cmd_flats(ctx: Ctx, p=None) -> None:
    started = filters.defaults(ctx.settings, ctx.chat)
    if started is not None:
        ctx.reply(f"""Фильтров у вас ещё не было, поставил начальные: 1-2 комнаты и цена до
{started} тыс. Это медиана по таким квартирам в сегодняшней выдаче, а не
чьё-то мнение. Меняйте кнопками под карточкой.""")
    if not show(ctx, "pick", 0):
        ctx.reply(f"""Подходящего нет

Отбор: {filters.human(filters.load(ctx.settings, ctx.chat))}

Ослабить: /filter""")


def cmd_saved(ctx: Ctx, p=None) -> None:
    if not saved(ctx.settings, ctx.chat):
        ctx.reply("""Отложенных пока нет

Откройте подбор (/flats) и нажмите «★ отложить» под понравившимся.""")
        return
    show(ctx, "saved", 0)


def button(ctx: Ctx, data: str, msg_id: str) -> bool:
    """Кнопка карусели; False, если кнопка не отсюда."""
    s, chat = ctx.settings, ctx.chat
    st = state(s, chat)
    mode = st.mode or "pick"
    if data in ("c<", "v<"):
        if st.idx <= 0:
            ctx.answer("это первый")
            return True
        ctx.answer()
        show(ctx, mode, st.idx - 1, msg_id)
    elif data in ("c>", "v>"):
        # Ответ на нажатие уходит до карточки: индикатор гаснет сразу, а
        # не после загрузки фото.
        ctx.answer()
        toast = ctx.replies[-1]
        if not show(ctx, mode, st.idx + 1, msg_id):
            toast.text = "дальше ничего нет"
    elif data in ("cs", "vs"):
        ctx.answer(toggle_saved(s, chat, st.offer) or "не вышло")
        # В отложенных убранный вариант пропадает из списка, поэтому номер
        # остается, а на его место встает следующий. Убран последний, тогда
        # шаг назад, а если список опустел, карточка остается как была.
        if st.mode != "saved":
            show(ctx, "pick", st.idx, msg_id)
        elif not show(ctx, "saved", st.idx, msg_id) and st.idx > 0:
            show(ctx, "saved", st.idx - 1, msg_id)
    elif data in ("cp-", "cp+"):
        cur = int(filters.load(s, chat).get("price", 100))
        cur = min(999, max(10, cur - 10 if data == "cp-" else cur + 10))
        filters.put(s, chat, "price", str(cur))
        ctx.answer(f"цена до {cur} тыс")
        show(ctx, "pick", 0, msg_id)
    elif data in ("cr1", "cr2", "cr12"):
        cur = {"cr1": "1", "cr2": "2", "cr12": "1,2"}[data]
        filters.put(s, chat, "rooms", cur)
        ctx.answer(f"комнат: {cur}")
        show(ctx, "pick", 0, msg_id)
    elif data == "sv":
        ctx.answer("отложенные")
        if not saved(s, chat):
            ctx.reply("Отложенных пока нет. Нажмите «★ отложить» под понравившимся.")
        else:
            show(ctx, "saved", 0, msg_id)
    elif data in ("s", "first", "next"):
        # next и first живут на сообщениях до карусели: нажатие на старой
        # кнопке открывает карусель, а не молчит.
        ctx.answer("ищу")
        if st.mode == "saved" and msg_id and data == "s":
            show(ctx, "pick", 0, msg_id)
        else:
            cmd_flats(ctx)
    else:
        return False
    return True
