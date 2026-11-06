"""Новое под отбор: сводка раз в день, после среза, без запроса.

Подбор по /flats показывает весь рынок под фильтром, и вчерашнее в нем
стоит вперемешку с сегодняшним. Решать про переезд мешает другое:
хорошие варианты уходят за пару дней, и узнать о них надо в день
появления, а не когда вспомнила открыть бота.

Сводка уходит владелице, когда витрины пересобраны за сегодняшний сбор и
замок обхода снят: иначе посреди прогона новое считалось бы по половине
корзин. Гостям не уходит ничего, о чем не просили, им есть /new.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from bot import cards, db, filters
from bot.actions import Ctx
from bot.config import Settings

TOP = 3

# Срез мог закончиться ночью (догон при входе в систему). Сводка в три
# часа ночи будит, а варианты за ночь не уйдут.
QUIET_BEFORE, QUIET_AFTER = 9, 22


def stamp_path(settings: Settings, chat: str):
    return settings.state_dir / f"news.{chat}"


def sent_day(settings: Settings, chat: str) -> str:
    try:
        return stamp_path(settings, chat).read_text().strip()
    except FileNotFoundError:
        return ""


def mark_sent(settings: Settings, chat: str, day: str) -> None:
    cards._write(stamp_path(settings, chat), day + "\n")


def ready_day(settings: Settings, now: datetime | None = None) -> str | None:
    """Сегодняшний день, если по нему уже можно считать новое, иначе None."""
    now = now or datetime.now()
    if not QUIET_BEFORE <= now.hour < QUIET_AFTER:
        return None
    if (settings.box_repo / "data" / "daily.lock").exists():
        return None
    rows = db.query("select mart_day, collected_day from marts.data_freshness")
    if not rows:
        return None
    mart_day, collected_day = rows[0]
    today = now.date()
    if mart_day != today or collected_day != mart_day:
        return None
    return today.isoformat()


@dataclass
class Top:
    district: str
    rooms: int
    area: int
    price: int
    metro: str


def rub(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def digest(ctx: Ctx, day: str) -> bool:
    """Сводка нового в ctx.replies; False, если под отбор нового нет."""
    f = filters.load(ctx.settings, ctx.chat)
    cond, params = filters.where(f)
    rows = db.query(
        "select o.district, o.rooms_count, round(o.total_area)::int, round(o.price)::int, "
        "coalesce(o.metro_real_walk_min::text, '?'), count(*) over () "
        f"from marts.offer_pick o where {cond} and {cards.NEW_COND} {cards.ORDER} limit {TOP}",
        params)
    if not rows:
        return False
    total = rows[0][5]
    top = [Top(*r[:5]) for r in rows]
    lines = "\n".join(f"{t.district} · {t.rooms}к, {t.area} м2 · {rub(t.price)} руб · "
                      f"метро {t.metro} мин" for t in top)
    d = date.fromisoformat(day).strftime("%d.%m")
    more = f"\n\nЛучшие {len(top)} по баллу:" if total > len(top) else "\n"
    ctx.reply(f"""Новое под ваш отбор за {d}: {total}{more}
{lines}

Отбор: {filters.human(f)}""", [[("смотреть новые", "nw")]])
    return True


def check(settings: Settings) -> tuple[str, str, Ctx] | None:
    """Что отправить владелице сейчас: (чат, день, ответы) или None.

    День помечается отправленным и тогда, когда нового нет: иначе пустой
    день спрашивал бы базу каждые десять минут до вечера.
    """
    day = ready_day(settings)
    chat = settings.owner
    if day is None or sent_day(settings, chat) == day:
        return None
    ctx = Ctx(settings, chat, "owner")
    if not digest(ctx, day):
        mark_sent(settings, chat, day)
        return None
    return chat, day, ctx


def cmd_new(ctx: Ctx, p=None) -> None:
    if not cards.show(ctx, "new", 0):
        ctx.reply(f"""Нового под отбор за последний собранный день нет

Отбор: {filters.human(filters.load(ctx.settings, ctx.chat))}

Весь подбор: /flats""")
