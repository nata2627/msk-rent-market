"""Новый бот: подбор карточкой, листание, отложенные, фото.

Повторяет проверки test_bot.py для этой части и добавляет поведение,
которое у старого бота проверялось только с телефона. База подменяется
списком вариантов, телеграм подменяется записью вызовов.
"""
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

from bot import access, app, cards, commands, config, db, filters
from bot.actions import Answer, Card, Ctx, Reply
from bot.photos import Photos

ROOT = Path(__file__).resolve().parents[1]
OLD = (ROOT / "scripts" / "box_bot.sh").read_text()


def offer(i, price=70000, photo=True, active=True):
    return (str(1000 + i), 80 - i, f"Район {i}", 1, 40, f"{price:,}".replace(",", " "),
            "187 000", "оплата за 1 мес. 70 000~залог 75 000~комиссия 60% 42 000",
            "9", "allowed", active, f"https://www.cian.ru/rent/flat/{1000 + i}/",
            f"https://img/{i}.jpg" if photo else "")


@pytest.fixture
def base(monkeypatch):
    """Подбор из трех вариантов; запросы разбираются по своему виду."""
    rows = [offer(0), offer(1), offer(2, active=False)]
    seen = []

    def query(sql, params=None):
        seen.append((sql, params))
        if "count(*) over ()" in sql:
            off = params[-1]
            return [(*r, len(rows)) for r in rows[off:off + 2]]
        if "o.offer_id = %s" in sql:
            return [r for r in rows if r[0] == params[0]][:1]
        if "percentile_cont" in sql:
            return [(77,)]
        raise AssertionError(sql)

    monkeypatch.setattr(db, "query", query)
    return SimpleNamespace(rows=rows, seen=seen)


@pytest.fixture
def settings(tmp_path):
    (tmp_path / ".env").write_text("NOTIFY_TG_TOKEN_DEV=dev:secret\nNOTIFY_TG_CHAT=42\n"
                                   "NOTIFY_TG_GUESTS=7\n")
    return config.load({}, repo=tmp_path)


def ask(settings, text, chat="42"):
    t = time.time()
    return access.handle_message(settings, chat, text, t, t, lambda s: None)


def press(settings, data, chat="42", msg="500"):
    """Нажатие, а затем то, что сделал бы app: запомнить id карточки."""
    out = access.handle_button(settings, chat, data, msg, lambda s: None)
    for a in out:
        if isinstance(a, Card):
            a.on_sent(a.msg_id or "500")
    return out


def card_of(actions):
    return next(a for a in actions if isinstance(a, Card))


def answer_of(actions):
    return next(a.text for a in actions if isinstance(a, Answer))


def open_flats(settings, chat="42"):
    out = ask(settings, "/flats", chat)
    card_of(out).on_sent("500")
    return out


# ---------------------------------------------------------------------
# Фильтры: хранение и условие
# ---------------------------------------------------------------------

def test_файл_фильтров_в_формате_старого_бота(settings):
    filters.put(settings, "42", "price", "90")
    filters.put(settings, "42", "rooms", "2")
    filters.put(settings, "42", "price", "80")
    assert (settings.state_dir / "filters.42").read_text() == "rooms=2\nprice=80\n"
    filters.put(settings, "42", "rooms", "")
    assert filters.load(settings, "42") == {"price": "80"}


def test_мусор_в_файле_фильтров_не_доходит_до_запроса(settings):
    settings.state_dir.mkdir(parents=True)
    (settings.state_dir / "filters.42").write_text(
        "price=90; drop table x\nrooms=1,2\npets=allowed' or 1=1\nчто-то\nmetro=15\n")
    assert filters.load(settings, "42") == {"rooms": "1,2", "metro": "15"}


def test_условие_только_параметрами():
    cond, params = filters.where({"price": "90", "rooms": "1,2", "district": "Марьино'; --",
                                  "pets": "allowed", "park": "1", "mkad": "1"})
    assert "Марьино" not in cond
    assert params == [90000, [1, 2], "allowed", "Марьино'; --", 0]
    assert cond.endswith("crawls_missed <= %s and is_active")


def test_свежесть_в_подборе_не_отключаема():
    assert filters.where({})[1] == [0]
    assert "crawls_missed <= ${v:-0}" in OLD


def test_подпись_отбора_как_у_старого_бота():
    f = {"price": "90", "rooms": "1,2", "metro": "15", "pets": "negotiable", "mkad": "1"}
    assert filters.human(f) == ("цена до 90 тыс, комнаты 1,2, метро до 15 мин, "
                                "животные по договоренности, внутри МКАД")
    for piece in ('out="$out, цена до $v тыс"', 'out="$out, внутри МКАД"',
                  'out="$out, животные $(pets_human "$v")"'):
        assert piece in OLD


def test_подбор_не_заводит_своего_определения():
    src = "".join(p.read_text() for p in (ROOT / "bot").glob("*.py"))
    assert "marts.offer_pick" in src
    for gate in ("is_duplicate", "is_room_share", "is_daily", "lease_term_type"):
        assert gate not in src


def test_начальный_отбор_ставится_один_раз(settings, base):
    out = ask(settings, "/flats")
    assert out[0].text.startswith("Фильтров у вас ещё не было, поставил начальные")
    assert "77 тыс." in out[0].text
    assert filters.load(settings, "42") == {"rooms": "1,2", "price": "77"}
    assert not isinstance(ask(settings, "/flats")[0], Reply)


def test_начальный_отбор_без_базы_сто_тысяч(settings, monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: None)
    assert filters.defaults(settings, "42") == 100


# ---------------------------------------------------------------------
# Карточка
# ---------------------------------------------------------------------

def test_карточка_как_у_старого_бота(settings, base):
    c = card_of(open_flats(settings))
    assert c.caption == """Вариант 1 из 3
Район 0 · 80 баллов
1 комн., 40 м2
70 000 руб/мес
метро 9 мин пешком, животные можно

При заселении 187 000 руб
оплата за 1 мес. 70 000
залог 75 000
комиссия 60% 42 000

Отбор: цена до 77 тыс, комнаты 1,2

https://www.cian.ru/rent/flat/1000/"""
    assert c.photo == "https://img/0.jpg"
    assert c.prefetch == "https://img/1.jpg"
    assert c.msg_id == ""
    for piece in ('head="Вариант $(( idx + 1 )) из ${total:-?}"', "$price руб/мес",
                  "метро $metro мин пешком, животные $(pets_human \"$pets\")",
                  "При заселении %s руб"):
        assert piece in OLD


def test_столбцы_карточки_дословно(settings):
    old = OLD.split('CARD_COLS="')[1].split('"\n')[0]
    assert cards.CARD_COLS.replace("%%", "%") == old


def test_снятое_и_отложенное_помечены(settings, base):
    cards.toggle_saved(settings, "42", "1002")
    ctx = Ctx(settings, "42", "owner")
    assert cards.show(ctx, "pick", 2)
    text = ctx.replies[0].caption
    assert "\n★ отложено\n\nОтбор:" in text
    assert "\n\nСнято с публикации, по ссылке может не открыться.\n\n" in text
    assert ctx.replies[0].prefetch == ""


def test_кнопки_карточки(settings, base):
    c = card_of(open_flats(settings))
    assert c.keyboard == [
        [("◀", "c<"), ("★ отложить", "cs"), ("▶", "c>")],
        [("цена -10", "cp-"), ("до 77 тыс", "noop"), ("+10", "cp+")],
        [("1к", "cr1"), ("2к", "cr2"), ("● 1-2к", "cr12")],
        [("еще фильтры", "filters"), ("отложенные", "sv")],
    ]
    assert cards.keyboard("saved", True, "", "") == [
        [("◀", "v<"), ("★ убрать", "vs"), ("▶", "v>")], [("к подбору", "s")]]
    for row in c.keyboard:
        for text, data in row:
            assert len(data.encode()) <= 64 and text.strip()


def test_подходящего_нет(settings, base):
    base.rows.clear()
    out = ask(settings, "/подбор")
    assert out[-1].text == """Подходящего нет

Отбор: цена до 77 тыс, комнаты 1,2

Ослабить: /filter"""


# ---------------------------------------------------------------------
# Листание
# ---------------------------------------------------------------------

def test_листание_вперед_назад_и_края(settings, base):
    open_flats(settings)
    assert answer_of(press(settings, "c<")) == "это первый"
    out = press(settings, "c>")
    assert out[0] == Answer(None)
    assert card_of(out).caption.startswith("Вариант 2 из 3")
    assert card_of(out).msg_id == "500"
    press(settings, "c>")
    out = press(settings, "c>")
    assert out == [Answer("дальше ничего нет")]
    assert cards.state(settings, "42").idx == 2
    assert card_of(press(settings, "c<")).caption.startswith("Вариант 2 из 3")


def test_отложить_и_вернуть_на_карточке(settings, base):
    open_flats(settings)
    press(settings, "c>")
    out = press(settings, "cs")
    assert answer_of(out) == "отложено, список /saved"
    assert "★ убрать" in str(card_of(out).keyboard)
    assert card_of(out).caption.startswith("Вариант 2 из 3")
    assert answer_of(press(settings, "cs")) == "убрано из отложенных"
    assert cards.saved(settings, "42") == []


def test_отложенные_листаются_и_убираются(settings, base):
    for i in ("1000", "1001", "1002"):
        cards.toggle_saved(settings, "42", i)
    assert cards.saved(settings, "42") == ["1002", "1001", "1000"]
    card_of(ask(settings, "/saved")).on_sent("500")
    out = press(settings, "v>")
    assert card_of(out).caption.startswith("Отложенное 2 из 3")
    press(settings, "v>")
    # Убран последний: шаг назад.
    out = press(settings, "vs")
    assert answer_of(out) == "убрано из отложенных"
    assert card_of(out).caption.startswith("Отложенное 2 из 2")
    # Убран средний: на его место встает следующий.
    press(settings, "v<")
    out = press(settings, "vs")
    assert card_of(out).caption.startswith("Отложенное 1 из 1")


def test_отложенных_нет(settings, base):
    assert ask(settings, "/отложенные")[0].text == """Отложенных пока нет

Откройте подбор (/flats) и нажмите «★ отложить» под понравившимся."""
    open_flats(settings)
    out = press(settings, "sv")
    assert out == [Answer("отложенные"),
                   Reply("Отложенных пока нет. Нажмите «★ отложить» под понравившимся.")]


def test_отложенное_принимает_только_цифры(settings):
    assert cards.toggle_saved(settings, "42", "1; rm -rf /") is None
    assert cards.toggle_saved(settings, "42", "١٢٣") is None
    assert not (settings.state_dir / "saved.42").exists()


def test_из_отложенных_к_подбору_на_месте(settings, base):
    cards.toggle_saved(settings, "42", "1001")
    card_of(ask(settings, "/saved")).on_sent("500")
    out = press(settings, "s")
    assert answer_of(out) == "ищу"
    assert card_of(out).msg_id == "500"
    assert card_of(out).caption.startswith("Вариант 1 из 3")


@pytest.mark.parametrize("data", ["first", "next"])
def test_старые_кнопки_открывают_карусель(settings, base, data):
    out = press(settings, data)
    assert answer_of(out) == "ищу"
    assert card_of(out).msg_id == ""


def test_цена_кнопками_в_границах(settings, base):
    open_flats(settings)
    for _ in range(10):
        out = press(settings, "cp-")
    assert answer_of(out) == "цена до 10 тыс"
    filters.put(settings, "42", "price", "995")
    assert answer_of(press(settings, "cp+")) == "цена до 999 тыс"
    assert card_of(press(settings, "cr2")).keyboard[2][1] == ("● 2к", "cr2")
    assert filters.load(settings, "42")["rooms"] == "2"


@pytest.mark.parametrize("data", ["rm -rf /", "p-10; id", "x;reset", "' or 1=1", "эксплойт", ""])
def test_неизвестная_кнопка_ничего_не_меняет(settings, data):
    filters.put(settings, "42", "price", "90")
    notes = []
    out = access.handle_button(settings, "42", data, "500", notes.append)
    assert out == [Answer(commands.UNKNOWN_BUTTON)]
    assert filters.load(settings, "42") == {"price": "90"}
    assert notes[-1] == f"неизвестная кнопка: {data}"


def test_надпись_на_кнопке_просто_надпись(settings):
    assert press(settings, "noop") == [Answer("это просто надпись")]


def test_чужой_чат_кнопкой_ничего_не_получает(settings, base):
    notes = []
    assert access.handle_button(settings, "999", "c>", "500", notes.append) == []
    assert "чужой чат 999" in notes[0]
    assert not list(settings.state_dir.glob("*999*"))


def test_у_гостя_свои_фильтры_карточка_и_отложенные(settings, base):
    open_flats(settings, chat="7")
    press(settings, "cr2", chat="7")
    press(settings, "cs", chat="7")
    assert filters.load(settings, "7")["rooms"] == "2"
    assert cards.saved(settings, "7")
    assert not (settings.state_dir / "filters.42").exists()
    assert not (settings.state_dir / "saved.42").exists()


def test_состояние_карусели_в_формате_старого_бота(settings, base):
    open_flats(settings)
    assert (settings.state_dir / "card.42").read_text() == "pick|0|500|1000\n"
    assert 'card_set() { echo "$1|$2|$3|$4" >"$(card_file)"; }' in OLD


def test_перенесенные_команды_не_числятся_в_переносе():
    for cmd in ("/flats", "/подбор", "/saved", "/отложенные"):
        assert cmd in commands.HANDLERS and cmd not in commands.PENDING


# ---------------------------------------------------------------------
# Телеграм: правка на месте, запасные пути, ответ на нажатие
# ---------------------------------------------------------------------

class FakeBot:
    def __init__(self, edit_error=None):
        self.calls = []
        self.edit_error = edit_error
        self.next_id = 900

    def _msg(self, photo=False):
        self.next_id += 1
        return SimpleNamespace(message_id=self.next_id,
                               photo=[SimpleNamespace(file_id=f"F{self.next_id}")] if photo else None)

    async def edit_message_media(self, **kw):
        self.calls.append(("edit", kw))
        if self.edit_error:
            raise BadRequest(self.edit_error)
        return self._msg(photo=True)

    async def delete_message(self, **kw):
        self.calls.append(("delete", kw))

    async def send_photo(self, **kw):
        self.calls.append(("photo", kw))
        return self._msg(photo=True)

    async def send_message(self, **kw):
        self.calls.append(("message", kw))
        return self._msg()

    async def answer_callback_query(self, qid, text=None):
        self.calls.append(("answer", text))


class FakePhotos(Photos):
    def __init__(self, blobs):
        super().__init__(Path("/nonexistent"), client=object())
        self.blobs = blobs
        self.asked = []

    async def get(self, url):
        self.asked.append(url)
        return self.blobs.get(url, b"")


def run(bot, photos, actions, query_id=None):
    ctx = SimpleNamespace(bot=bot, bot_data={"photos": photos})
    asyncio.run(app.execute(ctx, "42", actions, query_id))


def a_card(msg_id="", photo="https://img/0.jpg", sent=None):
    return Card("Вариант 1 из 3\nтекст", [[("▶", "c>")]], photo=photo, msg_id=msg_id,
                on_sent=(sent.append if sent is not None else None))


def test_новая_карточка_с_фото_и_file_id_потом(settings):
    bot, photos, sent = FakeBot(), FakePhotos({"https://img/0.jpg": b"jpg"}), []
    run(bot, photos, [a_card(sent=sent)])
    kind, kw = bot.calls[0]
    assert kind == "photo" and kw["photo"] == b"jpg"
    assert kw["caption"].startswith("<b>Вариант 1 из 3</b>")
    assert sent == ["901"]
    # Второй показ того же фото без загрузки: по file_id.
    run(bot, photos, [a_card(msg_id="901")])
    assert bot.calls[1][0] == "edit"
    assert bot.calls[1][1]["media"].media == "F901"
    assert photos.asked == ["https://img/0.jpg"]


def test_не_изменилось_это_не_ошибка():
    bot, sent = FakeBot(edit_error="Message is not modified: specified new message content"), []
    run(bot, FakePhotos({"https://img/0.jpg": b"jpg"}), [a_card(msg_id="77", sent=sent)])
    assert [c[0] for c in bot.calls] == ["edit"]
    assert sent == ["77"]


def test_правка_не_удалась_старое_удаляется_новое_приходит():
    bot, sent = FakeBot(edit_error="Message to edit not found"), []
    run(bot, FakePhotos({"https://img/0.jpg": b"jpg"}), [a_card(msg_id="77", sent=sent)])
    assert [c[0] for c in bot.calls] == ["edit", "delete", "photo"]
    assert sent == ["901"]


def test_без_фото_карточка_текстом():
    bot, sent = FakeBot(), []
    run(bot, FakePhotos({}), [a_card(msg_id="77", photo="", sent=sent)])
    assert [c[0] for c in bot.calls] == ["delete", "message"]
    assert bot.calls[1][1]["reply_markup"].inline_keyboard[0][0].callback_data == "c>"


def test_нажатие_гасится_первым_и_всегда():
    bot = FakeBot()
    run(bot, FakePhotos({}), [Answer("ищу"), a_card(photo="")], query_id="q")
    assert bot.calls[0] == ("answer", "ищу")
    bot = FakeBot()
    run(bot, FakePhotos({}), [Reply("Не получилось")], query_id="q")
    assert bot.calls[-1] == ("answer", None)


def test_фото_качается_один_раз_и_кешируется(tmp_path):
    hits = []

    class Resp:
        status_code = 200

        async def aiter_bytes(self):
            await asyncio.sleep(0.01)
            yield b"jpg"

    class Client:
        def stream(self, method, url):
            hits.append(url)

            class Ctx:
                async def __aenter__(self):
                    return Resp()

                async def __aexit__(self, *a):
                    return False
            return Ctx()

    async def go():
        p = Photos(tmp_path / "photos", client=Client())
        a, b = await asyncio.gather(p.get("https://img/1.jpg"), p.get("https://img/1.jpg"))
        c = await p.get("https://img/1.jpg")
        return a, b, c

    assert asyncio.run(go()) == (b"jpg", b"jpg", b"jpg")
    assert hits == ["https://img/1.jpg"]
    assert len(list((tmp_path / "photos").glob("*.jpg"))) == 1


def test_приложение_собирается_с_обработчиками(settings):
    """02.11 из app.py при правке выпал on_message: тесты логики были
    зелеными, а процесс падал на старте, и бот молчал."""
    from telegram.ext import CallbackQueryHandler, MessageHandler
    s = config.load({"NOTIFY_TG_TOKEN_DEV": "123:abc", "NOTIFY_TG_CHAT": "42"},
                    repo=settings.repo)
    built = app.build(s)
    kinds = {type(h) for group in built.handlers.values() for h in group}
    assert {MessageHandler, CallbackQueryHandler} <= kinds


def test_старое_нажатие_не_мешает_показать_карточку():
    class Late(FakeBot):
        async def answer_callback_query(self, qid, text=None):
            raise BadRequest("Query is too old and response timeout expired or query id is invalid")
    bot, sent = Late(), []
    run(bot, FakePhotos({}), [Answer(None), a_card(msg_id="77", photo="", sent=sent)], query_id="q")
    assert [c[0] for c in bot.calls] == ["delete", "message"]
    assert sent == ["901"]
