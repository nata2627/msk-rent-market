"""Команды бота: текст на входе, ответы на выходе, без телеграма.

Разбор белым списком, как у старого бота: аккаунт могут увести, и
опечатка не должна превращаться в действие. Тексты ответов перенесены из
scripts/box_bot.sh дословно.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from bot import status
from bot.config import Settings


@dataclass
class Reply:
    text: str
    keyboard: dict | None = None


@dataclass
class Ctx:
    settings: Settings
    chat: str
    role: str  # owner или guest; чужой сюда не доходит
    replies: list[Reply] = field(default_factory=list)

    def reply(self, text: str, keyboard: dict | None = None) -> None:
        self.replies.append(Reply(text, keyboard))


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
/saved — отложенные варианты
/filter — ваши фильтры подбора, с кнопками
/market — числа рынка за сегодня
/price — оценка цены: ссылка на объявление или «2к 45 м2 Хамовники»
/ask — вопрос о рынке словами: «сколько однушек в Марьино ушло за месяц»
/status — собрано ли сегодня и насколько свежи данные

Подбор можно задать словами, без команды: двушка до 90 у метро с котом

Фильтры у вас свои: что ни выставите, у владелицы ничего не изменится.
Команды, которые что-то делают на машине сбора, мне недоступны."""

HELP_OWNER = """Что я умею
посмотреть

/status — что сейчас с панелью и машиной
/flats — подбор карточками, листается кнопками
/saved — отложенные варианты
/filter — посмотреть и поменять фильтры подбора
/market — числа рынка за сегодня
/price — оценка цены: ссылка на объявление или «2к 45 м2 Хамовники»
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


# Команда и ее русские имена ведут в один обработчик, как в dispatch
# старого бота.
HANDLERS = {
    "/start": cmd_help,
    "/help": cmd_help,
    "/status": cmd_status,
    "/статус": cmd_status,
}

# Еще не перенесенные команды. Пока бот живет только на тестовом токене,
# честнее сказать это прямо, чем ответить «команды нет». Список пустеет
# по ходу этапа 2 и исчезает вместе с ним.
PENDING = {
    "/flats", "/подбор", "/saved", "/отложенные",
    "/undo", "/вернуть", "/filter", "/фильтр", "/отбор", "/market", "/рынок",
    "/price", "/цена", "/ask", "/вопрос", "/log", "/журнал", "/pulse", "/итог",
    "/clean", "/чистка", "/unlock", "/замок", "/catchup", "/догнать",
    "/dbup", "/база",
}


def dispatch(ctx: Ctx, text: str) -> list[Reply]:
    p = parse(text)
    handler = HANDLERS.get(p.cmd)
    if handler:
        handler(ctx, p)
    elif p.cmd in PENDING or not p.cmd.startswith("/"):
        ctx.reply(f"Команда «{p.cmd or text}» еще переносится на новый бот")
    else:
        ctx.reply(f"Команды «{p.cmd}» нет. Что умею — /help")
    return ctx.replies
