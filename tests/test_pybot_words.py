"""Новый бот: подбор фразой, /ask и /price функциями, а не подпроцессом.

GigaChat и база подменяются, в сеть тесты не ходят. Тексты сверяются со
старым ботом, запросы /ask с теми, что box_ask.py шлет через psql.
"""
import datetime
import re
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bot import access, db, filters, words
from bot.actions import Card, Reply
from tests.test_pybot_cards import OLD, base, settings  # noqa: F401

box_nl, box_ask, box_price = words.box_nl, words.box_ask, words.box_price
DISTRICTS = ["Хамовники", "Марьино", "Арбат"]


@pytest.fixture
def world(base, monkeypatch):  # noqa: F811
    """Справочник районов, ответы модели и SQL /ask, тревоги без отправки."""
    plain = db.query
    asked, alarms = [], []

    def query(sql, params=None):
        if sql == filters.DISTRICTS_SQL:
            return [(d,) for d in DISTRICTS]
        if sql in words.ASK_SQL.values():
            asked.append((sql, params))
            return [(datetime.date(2026, 11, 2), 120, 85000, 70000, 99000, Decimal("1650.5"))]
        return plain(sql, params)

    def boom(*a, **k):
        raise AssertionError("в сеть нельзя")

    monkeypatch.setattr(db, "query", query)
    monkeypatch.setattr(db, "scalar", lambda sql, p=None: 77)
    monkeypatch.setattr(box_nl.urllib.request, "urlopen", boom)
    monkeypatch.setattr(box_nl, "primary", lambda: "gigachat")
    monkeypatch.setattr(box_nl, "alarm", lambda err, env=None: alarms.append((err, env)))
    monkeypatch.setattr(box_ask, "bot_model", lambda: "GigaChat-2")
    return SimpleNamespace(asked=asked, alarms=alarms)


def ask(settings, text, chat="42"):  # noqa: F811
    t = time.time()
    return access.handle_message(settings, chat, text, t, t, lambda s: None)


def text_of(actions):
    return "\n".join(a.text for a in actions if isinstance(a, Reply))


def model(monkeypatch, raw):
    monkeypatch.setattr(box_nl, "gigachat_raw", lambda *a, **k: raw)


# ---------------------------------------------------------------------
# Подбор фразой
# ---------------------------------------------------------------------

def test_фраза_пишет_фильтры_и_показывает_карточку(settings, world, monkeypatch):
    filters.put(settings, "42", "price", "80")
    model(monkeypatch, {"rooms": [2], "price_max": 90, "unsupported": ["балкон", "вид, на реку"]})
    out = ask(settings, "двушка до 90 с балконом")
    assert text_of(out).startswith("""Ищу так: цена до 90 тыс, комнаты 2
Для этого фильтра нет: балкон, вид,  на реку

Поправить кнопками: /filter
Вернуть прежние фильтры: /undo""")
    assert any(isinstance(a, Card) for a in out)
    assert filters.prev_path(settings, "42").read_text() == "price=80\n"
    # А /undo возвращает то, что было до фразы.
    assert text_of(ask(settings, "/undo")) == "Вернул прежние фильтры: цена до 80 тыс"


def test_непонятая_фраза_присылает_экран(settings, world, monkeypatch):
    model(monkeypatch, {})
    out = ask(settings, "привет")
    assert out[0].text == "Не понял фразу\n\nМожно так: двушка до 90 у метро с котом. Или кнопками ниже."
    assert out[1].keyboard and out[1].text.startswith("Фильтры подбора")


def test_лимит_фраз_в_папке_своего_бота(settings, world, monkeypatch):
    """Квота тестового бота не трогает data/bot боевого."""
    model(monkeypatch, {"rooms": [1]})
    for _ in range(20):
        ask(settings, "однушка", chat="7")
    out = ask(settings, "однушка", chat="7")
    assert text_of(out) == "Фраз на сегодня больше не разбираю, лимит 20 в сутки.\nКнопками можно сколько угодно: /filter"
    assert (settings.state_dir / "nl.7").exists()
    assert box_nl.STATE == settings.state_dir


def test_тексты_фразы_как_у_старого():
    body = OLD.split("cmd_phrase() {")[1].split("\n}")[0]
    src = open(words.__file__).read()
    for line in ("Поправить кнопками: /filter", "Вернуть прежние фильтры: /undo",
                 "Подходящего нет. Ослабить: /filter", "Кнопками можно сколько угодно: /filter",
                 "Можно так: двушка до 90 у метро с котом. Или кнопками ниже."):
        assert line in body and line in src


def test_гостю_фраза_доступна(settings, world, monkeypatch):
    model(monkeypatch, {"rooms": [1]})
    assert text_of(ask(settings, "однушка", chat="7")).startswith("Ищу так: комнаты 1")


def test_тревога_о_ключе_от_тестового_бота(settings, world, monkeypatch):
    def rejected(*a, **k):
        raise box_nl.KeyRejected("OAuth ответил 401")
    monkeypatch.setattr(box_nl, "gigachat_raw", rejected)
    sent = []
    monkeypatch.setattr(box_nl, "alarm", lambda err, env=None: sent.append(env["NOTIFY_TG_TOKEN"]))
    ask(settings, "двушка")
    assert sent == [settings.token]


# ---------------------------------------------------------------------
# /ask
# ---------------------------------------------------------------------

@pytest.mark.parametrize("fn", list(box_ask.SQL))
def test_переменные_psql_стали_параметрами(fn):
    sql = words.ASK_SQL[fn]
    assert not re.search(r"(?<!:):'?\w", sql.replace("::", "")), sql
    assert "::int[]" in sql or fn == "data_status"


def test_вопрос_отвечает_по_базе(settings, world, monkeypatch):
    monkeypatch.setattr(box_ask, "model_raw", lambda *a, **k: {
        "function": "market_price", "arguments": {"rooms": [2], "district": "Хамовники"}})
    out = text_of(ask(settings, "/ask сколько стоит двушка в Хамовниках"))
    assert out.startswith("Цена аренды на 02.11")
    assert "Медиана 85 000" in out
    sql, params = world.asked[0]
    assert params["district"] == "Хамовники" and params["rooms"] == "{2}"


def test_вопрос_без_текста(settings, world):
    assert text_of(ask(settings, "/вопрос")).startswith("Задайте вопрос после команды.")


def test_вопрос_база_не_ответила(settings, world, monkeypatch):
    monkeypatch.setattr(box_ask, "model_raw", lambda *a, **k: {"function": "data_status", "arguments": {}})
    monkeypatch.setattr(db, "query", lambda sql, p=None: [] if sql == filters.DISTRICTS_SQL else None)
    assert text_of(ask(settings, "/ask свежие ли данные")).startswith("База не ответила")


def test_вопрос_гостя_в_общей_квоте(settings, world, monkeypatch):
    """Вопросы и фразы делят один лимит, как у старого бота."""
    monkeypatch.setattr(box_ask, "model_raw", lambda *a, **k: {"function": None, "arguments": {}})
    for _ in range(20):
        ask(settings, "/ask что-нибудь", chat="7")
    assert "лимит 20 в сутки" in text_of(ask(settings, "/ask что-нибудь", chat="7"))


# ---------------------------------------------------------------------
# /price
# ---------------------------------------------------------------------

def test_цена_пустая_подсказка(settings, world):
    assert text_of(ask(settings, "/price")) == box_price.HELP


def test_цена_словами(settings, world, monkeypatch):
    def query(sql, params=None):
        if "distinct district from ml.price_grid" in sql:
            return [(d,) for d in DISTRICTS]
        if "from ml.price_grid" in sql:
            return [(98000.0, 85000.0, 112000.0, 31, 45)]
        raise AssertionError(sql)
    monkeypatch.setattr(db, "query", query)
    out = text_of(ask(settings, "/цена 2к 45 м2 Хамовники"))
    assert out.startswith("Типичная 2к 45 м2, Хамовники\n98 тыс в месяц (80%: 85-112)")


@pytest.mark.parametrize("arg", ["80 тыс", "80", "80тыс", "80 000", "80000 руб", "80 т."])
def test_цена_одним_числом_ставит_потолок(settings, world, arg):
    out = text_of(ask(settings, f"/price {arg}"))
    assert out.startswith("Принято: price = 80\n")
    assert filters.load(settings, "42")["price"] == "80"


def test_цена_одним_числом_гостю_в_его_фильтр(settings, world):
    ask(settings, "/price 77 тыс", chat="7")
    assert filters.load(settings, "7")["price"] == "77"
    assert "price" not in filters.load(settings, "42")


def test_цена_одним_числом_вне_границ(settings, world):
    assert text_of(ask(settings, "/price 5")).startswith("Не похоже на правду")
    assert "price" not in filters.load(settings, "42")


@pytest.mark.parametrize("value,want", [
    (None, ""), (True, "t"), (False, "f"), (45.0, "45"), (45.5, "45.5"),
    (Decimal("12.30"), "12.30"), (datetime.date(2026, 11, 2), "2026-11-02"), (7, "7"),
])
def test_значения_как_печатает_psql(value, want):
    assert words.psql_text(value) == want
