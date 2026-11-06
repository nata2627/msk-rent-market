"""Сводка нового под отбор и карусель только по новому.

База подменяется: запросы разбираются по своему виду, как в
test_pybot_cards.py.
"""
import time
from datetime import date, datetime

import pytest

from bot import access, cards, config, db, filters, menu, news
from bot.actions import Card, Reply

TODAY = date(2026, 11, 6)
NOON = datetime(2026, 11, 6, 13, 0)


def offer(i):
    return (str(1000 + i), 80 - i, f"Район {i}", 2, 45, "85 000", "", "", "7", "allowed", True,
            f"https://www.cian.ru/rent/flat/{1000 + i}/", "")


@pytest.fixture
def settings(tmp_path):
    (tmp_path / ".env").write_text("NOTIFY_TG_TOKEN_DEV=dev:secret\nNOTIFY_TG_CHAT=42\n"
                                   "NOTIFY_TG_GUESTS=7\n")
    s = config.load({}, repo=tmp_path)
    s.state_dir.mkdir(parents=True)
    filters.put(s, "42", "price", "90")
    filters.put(s, "42", "rooms", "2")
    return s


@pytest.fixture
def base(monkeypatch):
    state = {"days": (TODAY, TODAY), "new": [offer(0), offer(1), offer(2), offer(3)], "seen": []}

    def query(sql, params=None):
        state["seen"].append(sql)
        if "marts.data_freshness" in sql:
            return [state["days"]]
        if "count(*) over ()" in sql and "round(o.price)" in sql:
            rows = state["new"][:news.TOP]
            return [(r[2], r[3], r[4], 85000, r[8], len(state["new"])) for r in rows]
        if "count(*) over ()" in sql:
            assert cards.NEW_COND in sql
            off = params[-1]
            return [(*r, len(state["new"])) for r in state["new"][off:off + 2]]
        raise AssertionError(sql)

    monkeypatch.setattr(db, "query", query)
    return state


def test_сводка_ждет_пересобранных_витрин(settings, base):
    assert news.ready_day(settings, NOON) == "2026-11-06"
    base["days"] = (date(2026, 11, 5), TODAY)
    assert news.ready_day(settings, NOON) is None


def test_сводка_не_идет_посреди_прогона(settings, base):
    (settings.box_repo / "data" / "daily.lock").mkdir(parents=True)
    assert news.ready_day(settings, NOON) is None


def test_сводка_не_будит_ночью(settings, base):
    assert news.ready_day(settings, datetime(2026, 11, 6, 3, 0)) is None


def test_сводка_раз_в_день(settings, base, monkeypatch):
    monkeypatch.setattr(news, "ready_day", lambda s: "2026-11-06")
    chat, day, ctx = news.check(settings)
    assert chat == "42" and day == "2026-11-06"
    text = ctx.replies[0].text
    assert text.startswith("Новое под ваш отбор за 06.11: 4")
    assert "Район 0 · 2к, 45 м2 · 85 000 руб · метро 7 мин" in text
    assert ctx.replies[0].keyboard == [[("смотреть новые", "nw")]]
    news.mark_sent(settings, chat, day)
    assert news.check(settings) is None


def test_пустой_день_помечается_и_молчит(settings, base, monkeypatch):
    monkeypatch.setattr(news, "ready_day", lambda s: "2026-11-06")
    base["new"] = []
    assert news.check(settings) is None
    assert news.sent_day(settings, "42") == "2026-11-06"


def test_кнопка_открывает_карусель_нового(settings, base):
    replies = access.handle_button(settings, "42", "nw", "555", lambda s: None)
    card = [r for r in replies if isinstance(r, Card)][0]
    assert card.caption.startswith("Новое 1 из 4")
    # Сводка остается: карусель приходит новым сообщением.
    assert card.msg_id == ""
    assert ("весь подбор", "s") in card.keyboard[1]


def test_листание_нового_остается_в_новом(settings, base):
    card = [r for r in access.handle_button(settings, "42", "nw", "555", lambda s: None)
            if isinstance(r, Card)][0]
    card.on_sent("600")
    nxt = [r for r in access.handle_button(settings, "42", "c>", "600", lambda s: None)
           if isinstance(r, Card)][0]
    assert nxt.caption.startswith("Новое 2 из 4")


def test_команда_new_и_гостю(settings, base):
    t = time.time()
    for chat in ("42", "7"):
        filters.put(settings, chat, "rooms", "2")
        replies = access.handle_message(settings, chat, "/new", t, t, lambda s: None)
        assert isinstance(replies[0], Card) and replies[0].caption.startswith("Новое 1 из 4")


def test_нового_нет(settings, base):
    base["new"] = []
    t = time.time()
    replies = access.handle_message(settings, "42", "/новое", t, t, lambda s: None)
    assert isinstance(replies[0], Reply) and replies[0].text.startswith("Нового под отбор")


def test_new_в_меню():
    assert "new" in [c for c, _ in menu.COMMANDS]
