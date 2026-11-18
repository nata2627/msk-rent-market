"""Перерыв обхода: после BREAK_AFTER страниц браузер закрывается и ждет."""
from etl import browser_client as bc


def test_break_after_n_pages(monkeypatch):
    slept, closed = [], []
    monkeypatch.setattr(bc.config, "BREAK_AFTER", 500)
    monkeypatch.setattr(bc.config, "BREAK_MINUTES", 25)
    monkeypatch.setattr(bc.time, "sleep", slept.append)
    c = bc.BrowserClient()
    monkeypatch.setattr(c, "close", lambda: closed.append(1))
    c.requests_made = 499
    c._maybe_break()
    assert not slept
    c.requests_made = 500
    c._maybe_break()
    assert slept == [1500] and closed == [1]
    # Следующий перерыв отсчитывается от этого, а не от начала обхода.
    c.requests_made = 999
    c._maybe_break()
    assert len(slept) == 1
    c.requests_made = 1000
    c._maybe_break()
    assert len(slept) == 2


def test_break_off(monkeypatch):
    monkeypatch.setattr(bc.config, "BREAK_AFTER", 0)
    monkeypatch.setattr(bc.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError))
    c = bc.BrowserClient()
    c.requests_made = 5000
    c._maybe_break()
