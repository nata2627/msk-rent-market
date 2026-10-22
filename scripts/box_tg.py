#!/usr/bin/env python3
"""Разговор с телеграмом: опрос, отправка, кнопки.

Зачем отдельный файл, если бот на bash. Кнопки телеграма — это JSON в
теле запроса и JSON в ответе, а собирать и разбирать его в bash значит
городить кавычки поверх кавычек. Один раз это уже стоило ошибки: id чата
искался регулярным выражением по тексту ответа и не находился, потому
что полей "id" там несколько и порядок не обещан.

Здесь только разговор с телеграмом. Что делать с командой, решает
scripts/box_bot.sh — вся работа с базой и машиной осталась там.

Разметка повторяет договор box_notify.sh: первая строка — заголовок
(жирным), строка, начатая «$ », — команда (моноширинной), остальное как
есть. Проверяется тестом, что обе стороны рисуют одинаково.

    box_tg.py poll <offset>            строка на событие, TSV
    box_tg.py send <текст> [клавиатура]
    box_tg.py edit <msg_id> <текст> [клавиатура]
    box_tg.py answer <callback_id> [всплывающая подсказка]
    box_tg.py commands                 меню команд в телеграме
"""
from __future__ import annotations

import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
API = "https://api.telegram.org"

# Меню, которое телеграм показывает по нажатию «/». Порядок осмысленный:
# сверху то, чем пользуются в дороге каждый день.
COMMANDS = [
    ("status", "что сейчас с панелью и машиной"),
    ("flats", "верх подбора под фильтрами"),
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


def from_env(name: str) -> str:
    """Значение из окружения, иначе из .env рядом с проектом."""
    if os.environ.get(name):
        return os.environ[name]
    env = REPO / ".env"
    if not env.exists():
        return ""
    for line in env.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip("\"'")
    return ""


TOKEN = from_env("NOTIFY_TG_TOKEN")
CHAT = from_env("NOTIFY_TG_CHAT")


def call(method: str, **params):
    """Запрос к телеграму. Молчит при любой беде: уведомление не имеет
    права стать новой причиной потерять день."""
    if not TOKEN:
        return {}
    data = {k: v for k, v in params.items() if v is not None}
    body = urllib.parse.urlencode(data).encode()
    # Срок жёсткий, но с запасом на длинное ожидание: getUpdates держит
    # соединение открытым, пока не придёт событие.
    timeout = 70 if method == "getUpdates" else 15
    try:
        with urllib.request.urlopen(f"{API}/bot{TOKEN}/{method}", data=body,
                                    timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return {}


def render(text: str) -> str:
    """Разметка — тот же договор, что в box_notify.sh."""
    lines = text.rstrip("\n").split("\n")
    out = []
    for i, line in enumerate(lines):
        safe = html.escape(line)
        if i == 0:
            out.append(f"<b>{safe}</b>")
        elif line.startswith("$ "):
            out.append(f"<code>{html.escape(line[2:])}</code>")
        else:
            out.append(safe)
    return "\n".join(out)


def keyboard_arg(arg: str | None) -> str | None:
    """Клавиатура приходит как путь к файлу или как сам JSON."""
    if not arg:
        return None
    # Сам JSON или путь к файлу с ним. Порядок проверки важен: длинная
    # строка JSON, поданная в Path.exists(), роняет os.stat с «File name
    # too long» — и экран с кнопками не отправляется вовсе.
    if arg.lstrip().startswith("{"):
        raw = arg
    else:
        try:
            raw = Path(arg).read_text()
        except OSError:
            return None
    # Проверяем, что это разбираемый JSON: кривая клавиатура роняет
    # отправку целиком, а сообщение о беде дороже кнопок.
    try:
        json.loads(raw)
    except json.JSONDecodeError:
        return None
    return raw


def cmd_poll(offset: str) -> None:
    """Длинное ожидание: соединение висит, пока не придёт событие.

    Раньше опрос был мгновенным, раз в минуту из cron, и этого хватало
    для команд текстом. С кнопками так нельзя: нажатие висело бы до
    минуты с крутящимся индикатором. Телеграм сам держит соединение до
    пятидесяти секунд, и cron поднимает следующий заход сразу за этим —
    то есть ответ приходит мгновенно, а зависнуть процесс всё равно не
    может: дольше семидесяти секунд он не живёт.
    """
    r = call("getUpdates", offset=offset, timeout=50,
             allowed_updates=json.dumps(["message", "callback_query"]))
    if not r.get("ok"):
        return
    for u in r.get("result", []):
        uid = u.get("update_id")
        if "message" in u:
            m = u["message"]
            chat = (m.get("chat") or {}).get("id")
            text = (m.get("text") or "").replace("\t", " ").replace("\n", " ")
            print(f"{uid}\tmsg\t{chat}\t{m.get('date', 0)}\t\t{text}")
        elif "callback_query" in u:
            c = u["callback_query"]
            msg = c.get("message") or {}
            chat = (msg.get("chat") or {}).get("id")
            # Нажатие кнопки несёт с собой id сообщения: по нему экран
            # фильтров правится на месте, а не засыпается копиями.
            print(f"{uid}\tcb\t{chat}\t{msg.get('date', 0)}\t"
                  f"{c.get('id')}:{msg.get('message_id')}\t{c.get('data', '')}")


def main() -> int:
    if not TOKEN or not CHAT:
        return 0
    if len(sys.argv) < 2:
        return 0
    what = sys.argv[1]

    if what == "poll":
        cmd_poll(sys.argv[2] if len(sys.argv) > 2 else "0")
    elif what == "send":
        r = call("sendMessage", chat_id=CHAT, text=render(sys.argv[2]),
                 parse_mode="HTML", disable_web_page_preview="true",
                 reply_markup=keyboard_arg(sys.argv[3] if len(sys.argv) > 3 else None))
        # id отправленного сообщения печатается наружу: по нему экран с
        # кнопками правится на месте, и по нему же проверяется, что
        # правка вообще работает.
        print((r.get("result") or {}).get("message_id", ""))
    elif what == "edit":
        # Правка на месте, а не новое сообщение: иначе экран фильтров
        # после пяти нажатий превращается в пять одинаковых сообщений.
        call("editMessageText", chat_id=CHAT, message_id=sys.argv[2],
             text=render(sys.argv[3]), parse_mode="HTML",
             disable_web_page_preview="true",
             reply_markup=keyboard_arg(sys.argv[4] if len(sys.argv) > 4 else None))
    elif what == "answer":
        # Без этого ответа кнопка крутится у человека на экране до
        # таймаута, даже когда всё уже сделано.
        call("answerCallbackQuery", callback_query_id=sys.argv[2],
             text=sys.argv[3] if len(sys.argv) > 3 else None)
    elif what == "commands":
        call("setMyCommands", commands=json.dumps(
            [{"command": c, "description": d} for c, d in COMMANDS],
            ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
