"""Тесты сборки панели объявление-день для оценки снижения цены. Без
базы: строки собираются руками в том же порядке колонок, что SQL."""
from datetime import date, timedelta

import numpy as np

from src.cut_effect import build_panel, hazard

D0 = date(2026, 10, 1)


def row(offer, k, price, gone=None, last=None, pub=None, seller="owner"):
    pub = pub or D0 - timedelta(days=5)
    return (offer, D0 + timedelta(days=k), price, pub, D0, last or D0 + timedelta(days=k),
            gone, "Хамовники", 1, seller, 10, 60000.0)


def test_event_on_last_row_and_cut_is_sticky():
    last = D0 + timedelta(days=2)
    rows = [row("a", 0, 60000, gone=D0 + timedelta(days=3), last=last),
            row("a", 1, 55000, gone=D0 + timedelta(days=3), last=last),
            row("a", 2, 56000, gone=D0 + timedelta(days=3), last=last)]
    p = build_panel(rows, last_day=D0 + timedelta(days=10))
    assert list(p["y"]) == [0, 0, 1]
    # Снижение на втором дне, и признак остается после подъема цены.
    assert list(p["cut"]) == [0, 1, 1]
    assert list(p["first_cut"]) == [False, True, False]
    assert list(p["age"]) == [5, 6, 7]


def test_unconfirmed_exit_is_not_an_event():
    """Снятие позже cutoff еще не подтверждено, объявление живое."""
    last_day = D0 + timedelta(days=4)
    rows = [row("b", k, 50000, gone=D0 + timedelta(days=4)) for k in range(2)]
    p = build_panel(rows, last_day=last_day)
    assert p["y"].sum() == 0
    assert len(p["y"]) == 2           # cutoff = 1-е число + 1 день


def test_rows_after_cutoff_dropped_and_outcome_window():
    rows = [row("c", k, 50000) for k in range(10)]
    p = build_panel(rows, last_day=D0 + timedelta(days=12))
    assert len(p["y"]) == 10
    # Исход за неделю виден только у дней не позже cutoff - 7.
    assert list(p["obs7"]) == [True] * 3 + [False] * 7


def test_born_cohort_flag():
    rows = [row("d", 0, 50000, pub=D0 - timedelta(days=1)),
            row("e", 0, 50000, pub=D0 - timedelta(days=9))]
    p = build_panel(rows, last_day=D0 + timedelta(days=10))
    assert list(p["born"]) == [True, False]


def test_hazard_recovers_planted_effect():
    """На синтетике с известной прибавкой к риску модель ее находит."""
    rng = np.random.default_rng(3)
    rows = []
    for o in range(800):
        cut_day = rng.integers(3, 12) if o % 2 else 99
        prices, gone = [], None
        for k in range(14):
            prices.append(50000 if k < cut_day else 47000)
            if rng.random() < 0.02 + (0.06 if k >= cut_day else 0.0):
                gone = D0 + timedelta(days=k + 1)
                break
        # Дата ухода и последний день стоят у всех строк объявления, как
        # в core.offer.
        last = D0 + timedelta(days=len(prices) - 1)
        rows += [row(o, k, pr, gone=gone, last=last) for k, pr in enumerate(prices)]
    p = build_panel(rows, last_day=D0 + timedelta(days=30))
    r = hazard(p)
    assert r["lo95_pp"] <= 6.0 <= r["hi95_pp"]
