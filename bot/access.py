"""Кто спрашивает и что ему можно. Правила старого бота без изменений."""
from __future__ import annotations

from bot.commands import UNKNOWN_BUTTON, Action, Answer, Ctx, Reply, dispatch, parse, press
from bot.config import Settings

# Журнала здесь намеренно нет: в нем пути, ошибки и устройство машины,
# гостю это ничего не дает, а показывает лишнее.
GUEST_COMMANDS = frozenset(
    "/help /start /status /flats /saved /undo /market /filter /price /ask "
    "/статус /подбор /рынок /фильтр /отбор /цена /отложенные /вернуть /вопрос".split()
)

# Бокс мог молчать сутки, и «догнать день», отправленное вчера, сегодня
# означало бы не то.
STALE_SECONDS = 900


def who(settings: Settings, chat: str) -> str:
    # Чужой это не враг: бота находят поиском. Ему просто ничего не уходит.
    if chat == settings.owner:
        return "owner"
    if chat in settings.guests:
        return "guest"
    return "stranger"


def guest_allowed(text: str) -> bool:
    # Фраза без косой черты это подбор словами, он гостю доступен.
    if not text.startswith("/"):
        return True
    return parse(text).cmd in GUEST_COMMANDS


def is_stale(sent_at: float, now: float) -> bool:
    return now - sent_at > STALE_SECONDS


# Список доступного гостю здесь сверен с GUEST_COMMANDS (решение 6 от 02.11):
# у старого бота в нем был /log, которого гостю нельзя.
GUEST_REFUSAL = """Это команда владелицы

Показывать можно всё, а трогать машину сбора — нет. Доступно:
/status, /flats, /saved, /market, /price, /ask, /filter со своими фильтрами"""


def stale_text(text: str) -> str:
    return (f"Команда «{text}» пришла больше пятнадцати минут назад — я её не\n"
            "выполняю. Если она ещё нужна, пришлите заново.")


def handle_message(settings: Settings, chat: str, text: str, sent_at: float,
                   now: float, note) -> list[Action]:
    """Входной фильтр и разбор одного сообщения. note пишет строку в журнал."""
    role = who(settings, chat)
    if role == "stranger":
        # Ответ подтвердил бы, что бот живой и что-то умеет. В журнал да,
        # чтобы знать, что стучались.
        note(f"чужой чат {chat}, пропускаю: {text}")
        return []
    if is_stale(sent_at, now):
        note(f"команда старше пятнадцати минут, пропускаю: {text}")
        return [Reply(stale_text(text))]
    if role == "guest" and not guest_allowed(text):
        note(f"гость {chat} просил «{text}», отказано")
        return [Reply(GUEST_REFUSAL)]
    note(f"команда от {role}: {text}")
    return dispatch(Ctx(settings, chat, role, note=note), text)


def handle_button(settings: Settings, chat: str, data: str, msg_id: str, note) -> list[Action]:
    """Нажатие кнопки. Срока давности нет: кнопка меняет только фильтры и
    карточку своего чата, а гостю доступны все кнопки, лечения среди них нет."""
    role = who(settings, chat)
    if role == "stranger":
        note(f"чужой чат {chat}, пропускаю кнопку: {data}")
        return []
    note(f"кнопка: {data}")
    replies = press(Ctx(settings, chat, role, note=note), data, msg_id)
    if replies == [Answer(UNKNOWN_BUTTON)]:
        note(f"неизвестная кнопка: {data}")
    return replies
