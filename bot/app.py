"""Разговор с телеграмом: длинный опрос, отправка, журнал задержек.

Один процесс держит getUpdates открытым, поэтому ответ уходит сразу, без
щели между заходами старого бота. Решения о том, что ответить, здесь не
принимаются: это access и commands.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

from telegram import LinkPreviewOptions, Update
import httpx
from telegram.request import HTTPXRequest
from telegram.ext import Application, ContextTypes, MessageHandler, TypeHandler, filters

from bot import config, status
from bot.access import handle_message
from bot.commands import Reply
from bot.liveness import ChatLocks, Pulse, guarded
from bot.render import render

log = logging.getLogger("bot")

ALLOWED_UPDATES = ["message", "callback_query"]

# httpx по умолчанию закрывает соединение после 5 с простоя, и ответ на
# первое нажатие после паузы платил за новое подключение к телеграму:
# замер 02.11.2026 на getMe 0,65-0,76 с против 0,16 с на открытом. Человек
# в боте почти всегда делает паузу дольше 5 с, поэтому соединение держится
# пять минут.
KEEPALIVE_S = 300
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def note(text: str) -> None:
    log.info(text)


def setup_logging(settings: config.Settings) -> None:
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(settings.log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%d.%m %H:%M:%S"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    # httpx пишет на уровне INFO адрес каждого запроса, а в адресе API
    # телеграма лежит токен. Выше WARNING его журнал не пускаем.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def offset_file(settings: config.Settings):
    return settings.state_dir / "offset"


async def skip_backlog(app: Application) -> None:
    """Первый запуск: все, что писали боту до него, пропускается.

    Выполнить сейчас команды недельной давности хуже, чем не выполнить.
    Признак первого запуска тот же, что у старого бота: нет отметки опроса.
    """
    settings: config.Settings = app.bot_data["settings"]
    marker = offset_file(settings)
    if marker.exists():
        return
    updates = await app.bot.get_updates(timeout=0, allowed_updates=ALLOWED_UPDATES)
    for u in updates:
        text = u.message.text if u.message else (u.callback_query.data if u.callback_query else "")
        note(f"первый запуск, пропускаю старое: {text}")
    last = updates[-1].update_id + 1 if updates else 0
    if updates:
        # Подтверждение телеграму: следующий getUpdates с этой отметкой
        # больше не отдаст пропущенное.
        await app.bot.get_updates(offset=last, timeout=0, allowed_updates=ALLOWED_UPDATES)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{last}\n")


async def remember_offset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Отметка нужна только как признак «уже запускался» и для разбора
    # журнала. Сам опрос держит отметку в памяти. События обрабатываются
    # параллельно, поэтому пишется наибольшая, а не последняя.
    top = max(context.bot_data.get("offset", 0), update.update_id + 1)
    context.bot_data["offset"] = top
    offset_file(context.bot_data["settings"]).write_text(f"{top}\n")


async def send(context: ContextTypes.DEFAULT_TYPE, chat: str, reply: Reply) -> None:
    await context.bot.send_message(
        chat_id=chat, text=render(reply.text), parse_mode="HTML",
        link_preview_options=NO_PREVIEW, reply_markup=reply.keyboard)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    started = time.monotonic()
    settings: config.Settings = context.bot_data["settings"]
    msg = update.message
    chat = str(msg.chat_id)
    # Перевод строки и табуляция становятся пробелом, как в box_tg.py:
    # разбор аргументов по словам на этом построен.
    text = (msg.text or "").replace("\t", " ").replace("\n", " ")
    async with context.bot_data["locks"](chat):
        replies = await guarded(handle_message, settings, chat, text, msg.date.timestamp(),
                                time.time(), note, label=text)
        thought = time.monotonic() - started
        for r in replies:
            await send(context, chat, r)
    if replies:
        # Обработка: от получения события процессом до ухода последнего
        # ответа. Возраст: от отправки человеком, по часам телеграма с
        # точностью до секунды, в него входит доставка события.
        spent = time.monotonic() - started
        age = time.time() - msg.date.timestamp()
        note(f"ответ в чат {chat}: {replies[0].text.splitlines()[0]} "
             f"| обработка {spent:.3f} с (логика {thought:.3f}, отправка {spent - thought:.3f}), "
             f"возраст {age:.0f} с")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Сюда попадает то, что упало вне guarded: отправка ответа, разбор
    # события. Процесс живет дальше, а след остается в журнале.
    log.error("ошибка вне обработчика", exc_info=context.error)


async def start_pulse(app: Application) -> None:
    await skip_backlog(app)
    pulse = Pulse(app.bot_data["settings"].state_dir / "pulse", on_beat=status.refresh_fresh)
    pulse.watch()
    # Задача пульса живет вне учета библиотеки: она стартует до опроса,
    # а останавливается в stop_pulse вместе с процессом.
    app.bot_data["pulse"] = asyncio.get_running_loop().create_task(pulse.run())


async def stop_pulse(app: Application) -> None:
    task = app.bot_data.get("pulse")
    if task:
        task.cancel()


def build(settings: config.Settings) -> Application:
    # concurrent_updates: разные чаты не ждут друг друга, а порядок внутри
    # одного чата держит ChatLocks.
    request = HTTPXRequest(connection_pool_size=8, httpx_kwargs={
        "limits": httpx.Limits(max_connections=8, max_keepalive_connections=8,
                               keepalive_expiry=KEEPALIVE_S)})
    app = (Application.builder().token(settings.token).request(request)
           .concurrent_updates(True)
           .post_init(start_pulse).post_shutdown(stop_pulse).build())
    app.bot_data["settings"] = settings
    app.bot_data["locks"] = ChatLocks()
    app.add_error_handler(on_error)
    app.add_handler(TypeHandler(Update, remember_offset), group=-1)
    app.add_handler(MessageHandler(filters.TEXT, on_message))
    return app


def run_once(settings: config.Settings, text: str) -> int:
    """Команда руками, ответ уходит владелице: проверить, не доставая телефон."""
    replies = handle_message(settings, settings.owner, text, time.time(), time.time(), note)

    async def go() -> None:
        app = Application.builder().token(settings.token).build()
        async with app:
            for r in replies:
                await app.bot.send_message(
                    chat_id=settings.owner, text=render(r.text), parse_mode="HTML",
                    link_preview_options=NO_PREVIEW, reply_markup=r.keyboard)

    asyncio.run(go())
    print(f"команда «{text}» выполнена, ответ ушёл в чат")
    return 0


def main() -> int:
    os.environ["TZ"] = "Europe/Moscow"
    time.tzset()
    settings = config.load()
    if not settings.token or not settings.owner:
        print(f"канал не настроен: нужны {config.TOKEN_KEY[settings.env]} и NOTIFY_TG_CHAT")
        return 1
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(settings)
    if sys.argv[1:2] == ["--run"]:
        return run_once(settings, " ".join(sys.argv[2:]))
    note(f"старт, режим {settings.env}")
    # bootstrap_retries=-1: макбук просыпается без сети, и первые запросы
    # падают. Ждать сеть внутри процесса дешевле, чем выходить и ждать,
    # пока launchd поднимет его заново.
    build(settings).run_polling(allowed_updates=ALLOWED_UPDATES, timeout=50,
                                drop_pending_updates=False, bootstrap_retries=-1)
    note("остановлен")
    return 0
