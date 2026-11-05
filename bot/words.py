"""Слова вместо кнопок: подбор фразой, /ask и /price.

Разбор и ответы живут в scripts/box_nl.py, box_ask.py и box_price.py, их
же зовет старый бот. Здесь они вызываются функциями, а не подпроцессом:
без нового python3 на каждую фразу и без docker exec psql на каждый
запрос. Запросы идут через общее соединение bot.db, а ответ получает
строки в том виде, в каком их печатал psql, поэтому разметка ответов
остается той же.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from bot import cards, db, filters
from bot.actions import Ctx
from bot.config import Settings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import box_ask  # noqa: E402
import box_nl  # noqa: E402
import box_price  # noqa: E402


def psql_text(v) -> str:
    """Значение так, как его печатает psql -qtA."""
    if v is None:
        return ""
    if v is True or v is False:
        return "t" if v else "f"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    return str(v)


def rows_text(rows: list[tuple]) -> list[list[str]]:
    return [[psql_text(v) for v in r] for r in rows]


def districts() -> list[str]:
    return [r[0] for r in db.query(filters.DISTRICTS_SQL) or []]


def _use_state(settings: Settings) -> None:
    # Квота фраз и токен GigaChat лежат в папке состояния своего бота:
    # тестовый не тратит лимит боевого и не пишет в data/bot.
    box_nl.STATE = settings.state_dir


def _alarm(settings: Settings):
    def alarm(err: str) -> None:
        env = dict(os.environ)
        if settings.dev:
            # Тревога из проверки пришла бы от боевого бота.
            env["NOTIFY_TG_TOKEN"] = settings.token
        box_nl.alarm(err, env)
    return alarm


# ---------------------------------------------------------------------
# /ask
# ---------------------------------------------------------------------

# Переменные psql (:'district', :days) становятся параметрами запроса.
# Приведения типа (::int) не трогаются: двоеточие перед ними двойное.
PSQL_VAR = re.compile(r":'(\w+)'|(?<!:):(\w+)\b")
ASK_VARS = {"rooms", "district", "days", "order", "limit"}
CAST = {"days": "::int", "limit": "::int"}


def to_params(sql: str) -> str:
    def sub(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        if name not in ASK_VARS:
            return m.group(0)
        return f"%({name})s" + ("" if m.group(1) else CAST.get(name, ""))
    return PSQL_VAR.sub(sub, sql)


ASK_SQL = {fn: to_params(sql) for fn, sql in box_ask.SQL.items()}


def ask_sql(fn: str, args: dict) -> list[list[str]]:
    v = {"rooms": "{" + ",".join(map(str, args.get("rooms", box_ask.ROOMS_DEFAULT))) + "}",
         "district": args.get("district", ""), "days": int(args.get("days", 7)),
         "order": args.get("order", "cheap"), "limit": int(args.get("limit", 5))}
    rows = db.query(ASK_SQL[fn], v)
    if rows is None:
        raise RuntimeError("запрос не прошел, подробности в журнале бота")
    return rows_text(rows)


def cmd_ask(ctx: Ctx, text: str) -> None:
    if not text:
        ctx.reply("Задайте вопрос после команды. Например: /ask сколько двушек в Хамовниках подешевело за неделю")
        return
    _use_state(ctx.settings)
    out = box_ask.answer(ctx.chat, ctx.role, text, districts, ask_sql, _alarm(ctx.settings))
    if not out:
        ctx.note("вопрос: пусто")
        ctx.reply("Не получилось ответить, попробуйте позже.")
        return
    head, _, body = out.partition("\n")
    ctx.note(f"вопрос: {head}")
    ctx.reply(body)


# ---------------------------------------------------------------------
# /price
# ---------------------------------------------------------------------

def price_query(sql: str) -> list[list[str]]:
    return rows_text(db.query(sql) or [])


def cmd_price(ctx: Ctx, text: str) -> None:
    # Оценка читается из готовых таблиц ml, модели и площадки в пути нет.
    ctx.reply(box_price.answer(text, price_query))


# ---------------------------------------------------------------------
# Подбор фразой
# ---------------------------------------------------------------------

def cmd_phrase(ctx: Ctx, text: str) -> None:
    """«двушка до 90 у метро с котом». box_nl разбирает, проверяет и пишет
    файл фильтров, прежние кладет рядом в .prev."""
    s, chat = ctx.settings, ctx.chat
    _use_state(s)
    status, unknown = box_nl.apply(str(filters.path(s, chat)), chat, ctx.role, text,
                                   districts, _alarm(s))
    # Как paste -sd, и sed 's/,/, /g' у старого бота: запятая внутри
    # непонятого тоже получает пробел.
    unk = ",".join(unknown).replace(",", ", ")
    ctx.note(f"фраза: {status}")
    if status.startswith("ok"):
        more = f"\nДля этого фильтра нет: {unk}" if unk else ""
        ctx.reply(f"""Ищу так: {filters.human(filters.load(s, chat))}{more}

Поправить кнопками: /filter
Вернуть прежние фильтры: /undo""")
        if not cards.show(ctx, "pick", 0):
            ctx.reply("Подходящего нет. Ослабить: /filter")
    elif status.startswith("limit"):
        ctx.reply(f"""Фраз на сегодня больше не разбираю, лимит {status[6:]} в сутки.
Кнопками можно сколько угодно: /filter""")
    else:
        more = f", для этого фильтра нет: {unk}" if unk else ""
        ctx.reply(f"""Не понял фразу{more}

Можно так: двушка до 90 у метро с котом. Или кнопками ниже.""")
        filters.screen(ctx)
