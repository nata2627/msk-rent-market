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
    box_tg.py edit <msg_id> <текст> [клавиатура] [чат]
    box_tg.py photo <url> <подпись> [чат]
    box_tg.py card <чат> <msg_id|-> <url фото|-> <подпись> <клавиатура>
    box_tg.py prefetch <url фото>      положить фото в кеш заранее
    box_tg.py answer <callback_id> [всплывающая подсказка]
    box_tg.py commands                 меню команд в телеграме
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import sys
import time
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
    ("flats", "подбор карточками, листается кнопками"),
    ("saved", "отложенные варианты"),
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
        elif line.startswith(("http://", "https://")):
            # Голая ссылка на объявление занимает четыре строки и ничего
            # не говорит: в ней хвост с трекингом длиннее самого адреса.
            # Показываем словом, адрес остаётся внутри.
            out.append(f'<a href="{html.escape(line, quote=True)}">Открыть объявление</a>')
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


def cmd_poll(offset: str, wait: int = 50) -> None:
    """Длинное ожидание: соединение висит, пока не придёт событие.

    Раньше опрос был мгновенным, раз в минуту из cron, и этого хватало
    для команд текстом. С кнопками так нельзя: нажатие висело бы до
    минуты с крутящимся индикатором. Телеграм сам держит соединение до
    пятидесяти секунд, и cron поднимает следующий заход сразу за этим —
    то есть ответ приходит мгновенно, а зависнуть процесс всё равно не
    может: дольше семидесяти секунд он не живёт.
    """
    r = call("getUpdates", offset=offset, timeout=max(1, min(wait, 50)),
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


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")


PHOTOS = REPO / "data" / "bot" / "photos"
PHOTOS_KEEP = 300


def fetch_photo(url: str) -> bytes:
    """Картинка объявления, скачанная самим ботом, с кешем на диске.

    Заголовок браузера нужен потому, что без него площадка отдает отказ:
    ровно поэтому телеграм не может забрать фото по ссылке сам. Предел
    телеграма на фотографию 10 МБ, и качать больше незачем.

    Кеш и повтор появились 24.10.2026. При быстром листании сервер фото
    отвечал 429 «слишком много запросов», на замере в 3 случаях из 16, и
    карточка приходила без картинки. Повтор идет с паузой, а кеш
    избавляет от второго запроса той же картинки, когда листают назад
    или открывают отложенное. Хранятся 300 последних, около 20 МБ.
    """
    if not url:
        return b""
    key = PHOTOS / (hashlib.md5(url.encode()).hexdigest() + ".jpg")
    if key.exists():
        key.touch()
        return key.read_bytes()
    blob = b""
    for pause in (0, 1.0, 2.5):
        time.sleep(pause)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Referer": "https://www.cian.ru/"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                blob = resp.read(10 * 1024 * 1024)
            break
        except urllib.error.HTTPError as e:
            # Повторять есть смысл только отказ по частоте. 404 значит,
            # что фото снято вместе с объявлением.
            if e.code != 429:
                return b""
        except (urllib.error.URLError, OSError, TimeoutError):
            return b""
    if blob:
        try:
            PHOTOS.mkdir(parents=True, exist_ok=True)
            key.write_bytes(blob)
            old = sorted(PHOTOS.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
            for p in old[:-PHOTOS_KEEP]:
                p.unlink(missing_ok=True)
        except OSError:
            pass
    return blob


def multipart(method: str, fields: dict, blob: bytes) -> dict:
    """Запрос с файлом. Multipart руками: ради него тащить зависимость
    незачем, а в боксе лишних пакетов и нет."""
    boundary = "----rentmsk" + uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        if value is None:
            continue
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f'name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                 'name="photo"; filename="flat.jpg"\r\n'
                 "Content-Type: image/jpeg\r\n\r\n".encode())
    parts.append(blob)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    try:
        req = urllib.request.Request(
            f"{API}/bot{TOKEN}/{method}", data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # Отказ телеграма несет объяснение в теле, оно нужно ниже: «не
        # изменилось» отказом не считается.
        try:
            return json.loads(e.read())
        except (OSError, json.JSONDecodeError):
            return {}
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return {}


def send_photo(url: str, caption: str, chat: str) -> bool:
    """Фотография объявления в чат.

    Сначала пробуем отдать телеграму ссылку: тогда картинку качает он
    сам, и бокс не тратит ни трафика, ни памяти. Но ЦИАН его фетчеру не
    отвечает (телеграм возвращает «failed to get HTTP URL content»),
    поэтому запасной путь обязателен: качаем сами и отправляем файлом.
    """
    r = call("sendPhoto", chat_id=chat, photo=url,
             caption=render(caption), parse_mode="HTML")
    if r.get("ok"):
        return True
    blob = fetch_photo(url)
    if not blob:
        return False
    return multipart("sendPhoto", {"chat_id": str(chat), "caption": render(caption),
                                   "parse_mode": "HTML"}, blob).get("ok", False)


def card(chat: str, msg_id: str, photo: str, caption: str, kb: str | None) -> str:
    """Карточка карусели: одно сообщение, которое листается на месте.

    Листание правит фото и подпись того же сообщения (editMessageMedia),
    поэтому чат не засыпается вариантами. Фото качает сам бот: по ссылке
    телеграм его у площадки не получит.

    Когда правка невозможна (у варианта нет фото, а прежнее сообщение
    было с фото, или наоборот, или сообщение слишком старое), старое
    удаляется и приходит новое. Печатается id сообщения, которое теперь
    держит карусель.
    """
    blob = fetch_photo(photo) if photo else b""
    text = render(caption)
    if msg_id:
        if blob:
            media = json.dumps({"type": "photo", "media": "attach://photo",
                                "caption": text, "parse_mode": "HTML"}, ensure_ascii=False)
            r = multipart("editMessageMedia", {"chat_id": str(chat), "message_id": msg_id,
                                               "media": media, "reply_markup": kb}, blob)
            if r.get("ok") or "not modified" in r.get("description", ""):
                return msg_id
        call("deleteMessage", chat_id=chat, message_id=msg_id)
    if blob:
        r = multipart("sendPhoto", {"chat_id": str(chat), "caption": text,
                                    "parse_mode": "HTML", "reply_markup": kb}, blob)
    else:
        r = call("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
                 disable_web_page_preview="true", reply_markup=kb)
    return str((r.get("result") or {}).get("message_id", ""))


def main() -> int:
    if not TOKEN or not CHAT:
        return 0
    if len(sys.argv) < 2:
        return 0
    what = sys.argv[1]

    if what == "poll":
        # Третий довод, сколько секунд ждать: заход не должен пережить
        # свою минуту на целое ожидание, иначе следующий упрется в замок.
        cmd_poll(sys.argv[2] if len(sys.argv) > 2 else "0",
                 int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 50)
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
        #
        # Чат обязателен пятым доводом. Пока он брался из .env, правка
        # уходила в чат владелицы: у неё совпадало и всё работало, а у
        # гостя телеграм не находил сообщение — кнопки не менялись, и
        # приходилось заново звать /filter.
        call("editMessageText",
             chat_id=sys.argv[5] if len(sys.argv) > 5 else CHAT,
             message_id=sys.argv[2],
             text=render(sys.argv[3]), parse_mode="HTML",
             disable_web_page_preview="true",
             reply_markup=keyboard_arg(sys.argv[4] if len(sys.argv) > 4 else None))
    elif what == "photo":
        print("ok" if send_photo(sys.argv[2], sys.argv[3],
                                 sys.argv[4] if len(sys.argv) > 4 else CHAT) else "fail")
    elif what == "card":
        a = sys.argv[2:7] + [""] * 5
        print(card(a[0], "" if a[1] == "-" else a[1], "" if a[2] == "-" else a[2],
                   a[3], keyboard_arg(a[4])))
    elif what == "prefetch":
        # Следующая карточка, пока человек смотрит текущую.
        fetch_photo(sys.argv[2] if len(sys.argv) > 2 else "")
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
