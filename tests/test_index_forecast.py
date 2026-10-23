"""Тесты задачи об индексе и прогнозе. Без базы и без сети: данные
синтетические, а правило выбора проверяется на подставленных таблицах."""
import math
from datetime import date

import numpy as np
import pytest

from src import forecast as fc
from src import panel_index as pi


# --- прогноз ---------------------------------------------------------
def test_rw_flat_and_widening():
    y = np.log(np.linspace(100, 120, 40))
    mean, sd = fc.rw(y, 6)
    assert np.allclose(mean, y[-1])
    assert np.allclose(sd / sd[0], np.sqrt(np.arange(1, 7)))


def test_snaive_takes_same_month_a_year_ago():
    y = np.arange(36, dtype=float)
    mean, _ = fc.snaive(y, 6)
    assert list(mean) == [24, 25, 26, 27, 28, 29]


def _table(mape, cov):
    return {s: {m: {"mape": mape[m], "cov80": cov[m]} for m in fc.MODELS}
            for s in ("nominal_1", "nominal_2")}


def test_challenger_needs_ten_percent_gain():
    """Девять процентов выигрыша не дают права заменить ориентир."""
    cov = {m: 0.8 for m in fc.MODELS}
    t = _table({"rw": 2.0, "snaive": 5.0, "ets": 1.82, "sarima": 3.0}, cov)
    assert fc.choose(t)["chosen"] == "rw"
    t = _table({"rw": 2.0, "snaive": 5.0, "ets": 1.80, "sarima": 3.0}, cov)
    assert fc.choose(t)["chosen"] == "ets"


def test_challenger_with_bad_coverage_is_rejected():
    """Точная модель с самоуверенным интервалом не проходит."""
    cov = {"rw": 0.8, "snaive": 0.8, "ets": 0.6, "sarima": 0.8}
    t = _table({"rw": 2.0, "snaive": 5.0, "ets": 1.0, "sarima": 1.7}, cov)
    assert fc.choose(t)["chosen"] == "sarima"


def test_benchmark_is_better_of_two():
    cov = {m: 0.8 for m in fc.MODELS}
    t = _table({"rw": 6.0, "snaive": 3.0, "ets": 5.0, "sarima": 5.0}, cov)
    d = fc.choose(t)
    assert d["benchmark"] == "snaive" and d["chosen"] == "snaive"


class _Conn:
    """Подменная база: два запроса load, ряд аренды и ИПЦ."""
    def __init__(self, rent, cpi):
        self.answers = [rent, cpi]

    def cursor(self):
        conn = self

        class C:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a): self.rows = conn.answers.pop(0)
            def fetchall(self): return self.rows
        return C()


def test_real_series_in_last_month_rubles():
    months = [date(2025, m, 1) for m in (1, 2, 3)]
    rent = [(m, r, 100.0) for m in months for r in (1, 2)]
    cpi = [(months[0], 5.0), (months[1], 10.0), (months[2], 0.0)]
    _, s = fc.load(_Conn(rent, cpi))
    # Цена не менялась, а цены выросли на 10%: в рублях марта январь дороже.
    assert s["real_1"][-1] == pytest.approx(100.0)
    assert s["real_1"][0] == pytest.approx(110.0)
    assert s["nominal_2"][0] == 100.0


def test_add_months_crosses_year():
    assert fc.add_months(date(2026, 8, 1), 6) == date(2027, 2, 1)


# --- индекс по панели ------------------------------------------------
def test_chain_steps_bridge_missing_day():
    """Пропущенный день не рвет цепочку: шаг 08.10-10.10 считается."""
    d1, d2, d3 = date(2026, 10, 8), date(2026, 10, 10), date(2026, 10, 11)
    rows = [(d1, 1, 100.0), (d1, 2, 200.0),
            (d2, 1, 90.0), (d2, 2, 200.0), (d2, 3, 50.0),
            (d3, 1, 90.0), (d3, 3, 55.0)]
    steps = pi.chain_steps(rows)
    assert [(s[0], s[1], s[2]) for s in steps] == [(d2, d1, 2), (d3, d2, 2)]
    assert steps[0][3] == pytest.approx(math.log(0.9) / 2)
    idx = pi.chain_index(steps)
    total = math.exp(math.log(0.9) / 2 + math.log(1.1) / 2)
    assert idx[-1]["index"] == pytest.approx(100 * total, abs=1e-3)


def test_demeaning_equals_district_dummies():
    """Поглощение эффектов района вычитанием средних дает тот же
    коэффициент, что регрессия с фиктивными по району."""
    rng = np.random.default_rng(0)
    n = 600
    g = rng.integers(0, 5, n)
    x = rng.normal(size=n) + g
    y = 0.7 * x + g * 2.0 + rng.normal(scale=0.1, size=n)
    b, _ = pi.cluster_ols(pi.demean(y, g), pi.demean(x, g)[:, None], np.arange(n),
                          n_absorbed=5)
    D = np.column_stack([x] + [(g == k).astype(float) for k in range(5)])
    full = np.linalg.lstsq(D, y, rcond=None)[0]
    assert b[0] == pytest.approx(full[0], rel=1e-9)


def test_detectability_flags_small_noise():
    rng = np.random.default_rng(1)
    series = [{"coef": float(v)} for v in rng.normal(scale=0.0005, size=30)]
    r = pi.detectability(series, np.full(10, 0.01))
    assert r["distinguishes"] and r["mde_pct"] < 1.0
