"""Команды бота: текст на входе, ответы на выходе, без телеграма.

Разбор белым списком, как у старого бота: аккаунт могут увести, и
опечатка не должна превращаться в действие. Тексты ответов перенесены из
scripts/box_bot.sh дословно.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from bot import cards, filters, heal, info, news, status, words
from bot.actions import Action, Answer, Card, Ctx, Reply  # noqa: F401


@dataclass(frozen=True)
class Parsed:
    cmd: str   # первое слово в нижнем регистре, без @имя_бота
    arg: str   # второе слово (awk $2 у старого бота)
    tail: str  # все после первого пробела (cut -d' ' -f2-)


def parse(text: str) -> Parsed:
    words = text.split()
    cmd = words[0].lower().split("@", 1)[0] if words else ""
    arg = words[1] if len(words) > 1 else ""
    tail = text.split(" ", 1)[1] if " " in text else ""
    return Parsed(cmd, arg, tail)


HELP_GUEST = """Что я умею

/flats — подбор карточками, листается кнопками
/new — только новое под отбор за последний день
/saved — отложенные варианты
/filter — ваши фильтры подбора, с кнопками
/market — числа рынка за сегодня
/price — оценка цены: ссылка на объявление или «2к 45 м2 Хамовники»
/price 77 — потолок цены в подборе, в тысячах
/ask — вопрос о рынке словами: «сколько однушек в Марьино ушло за месяц»
/status — собрано ли сегодня и насколько свежи данные

Подбор можно задать словами, без команды: двушка до 90 у метро с котом

Фильтры у вас свои: что ни выставите, у владелицы ничего не изменится.
Команды, которые что-то делают на машине сбора, мне недоступны."""

HELP_OWNER = """Что я умею
посмотреть

/status — что сейчас с панелью и машиной
/flats — подбор карточками, листается кнопками
/new — только новое под отбор за последний день
/saved — отложенные варианты
/filter — посмотреть и поменять фильтры подбора
/market — числа рынка за сегодня
/price — оценка цены: ссылка на объявление или «2к 45 м2 Хамовники»
/price 77 — потолок цены в подборе, в тысячах
/ask — вопрос о рынке словами: «сколько однушек в Марьино ушло за месяц»
/log — хвост журнала прогона
/pulse — прислать итог дня прямо сейчас

Подбор можно задать словами, без команды: двушка до 90 у метро с котом

починить

/clean — освободить место на диске
/unlock — снять заклинивший замок
/catchup — догнать сегодняшний день
/dbup — поднять базу, если она упала

Команды лечения сами откажутся работать, если идёт обход: второй браузер
в том же профиле сломал бы сессию площадки обоим."""


def cmd_status(ctx: Ctx, p: Parsed) -> None:
    ctx.reply(status.text(ctx.role, status.gather(ctx.settings)))


def cmd_help(ctx: Ctx, p: Parsed) -> None:
    ctx.reply(HELP_OWNER if ctx.role == "owner" else HELP_GUEST)


def cmd_market(ctx: Ctx, p: Parsed) -> None:
    ctx.reply(info.market())


def cmd_log(ctx: Ctx, p: Parsed) -> None:
    ctx.reply(info.log(ctx.settings, p.arg))


def cmd_pulse(ctx: Ctx, p: Parsed) -> None:
    ctx.reply(info.pulse(ctx.settings))


def cmd_filter(ctx: Ctx, p: Parsed) -> None:
    # Значение берется хвостом с третьего слова, а не словом: название
    # района бывает с пробелом (cut -s -d' ' -f3- у старого бота).
    rest = p.tail.split(" ", 1)
    filters.cmd_filter(ctx, p.arg.lower(), rest[1] if len(rest) > 1 else "")


# «80», «80 тысяч», «80 тыс. руб», «80000 рублей»: одно число без комнат
# и площади. Для оценки его мало, поэтому это потолок цены в фильтре подбора.
CEILING = re.compile(r"(\d{1,3}(?: ?\d{3})?)(?: ?(?:тыс\w*|т)\.?)?(?: ?(?:руб\w*|р|₽)\.?)?")


def cmd_price(ctx: Ctx, p: Parsed) -> None:
    m = CEILING.fullmatch(p.tail.strip().lower())
    if m:
        rub = int(m.group(1).replace(" ", ""))
        # Рубли вместо тысяч округляются до тысяч, а не отвергаются.
        val = round(rub / 1000) if rub >= 1000 else rub
        filters.cmd_filter(ctx, "цена", str(val))
        return
    # Хвостом целиком: в нем ссылка или «2к 45 м2 район».
    words.cmd_price(ctx, p.tail)


def cmd_ask(ctx: Ctx, p: Parsed) -> None:
    words.cmd_ask(ctx, p.tail)


DAY = re.compile(r"20[0-9][0-9]-[0-1][0-9]-[0-3][0-9]")
DAYS_BACK = {"сегодня": 0, "вчера": 1, "позавчера": 2}


def cmd_catchup(ctx: Ctx, p: Parsed) -> None:
    # Дата проверяется образцом, а не подставляется: она уходит доводом
    # в box_daily.sh. Слова дня считаются здесь же, в box_daily.sh
    # попадает уже дата.
    arg = p.arg.lower()
    if not arg:
        heal.cmd_catchup(ctx, date.today().isoformat())
    elif arg in DAYS_BACK:
        heal.cmd_catchup(ctx, (date.today() - timedelta(days=DAYS_BACK[arg])).isoformat())
    elif DAY.fullmatch(arg):
        heal.cmd_catchup(ctx, arg)
    else:
        ctx.reply("Дата должна выглядеть как 2026-10-22, или скажи вчера, позавчера. "
                  "Без даты беру сегодня.")


# Команда и ее русские имена ведут в один обработчик, как в dispatch
# старого бота.
HANDLERS = {
    "/start": cmd_help,
    "/help": cmd_help,
    "/status": cmd_status,
    "/статус": cmd_status,
    "/market": cmd_market,
    "/рынок": cmd_market,
    "/log": cmd_log,
    "/журнал": cmd_log,
    "/pulse": cmd_pulse,
    "/итог": cmd_pulse,
    "/flats": cards.cmd_flats,
    "/подбор": cards.cmd_flats,
    "/new": news.cmd_new,
    "/новое": news.cmd_new,
    "/saved": cards.cmd_saved,
    "/отложенные": cards.cmd_saved,
    "/filter": cmd_filter,
    "/фильтр": cmd_filter,
    "/отбор": cmd_filter,
    "/undo": filters.cmd_undo,
    "/вернуть": filters.cmd_undo,
    "/price": cmd_price,
    "/цена": cmd_price,
    "/ask": cmd_ask,
    "/вопрос": cmd_ask,
    "/clean": heal.cmd_clean,
    "/чистка": heal.cmd_clean,
    "/unlock": heal.cmd_unlock,
    "/замок": heal.cmd_unlock,
    "/catchup": cmd_catchup,
    "/догнать": cmd_catchup,
    "/dbup": heal.cmd_dbup,
    "/база": heal.cmd_dbup,
}

# Обычный срок обработчика минута. /dbup ждет docker compose до 120 с и
# потом базу, как старый бот, и под минутой ответил бы «не успела».
TIMEOUT_S = {"/dbup": 180, "/база": 180}


def dispatch(ctx: Ctx, text: str) -> list[Action]:
    p = parse(text)
    handler = HANDLERS.get(p.cmd)
    if handler:
        handler(ctx, p)
    elif not p.cmd.startswith("/"):
        # Подбор словами: «двушка до 90 у метро с котом».
        words.cmd_phrase(ctx, text)
    else:
        ctx.reply(f"Команды «{p.cmd}» нет. Что умею — /help")
    return ctx.replies


UNKNOWN_BUTTON = "не знаю такой кнопки"


def press(ctx: Ctx, data: str, msg_id: str) -> list[Action]:
    """Нажатие кнопки. Белый список: неизвестное ничего не меняет."""
    if cards.button(ctx, data, msg_id) or filters.button(ctx, data, msg_id):
        pass
    elif data == "noop":
        ctx.answer("это просто надпись")
    else:
        ctx.answer(UNKNOWN_BUTTON)
    return ctx.replies
