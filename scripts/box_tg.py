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

    box_tg.py poll <offset>            строка на событие, поля через \x1f
    box_tg.py send <текст> [клавиатура]
    box_tg.py edit <msg_id> <текст> [клавиатура]
    box_tg.py photo <url> <подпись> [чат]
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
import uuid
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


# Разделитель полей — \x1f, а не табуляция.
#
# Табуляция для bash — ПРОБЕЛЬНЫЙ разделитель: два таба подряд
# схлопываются в один, и пустое поле исчезает вместе с ними. У обычного
# сообщения поле «id нажатия» пустое, поэтому текст команды уезжал на
# позицию влево и до разбора доходил пустым — бот отвечал «я понимаю
# только команды» на любую команду. Кнопки при этом работали: у них это
# поле непустое. \x1f пробельным не считается, и пустые поля остаются на
# своих местах.
SEP = "\x1f"


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
            print(SEP.join([str(uid), "msg", str(chat),
                            str(m.get("date", 0)), "", text]))
        elif "callback_query" in u:
            c = u["callback_query"]
            msg = c.get("message") or {}
            chat = (msg.get("chat") or {}).get("id")
            # Нажатие кнопки несёт с собой id сообщения: по нему экран
            # фильтров правится на месте, а не засыпается копиями.
            print(SEP.join([str(uid), "cb", str(chat),
                            str(msg.get("date", 0)),
                            f"{c.get('id')}:{msg.get('message_id')}",
                            c.get("data", "")]))


def send_photo(url: str, caption: str, chat: str) -> bool:
    """Фотография объявления в чат.

    Сначала пробуем отдать телеграму ссылку: тогда картинку качает он
    сам, и бокс не тратит ни трафика, ни памяти. Но ЦИАН его фетчеру не
    отвечает — телеграм возвращает «failed to get HTTP URL content», —
    поэтому запасной путь обязателен: качаем сами и отправляем файлом.

    Почему ссылка всё-таки пробуется первой: она дешевле, и если
    площадка однажды перестанет резать чужие запросы, всё станет
    работать само.
    """
    r = call("sendPhoto", chat_id=chat, photo=url,
             caption=render(caption), parse_mode="HTML")
    if r.get("ok"):
        return True

    # Качаем сами. Заголовок браузера — потому что без него площадка
    # отдаёт отказ: ровно по этой причине и не справился телеграм.
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/155.0.0.0 Safari/537.36"),
            "Referer": "https://www.cian.ru/",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            # Предел телеграма на фотографию — 10 МБ, и качать больше
            # незачем: память бокса дороже картинки.
            blob = resp.read(10 * 1024 * 1024)
    except (urllib.error.URLError, OSError, TimeoutError):
        return False
    if not blob:
        return False

    # Multipart руками: ради одной отправки тащить зависимость незачем, а
    # в боксе лишних пакетов и нет.
    boundary = "----rentmsk" + uuid.uuid4().hex
    parts = []
    for name, value in (("chat_id", str(chat)),
                        ("caption", render(caption)),
                        ("parse_mode", "HTML")):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f'name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                 'name="photo"; filename="flat.jpg"\r\n'
                 "Content-Type: image/jpeg\r\n\r\n".encode())
    parts.append(blob)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)

    try:
        req = urllib.request.Request(
            f"{API}/bot{TOKEN}/sendPhoto", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()).get("ok", False)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return False


def main() -> int:
    if not TOKEN or not CHAT:
        return 0
    if len(sys.argv) < 2:
        return 0
    what = sys.argv[1]

    if what == "poll":
        cmd_poll(sys.argv[2] if len(sys.argv) > 2 else "0")
    elif what == "send":
        # Четвёртым доводом можно указать чат: ответ уходит тому, кто
        # спросил. Без этого гость писал бы боту, а ответ приходил бы
        # владелице — и выглядело бы это как молчание.
        to = sys.argv[4] if len(sys.argv) > 4 else CHAT
        r = call("sendMessage", chat_id=to, text=render(sys.argv[2]),
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
    elif what == "photo":
        print("ok" if send_photo(sys.argv[2], sys.argv[3],
                                 sys.argv[4] if len(sys.argv) > 4 else CHAT) else "fail")
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
