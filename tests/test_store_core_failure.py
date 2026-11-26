"""Сбой записи в core не роняет обход.

26.11.2026 испорченная страница core.offer_photo уронила ежедневный срез
на 76% сегмента, хотя raw она не задевала, а core пересобирается из raw.
Проверяется, что raw пишется, транзакция откатывается, сбой считается, а
следующая страница пишется как обычно.
"""
from datetime import date

import psycopg
import pytest

from etl import collect

OFFER = {"id": 1, "bargainTerms": {"price": 50000}}


class FakeConn:
    def __init__(self):
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def calls(monkeypatch):
    log = []
    monkeypatch.setattr(collect.db, "save_snapshots", lambda *a, **k: log.append("raw"))
    monkeypatch.setattr(collect, "parse_offer", lambda o, d: {"offer": o})
    for name in ("upsert_offers", "upsert_observations", "upsert_metro"):
        monkeypatch.setattr(collect.db, name, lambda *a, n=name, **k: log.append(n))
    collect._core_failed.update(pages=0, first=None)
    return log


def test_core_error_keeps_raw_and_counts(monkeypatch, calls):
    def broken(*a, **k):
        raise psycopg.errors.InternalError_(
            "MultiXactId 1835609973 has not been created yet -- apparent wraparound")
    monkeypatch.setattr(collect.db, "upsert_photos", broken)
    conn = FakeConn()

    n = collect._store(conn, 1, 1, date(2026, 11, 26), [OFFER], page=1, dry_run=False)

    assert n == 1
    assert calls[0] == "raw"
    assert conn.rollbacks == 1
    assert collect._core_failed["pages"] == 1
    assert "MultiXactId" in collect._core_failed["first"]


def test_first_error_is_kept(monkeypatch, calls):
    errors = iter(["первая", "вторая"])

    def broken(*a, **k):
        raise psycopg.errors.InternalError_(next(errors))
    monkeypatch.setattr(collect.db, "upsert_photos", broken)
    conn = FakeConn()

    collect._store(conn, 1, 1, date(2026, 11, 26), [OFFER], page=1, dry_run=False)
    collect._store(conn, 1, 1, date(2026, 11, 26), [OFFER], page=2, dry_run=False)

    assert collect._core_failed["pages"] == 2
    assert "первая" in collect._core_failed["first"]


def test_non_database_error_still_stops(monkeypatch, calls):
    # Ошибка разбора или кода не про базу: глушить ее значило бы молча
    # терять core каждый день.
    def broken(*a, **k):
        raise KeyError("photos")
    monkeypatch.setattr(collect.db, "upsert_photos", broken)

    with pytest.raises(KeyError):
        collect._store(FakeConn(), 1, 1, date(2026, 11, 26), [OFFER], page=1, dry_run=False)
    assert collect._core_failed["pages"] == 0
