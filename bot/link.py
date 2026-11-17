"""Ссылка на объявление в чате: карточка с баллом, даже если его нет в базе.

Есть в базе, значит карточка сразу, такая же, как в подборе, с оценкой
цены строкой ниже. Нет в базе (вышло после утреннего обхода), значит
страница открывается на площадке в своем профиле браузера
(etl/link_fetch.py), и карточка приходит следом, через полминуты.

Ссылок в сутки не больше LIMIT на всех: это запросы к площадке сверх
обхода, и счет им должен быть. Если площадка не пустила, до конца суток
по ссылкам больше не ходим, чтобы не задеть обход.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

from bot import cards, db
from bot.actions import Action, Card, Ctx, Fetch, Reply
from bot.config import Settings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import box_price  # noqa: E402

LINK = re.compile(r"cian\.ru/rent/flat/(\d{5,12})")
LIMIT = 10

# Ссылки, которые открываются прямо сейчас: вторая такая же, присланная
# в нетерпении, не должна открывать страницу второй раз.
PENDING: set[str] = set()


def offer_id(text: str) -> str | None:
    m = LINK.search(text)
    return m.group(1) if m else None


# ---------------------------------------------------------------------
# Счет и стоп на сутки
# ---------------------------------------------------------------------

def _count_path(settings: Settings, day: str) -> Path:
    return settings.state_dir / f"links.{day}"


def _blocked_path(settings: Settings, day: str) -> Path:
    return settings.state_dir / f"links.blocked.{day}"


def used(settings: Settings, day: str) -> int:
    try:
        return int(_count_path(settings, day).read_text().strip() or 0)
    except (FileNotFoundError, ValueError):
        return 0


def take(settings: Settings, day: str) -> None:
    cards._write(_count_path(settings, day), f"{used(settings, day) + 1}\n")


def mark_blocked(settings: Settings, day: str) -> None:
    cards._write(_blocked_path(settings, day), "1\n")


def blocked(settings: Settings, day: str) -> bool:
    return _blocked_path(settings, day).exists()


# ---------------------------------------------------------------------
# Открытое по ссылке хранится у бота: в базе его нет до обхода, а
# отложить его можно, и /saved должен его показать.
# ---------------------------------------------------------------------

def cache_path(settings: Settings, oid: str) -> Path:
    return settings.state_dir / "links" / f"{oid}.json"


def remember(settings: Settings, oid: str, result: dict) -> None:
    cards._write(cache_path(settings, oid),
                 json.dumps({**result, "day": date.today().isoformat()}, ensure_ascii=False))


def cached(settings: Settings, oid: str) -> dict | None:
    try:
        return json.loads(cache_path(settings, oid).read_text())
    except (FileNotFoundError, ValueError):
        return None


# ---------------------------------------------------------------------
# Карточка
# ---------------------------------------------------------------------

# Входит ли в подбор, спрашивается у marts.offer_pick, а не условиями
# здесь: определение подбора одно, в модели dbt.
IN_PICK = """, exists (select 1 from marts.offer_pick p
                       where p.source = o.source and p.offer_id = o.offer_id)"""

PRICE_SQL = "select ask, estimate, lo80, hi80 from ml.price_estimate where offer_id = %s"


def keyboard(oid: str, is_saved: bool):
    return [[("★ убрать" if is_saved else "★ отложить", f"ls{oid}"), ("весь подбор", "s")]]


def from_base(oid: str):
    """(строка карточки, пометки) из витрины; [] если нет, None если база молчит."""
    rows = db.query(f"select {cards.CARD_COLS}{IN_PICK} from marts.offer_scored o "
                    "where o.source = 'cian' and o.offer_id = %s limit 1", [oid])
    if rows is None:
        return None
    if not rows:
        return []
    notes = []
    if not rows[0][14]:
        notes.append("В подбор не входит: повтор другого объявления, комната, посуточно "
                     "или без цены.")
    price = db.query(PRICE_SQL, [oid])
    if price:
        notes.insert(0, box_price.short_line(*price[0]))
    return cards.to_row(rows[0][:14]), notes


def card(ctx: Ctx, oid: str, msg_id: str = "") -> bool:
    """Карточка объявления по ссылке; False, если ни в базе, ни у бота его нет."""
    got = from_base(oid)
    if got:
        row, notes = got
        head = "По ссылке"
    else:
        c = cached(ctx.settings, oid)
        if c is None:
            return False
        row = cards.to_row(c["row"])
        head = "По ссылке, со страницы ЦИАНа"
        notes = fetched_notes(c)
    is_saved = oid in cards.saved(ctx.settings, ctx.chat)
    tail = "\n\n" + "\n".join(notes) if notes else ""
    ctx.replies.append(Card(
        caption=cards.caption(head, row, is_saved, tail, ctx.role),
        keyboard=keyboard(oid, is_saved), photo=row.photo, msg_id=msg_id,
        label=f"карточка по ссылке {oid}: {row.district}, {row.price} руб"))
    return True


def fetched_notes(c: dict) -> list[str]:
    notes = []
    if c.get("repair"):
        notes.append(f"Ремонт по странице: {c['repair']}")
    notes.append("В базе его пока нет, открыто на ЦИАНе "
                 f"{c['day'][8:10]}.{c['day'][5:7]}. Балл по тем же правилам, "
                 "в статистику панели не входит.")
    return notes


def cmd_link(ctx: Ctx, oid: str) -> None:
    got = from_base(oid)
    if got is None:
        ctx.reply("База не отвечает\n\nПришлите ссылку через пару минут.")
        return
    if got:
        card(ctx, oid)
        return
    s, day = ctx.settings, date.today().isoformat()
    c = cached(s, oid)
    if c and c.get("day") == day:
        # Сегодня уже открывали: второй раз на площадку незачем.
        card(ctx, oid)
        return
    if oid in PENDING:
        ctx.reply("Уже открываю\n\nКарточка этого объявления придет следом.")
        return
    if blocked(s, day):
        ctx.reply("По ссылкам до завтра не хожу\n\nСегодня ЦИАН уже не пустил на страницу "
                  "объявления, а лишние запросы могут помешать сбору. Если объявление в "
                  "выдаче, завтра оно будет в базе.")
        return
    if used(s, day) >= LIMIT:
        ctx.reply(f"Предел ссылок на сегодня\n\nОткрываю не больше {LIMIT} объявлений в "
                  "сутки: это запросы к ЦИАНу сверх обхода. Если объявление в выдаче, завтра "
                  "оно будет в базе.")
        return
    take(s, day)
    PENDING.add(oid)
    ctx.note(f"ссылка {oid}: в базе нет, открываю на площадке")
    ctx.reply("Открываю на ЦИАНе\n\nЭтого объявления в базе пока нет. Карточка придет "
              "через полминуты.")
    ctx.replies.append(Fetch(oid, ctx.role))


def after_fetch(settings: Settings, chat: str, role: str, oid: str, result: dict,
                note=lambda text: None) -> list[Action]:
    """Что прислать, когда страница открыта (или не открылась)."""
    PENDING.discard(oid)
    status = result.get("status")
    note(f"ссылка {oid}: {status} {result.get('message', '')}".rstrip())
    if status == "ok":
        remember(settings, oid, result)
        ctx = Ctx(settings, chat, role, note=note)
        card(ctx, oid)
        return ctx.replies
    if status == "blocked":
        mark_blocked(settings, date.today().isoformat())
        return [Reply("ЦИАН не пустил\n\nСтраница объявления не открылась. До завтра по "
                      "ссылкам больше не хожу, чтобы не помешать сбору. Если объявление в "
                      "выдаче, завтра оно будет в базе.")]
    if status == "not_found":
        return [Reply("Объявления нет\n\nНа ЦИАНе его нет: снято с публикации или в "
                      "ссылке ошибка.")]
    if status == "not_rent":
        return [Reply("Не аренда квартиры\n\nТакие объявления я не оцениваю.")]
    return [Reply("Страница не открылась\n\nПопробуйте позже, подробности в журнале бота.")]


def button(ctx: Ctx, data: str, msg_id: str) -> bool:
    """★ под карточкой по ссылке; id в самой кнопке, а не в состоянии
    карусели: карточка по ссылке живет рядом с подбором и его не сбивает."""
    if not data.startswith("ls"):
        return False
    oid = data[2:]
    ctx.answer(cards.toggle_saved(ctx.settings, ctx.chat, oid) or "не вышло")
    card(ctx, oid, msg_id)
    return True

