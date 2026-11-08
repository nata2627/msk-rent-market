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
from types import SimpleNamespace

from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
                      LinkPreviewOptions, Update)
import httpx
from telegram.request import HTTPXRequest
from telegram.error import BadRequest, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, ContextTypes, MessageHandler,
                          TypeHandler, filters)

from bot import config, menu, news, status
from bot.access import handle_button, handle_message
from bot.commands import TIMEOUT_S, parse
from bot.actions import Action, Answer, Card, Keyboard, Reply
from bot.liveness import HANDLER_TIMEOUT_S, ChatLocks, Pulse, guarded
from bot.photos import Photos
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


def markup(keyboard: Keyboard | None) -> InlineKeyboardMarkup | None:
    if not keyboard:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row]
                                 for row in keyboard])


async def send(context: ContextTypes.DEFAULT_TYPE, chat: str, reply: Reply) -> None:
    if reply.msg_id:
        # Правка на месте: иначе экран фильтров после пяти нажатий это пять
        # одинаковых сообщений. Не вышло (сообщение удалено или с фото),
        # тогда новое, а не молчание, как было у старого бота.
        try:
            await context.bot.edit_message_text(
                chat_id=chat, message_id=reply.msg_id, text=render(reply.text),
                parse_mode="HTML", link_preview_options=NO_PREVIEW,
                reply_markup=markup(reply.keyboard))
            return
        except BadRequest as e:
            if "not modified" in e.message:
                return
            note(f"экран не поправить на месте: {e.message}")
    await context.bot.send_message(
        chat_id=chat, text=render(reply.text), parse_mode="HTML",
        link_preview_options=NO_PREVIEW, reply_markup=markup(reply.keyboard))


async def show_card(context: ContextTypes.DEFAULT_TYPE, chat: str, card: Card) -> str:
    """Карточка на месте прежней или новым сообщением; id того, что ее держит.

    Правка невозможна, когда у варианта нет фото, а прежнее сообщение было
    с фото (или наоборот), или сообщение слишком старое. Тогда старое
    удаляется и приходит новое, а не молчание.
    """
    bot, photos = context.bot, context.bot_data["photos"]
    kb = markup(card.keyboard)
    text = render(card.caption)
    media = photos.file_ids.get(card.photo) or await photos.get(card.photo)
    if card.msg_id:
        if media:
            try:
                m = await bot.edit_message_media(
                    chat_id=chat, message_id=card.msg_id, reply_markup=kb,
                    media=InputMediaPhoto(media, caption=text, parse_mode="HTML"))
                photos.remember(card.photo, m)
                return card.msg_id
            except BadRequest as e:
                if "not modified" in e.message:
                    return card.msg_id
                note(f"карточку не поправить на месте: {e.message}")
        try:
            await bot.delete_message(chat_id=chat, message_id=card.msg_id)
        except TelegramError:
            pass
    if media:
        try:
            m = await bot.send_photo(chat_id=chat, photo=media, caption=text,
                                     parse_mode="HTML", reply_markup=kb)
            photos.remember(card.photo, m)
            return str(m.message_id)
        except BadRequest as e:
            # Картинку телеграм не принял: вариант все равно нужен, текстом.
            note(f"фото не принято: {e.message}")
    m = await bot.send_message(chat_id=chat, text=text, parse_mode="HTML",
                               link_preview_options=NO_PREVIEW, reply_markup=kb)
    return str(m.message_id)


async def answer(context: ContextTypes.DEFAULT_TYPE, query_id: str, text: str | None) -> None:
    # Нажатию больше 15 с телеграм ответить не дает (бот лежал, нажатие
    # ждало в очереди). Это не повод не показать карточку.
    try:
        await context.bot.answer_callback_query(query_id, text=text)
    except BadRequest as e:
        note(f"нажатие не погашено: {e.message}")


async def execute(context: ContextTypes.DEFAULT_TYPE, chat: str, actions: list[Action],
                  query_id: str | None = None) -> None:
    answered = False
    for a in actions:
        if isinstance(a, Answer):
            if query_id and not answered:
                await answer(context, query_id, a.text)
                answered = True
        elif isinstance(a, Card):
            msg_id = await show_card(context, chat, a)
            if a.on_sent:
                a.on_sent(msg_id or a.msg_id)
            note(a.label)
            if a.prefetch:
                # Фото следующей карточки в кеш, пока человек смотрит эту.
                asyncio.get_running_loop().create_task(context.bot_data["photos"].get(a.prefetch))
        else:
            await send(context, chat, a)
    # Без ответа кнопка крутится у человека на экране до таймаута, даже
    # когда обработчик упал или ничего не показал.
    if query_id and not answered:
        await answer(context, query_id, None)


def first_line(actions: list[Action]) -> str:
    for a in actions:
        text = a.caption if isinstance(a, Card) else a.text
        if text:
            return text.splitlines()[0]
    return ""


def timeout_for(text: str) -> float:
    return TIMEOUT_S.get(parse(text).cmd, HANDLER_TIMEOUT_S)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    started = time.monotonic()
    settings: config.Settings = context.bot_data["settings"]
    msg = update.message
    chat = str(msg.chat_id)
    # Перевод строки и табуляция становятся пробелом, как у прежнего bash-бота:
    # разбор аргументов по словам на этом построен.
    text = (msg.text or "").replace("\t", " ").replace("\n", " ")
    async with context.bot_data["locks"](chat):
        replies = await guarded(handle_message, settings, chat, text, msg.date.timestamp(),
                                time.time(), note, label=text, timeout=timeout_for(text))
        thought = time.monotonic() - started
        await execute(context, chat, replies)
    if replies:
        # Обработка: от получения события процессом до ухода последнего
        # ответа. Возраст: от отправки человеком, по часам телеграма с
        # точностью до секунды, в него входит доставка события.
        spent = time.monotonic() - started
        age = time.time() - msg.date.timestamp()
        note(f"ответ в чат {chat}: {first_line(replies)} "
             f"| обработка {spent:.3f} с (логика {thought:.3f}, отправка {spent - thought:.3f}), "
             f"возраст {age:.0f} с")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    started = time.monotonic()
    settings: config.Settings = context.bot_data["settings"]
    query = update.callback_query
    if query.message is None:
        # Сообщение старше 48 часов телеграм уже не отдает: править нечего.
        await query.answer()
        return
    chat = str(query.message.chat.id)
    msg_id = str(query.message.message_id)
    data = query.data or ""
    async with context.bot_data["locks"](chat):
        actions = await guarded(handle_button, settings, chat, data, msg_id, note, label=data)
        if not actions:
            return  # чужой чат: ни ответа, ни индикатора
        thought = time.monotonic() - started
        await execute(context, chat, actions, query.id)
    spent = time.monotonic() - started
    note(f"кнопка {data} в чате {chat}: {first_line(actions)} "
         f"| обработка {spent:.3f} с (логика {thought:.3f}, телеграм {spent - thought:.3f})")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Сюда попадает то, что упало вне guarded: отправка ответа, разбор
    # события. Процесс живет дальше, а след остается в журнале.
    log.error("ошибка вне обработчика", exc_info=context.error)


MENU_CHECK_S = 3600


async def keep_menu(app: Application) -> None:
    stamp = app.bot_data["settings"].state_dir / "menu"
    commands = [BotCommand(c, d) for c, d in menu.COMMANDS]
    while True:
        if menu.due(stamp):
            try:
                await app.bot.set_my_commands(commands)
                stamp.touch()
                note("меню команд обновлено")
            except TelegramError as e:
                # Меню не стоит того, чтобы ронять процесс: попробуем через час.
                log.warning(f"меню команд не обновилось: {e}")
        await asyncio.sleep(MENU_CHECK_S)


NEWS_CHECK_S = 600


async def keep_news(app: Application) -> None:
    """Сводка нового владелице, раз в день после среза.

    Ждать окончания прогона по часам нельзя: срез идет от одного до трех
    часов, а догон и вовсе когда придется. Поэтому раз в десять минут
    спрашивается, готовы ли витрины, и это дешево.
    """
    settings = app.bot_data["settings"]
    while True:
        found = await guarded(news.check, settings, timeout=60, label="сводка нового")
        if isinstance(found, tuple):
            chat, day, ctx = found
            try:
                await execute(app, chat, ctx.replies)
                news.mark_sent(settings, chat, day)
                note(f"сводка нового за {day} ушла")
            except TelegramError as e:
                # День не помечен, через десять минут попробуем снова.
                log.warning(f"сводка нового не ушла: {e}")
        await asyncio.sleep(NEWS_CHECK_S)


async def start_pulse(app: Application) -> None:
    await skip_backlog(app)
    pulse = Pulse(app.bot_data["settings"].state_dir / "pulse", on_beat=status.refresh_fresh)
    pulse.watch()
    # Задача пульса живет вне учета библиотеки: она стартует до опроса,
    # а останавливается в stop_pulse вместе с процессом.
    loop = asyncio.get_running_loop()
    app.bot_data["pulse"] = loop.create_task(pulse.run())
    app.bot_data["menu"] = loop.create_task(keep_menu(app))
    app.bot_data["news"] = loop.create_task(keep_news(app))


async def stop_pulse(app: Application) -> None:
    for name in ("pulse", "menu", "news"):
        task = app.bot_data.get(name)
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
    app.bot_data["photos"] = Photos(settings.state_dir / "photos")
    app.add_error_handler(on_error)
    app.add_handler(TypeHandler(Update, remember_offset), group=-1)
    app.add_handler(MessageHandler(filters.TEXT, on_message))
    app.add_handler(CallbackQueryHandler(on_button))
    return app


def run_once(settings: config.Settings, text: str) -> int:
    """Команда руками, ответ уходит владелице: проверить, не доставая телефон."""
    replies = handle_message(settings, settings.owner, text, time.time(), time.time(), note)

    async def go() -> None:
        app = Application.builder().token(settings.token).build()
        async with app:
            # execute ждет от контекста только бота и общие данные.
            ctx = SimpleNamespace(bot=app.bot,
                                  bot_data={"photos": Photos(settings.state_dir / "photos")})
            await execute(ctx, settings.owner, replies)

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
