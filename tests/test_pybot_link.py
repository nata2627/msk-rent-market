"""Ссылка на объявление в чате: карточка из базы или со страницы площадки.

База подменяется разбором запросов по их виду, скрипт открытия страницы
подменяется готовым ответом. Площадка, браузер и телеграм не нужны.
"""
import asyncio
import json
import time
from datetime import date
from types import SimpleNamespace

import pytest

from bot import access, app, cards, config, db, link
from bot.actions import Answer, Card, Fetch, Reply

URL = "https://www.cian.ru/rent/flat/{}/?context=abc"


def row(oid, score=81, active=True):
    return (oid, score, "Хамовники", 1, 35, "90 000", "252 000",
            "оплата за 1 мес. 90 000~залог 90 000~комиссия 80% 72 000", "8", "allowed",
            active, f"https://www.cian.ru/rent/flat/{oid}/", f"https://img/{oid}.jpg",
            "Фрунзенская")


@pytest.fixture
def base(monkeypatch):
    """В витрине 311111111 (в подборе, с оценкой) и 322222222 (не в подборе, без оценки)."""
    state = SimpleNamespace(down=False, seen=[])

    def query(sql, params=None):
        state.seen.append((sql, params))
        if state.down:
            return None
        oid = params[0] if params else None
        if "from marts.offer_scored" in sql:
            return {"311111111": [(*row("311111111"), True)],
                    "322222222": [(*row("322222222", 40), False)]}.get(oid, [])
        if "from ml.price_estimate" in sql:
            return [(90000, 74000, 61000, 89000)] if oid == "311111111" else []
        if "from marts.offer_pick" in sql and "o.offer_id = %s" in sql:
            return [row(oid)] if oid in ("311111111", "322222222") else []
        raise AssertionError(sql)

    monkeypatch.setattr(db, "query", query)
    link.PENDING.clear()
    yield state
    link.PENDING.clear()


@pytest.fixture
def settings(tmp_path):
    (tmp_path / ".env").write_text("NOTIFY_TG_TOKEN_DEV=dev:secret\nNOTIFY_TG_CHAT=42\n"
                                   "NOTIFY_TG_GUESTS=7\n")
    return config.load({}, repo=tmp_path)


def ask(settings, text, chat="42"):
    t = time.time()
    return access.handle_message(settings, chat, text, t, t, lambda s: None)


def card_of(actions):
    return next(a for a in actions if isinstance(a, Card))


def test_ссылка_из_базы_это_карточка_с_баллом_и_ценой(settings, base):
    out = ask(settings, "смотри " + URL.format(311111111))
    c = card_of(out)
    assert c.caption.startswith("По ссылке\nХамовники · 81 баллов")
    assert "Модель цены 74 тыс (80%: 61-89), дороже похожих на 22%" in c.caption
    assert "В подбор не входит" not in c.caption
    assert c.keyboard == [[("★ отложить", "ls311111111"), ("весь подбор", "s")]]
    # Ссылка не ушла в разбор фразы: там тратится лимит GigaChat.
    assert not any(isinstance(a, Reply) and "Не понял" in a.text for a in out)


def test_вне_подбора_сказано_без_своего_определения(settings, base):
    c = card_of(ask(settings, URL.format(322222222)))
    assert "В подбор не входит" in c.caption
    assert "Модель цены" not in c.caption


def test_гость_видит_карточку_без_баллов(settings, base):
    c = card_of(ask(settings, URL.format(311111111), chat="7"))
    assert "баллов" not in c.caption


def test_нет_в_базе_значит_открыть_страницу(settings, base):
    out = ask(settings, URL.format(333333333))
    assert out[0].text.startswith("Открываю на ЦИАНе")
    assert out[1] == Fetch("333333333", "owner")
    assert link.used(settings, date.today().isoformat()) == 1
    # Нетерпеливый повтор не открывает страницу второй раз.
    again = ask(settings, URL.format(333333333))
    assert "Уже открываю" in again[0].text
    assert not any(isinstance(a, Fetch) for a in again)


def test_предел_в_сутки_и_стоп_после_блокировки(settings, base):
    day = date.today().isoformat()
    for _ in range(link.LIMIT):
        link.take(settings, day)
    out = ask(settings, URL.format(333333333))
    assert "не больше 10 объявлений в сутки" in out[0].text
    assert not any(isinstance(a, Fetch) for a in out)

    link._count_path(settings, day).unlink()
    link.mark_blocked(settings, day)
    out = ask(settings, URL.format(333333333))
    assert out[0].text.startswith("По ссылкам до завтра не хожу")


def test_база_молчит(settings, base):
    base.down = True
    assert "База не отвечает" in ask(settings, URL.format(311111111))[0].text


RESULT = {"status": "ok", "row": list(row("333333333", 77)), "repair": "дизайнерский"}


def test_открытая_страница_становится_карточкой_и_живет_у_бота(settings, base):
    link.PENDING.add("333333333")
    out = link.after_fetch(settings, "42", "owner", "333333333", RESULT)
    c = card_of(out)
    assert c.caption.startswith("По ссылке, со страницы ЦИАНа\nХамовники · 77 баллов")
    assert "Ремонт по странице: дизайнерский" in c.caption
    assert "в статистику панели не входит" in c.caption
    assert "333333333" not in link.PENDING
    # Второй раз за день страница не открывается, карточка из сохраненного.
    again = ask(settings, URL.format(333333333))
    assert not any(isinstance(a, Fetch) for a in again)
    assert "77 баллов" in card_of(again).caption


def test_отложенное_по_ссылке_видно_в_saved(settings, base):
    link.after_fetch(settings, "42", "owner", "333333333", RESULT)
    out = access.handle_button(settings, "42", "ls333333333", "500", lambda s: None)
    assert next(a.text for a in out if isinstance(a, Answer)) == "отложено, список /saved"
    assert "★ убрать" in card_of(out).keyboard[0][0][0]
    saved = card_of(ask(settings, "/saved"))
    assert saved.caption.startswith("Отложенное 1 из 1")
    assert "77 баллов" in saved.caption and "со страницы" not in saved.caption.split("\n")[0]


@pytest.mark.parametrize("status, words", [
    ("blocked", "До завтра по ссылкам больше не хожу"),
    ("not_found", "снято с публикации или в ссылке ошибка"),
    ("not_rent", "Не аренда квартиры"),
    ("error", "Попробуйте позже"),
])
def test_неудачи_открытия(settings, base, status, words):
    out = link.after_fetch(settings, "42", "owner", "333333333", {"status": status})
    assert words in out[0].text
    assert link.blocked(settings, date.today().isoformat()) == (status == "blocked")


class FakeBot:
    def __init__(self):
        self.calls = []

    async def send_message(self, **kw):
        self.calls.append(("message", kw))
        return SimpleNamespace(message_id=901, photo=None)

    async def send_photo(self, **kw):
        self.calls.append(("photo", kw))
        return SimpleNamespace(message_id=902, photo=[SimpleNamespace(file_id="F")])


class NoPhotos:
    file_ids = {}

    async def get(self, url):
        return b""

    def remember(self, url, m):
        pass


def test_app_открывает_в_фоне_и_присылает_карточку(settings, base, monkeypatch):
    calls = []

    async def script(s, oid):
        calls.append(oid)
        return RESULT

    monkeypatch.setattr(app, "run_link_script", script)
    bot = FakeBot()
    ctx = SimpleNamespace(bot=bot, bot_data={"photos": NoPhotos(), "settings": settings})

    async def go():
        await app.execute(ctx, "42", [Reply("Открываю"), Fetch("333333333", "owner")])
        await asyncio.gather(*ctx.bot_data["tasks"])

    asyncio.run(go())
    assert calls == ["333333333"]
    texts = [kw["text"] for kind, kw in bot.calls if kind == "message"]
    assert "Открываю" in texts[0]
    assert "77 баллов" in texts[1]


def test_скрипт_ответил_не_json(settings, tmp_path, monkeypatch):
    script = settings.repo / "scripts" / "box_link.sh"
    script.parent.mkdir(parents=True)
    script.write_text("echo шум в журнал\necho '{\"status\": \"not_found\"}'\n")
    assert asyncio.run(app.run_link_script(settings, "1"))["status"] == "not_found"
    script.write_text("echo сломалось\n")
    assert asyncio.run(app.run_link_script(settings, "1"))["status"] == "error"


def test_кнопка_не_из_ссылки_не_перехватывается(settings, base):
    ctx = SimpleNamespace(settings=settings, chat="42", replies=[])
    assert link.button(ctx, "c>", "500") is False
    assert json.dumps(link.offer_id("cian.ru/rent/flat/12345/")) == '"12345"'
    assert link.offer_id("cian.ru/sale/flat/12345/") is None
