"""Прогноз ставки аренды Москвы по ряду Росстата на шесть месяцев.

    make forecast

Почему по Росстату, а не по панели. Панель идет с 22.09.2026 и календарного
изменения цены по дням и неделям не различает (см. src/panel_index.py).
Прогноз по месяцу панели был бы подгонкой. У Росстата 140 месяцев с
января 2015, средняя плата за однокомнатную и двухкомнатную квартиру у
частных лиц.

Правило выбора модели записано здесь ДО того, как посчитано хоть одно
число бэктеста, и закоммичено отдельно от результатов. Менять его после
того, как увидены числа, нельзя: иначе выбор модели превращается в
подгонку под бэктест.

Протокол
--------
Ряды. Москва, 1 и 2 комнаты, январь 2015 и далее. Номинальный и
реальный (в рублях последнего месяца, дефлятор ИПЦ Москвы). Модели
работают с логарифмом уровня, ошибки считаются на уровне в рублях.

Кандидаты.
  rw      случайное блуждание, прогноз равен последнему значению;
  snaive  сезонный наивный, прогноз равен значению год назад;
  ets     ETS на логарифме, аддитивная ошибка, тренд нет или
          затухающий, сезонность нет или аддитивная с периодом 12,
          вариант выбирается по AICc на каждом окне обучения;
  sarima  SARIMA(p,1,q)(P,D,Q)12, порядок выбирается один раз по AIC на
          данных ДО первого начала бэктеста (p,q из 0..2, P,Q,D из 0..1),
          дальше на каждом начале переоцениваются только параметры.

Бэктест. Скользящее начало, 24 начала: последнее известное значение от
августа 2024 до июля 2026, горизонты 1-6, цели не позже последнего
месяца ряда. Метрики MAPE по уровню и покрытие интервалов 80% и 95%.

Правило выбора.
  1. Ориентир B это лучший по MAPE из rw и snaive.
  2. Сложная модель (ets или sarima) заменяет B, только если ее MAPE
     ниже MAPE ориентира хотя бы на 10% относительно И покрытие ее
     80%-интервала лежит в [0,70; 0,90].
  3. Если условию отвечают обе, берется та, у которой MAPE ниже.
  4. Иначе прогноз публикуется ориентиром B.
  MAPE здесь это среднее по горизонтам 1-6 и по двум комнатностям,
  номинальный ряд. Выбранный метод один на все четыре ряда: отбирать
  лучший отдельно для каждого ряда значит снова подгонять.

Прогноз. Шесть месяцев после последнего месяца ряда, интервалы 80% и 95%.
Результаты пишутся в results/forecast.json и results/backtest.csv.
"""
from __future__ import annotations

RULE = {
    "origins": 24,
    "horizons": 6,
    "benchmarks": ["rw", "snaive"],
    "challengers": ["ets", "sarima"],
    "min_rel_gain": 0.10,
    "coverage80": [0.70, 0.90],
    "selection_series": "nominal, rooms 1 and 2, mean MAPE over h=1..6",
}

import csv
import json
import logging
import math
import warnings
from datetime import date
from pathlib import Path

import numpy as np

log = logging.getLogger("forecast")

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
Z80, Z95 = 1.2816, 1.9600
MODELS = RULE["benchmarks"] + RULE["challengers"]


# ---------------------------------------------------------------------
# Данные
def load(conn) -> tuple[list[date], dict[str, np.ndarray]]:
    """Четыре ряда в уровне, рубли в месяц.

    Реальный ряд в рублях последнего месяца: дефлятор накапливается из
    помесячного ИПЦ Москвы. Первый месяц ряда служит базой, его
    собственный ИПЦ к прошлому месяцу не нужен.
    """
    with conn.cursor() as cur:
        cur.execute("""
            select period, rooms, price from core.rosstat_rent
            where region = 'Москва' order by period, rooms""")
        rent = cur.fetchall()
        cur.execute("""
            select period, mom_pct from core.rosstat_cpi
            where region = 'Москва' order by period""")
        cpi = dict(cur.fetchall())
    months = sorted({p for p, _, _ in rent})
    by = {(p, r): float(v) for p, r, v in rent}
    level = np.cumprod([1.0] + [1 + float(cpi[m]) / 100 for m in months[1:]])
    series = {}
    for r in (1, 2):
        nominal = np.array([by[(m, r)] for m in months])
        series[f"nominal_{r}"] = nominal
        series[f"real_{r}"] = nominal * level[-1] / level
    return months, series


# ---------------------------------------------------------------------
# Модели. Каждая получает логарифм ряда и возвращает прогноз логарифма
# на горизонты 1..h и полуширины интервалов в логарифмах.
def rw(y, h, **_):
    sigma = np.std(np.diff(y), ddof=1)
    steps = np.sqrt(np.arange(1, h + 1))
    return np.repeat(y[-1], h), sigma * steps


def snaive(y, h, **_):
    sigma = np.std(y[12:] - y[:-12], ddof=1)
    k = np.floor(np.arange(h) / 12) + 1
    return np.array([y[len(y) - 12 + i % 12] for i in range(h)]), sigma * np.sqrt(k)


def ets(y, h, **_):
    import pandas as pd
    from statsmodels.tsa.exponential_smoothing.ets import ETSModel
    # Series, а не массив: get_prediction в statsmodels 0.15 падает на
    # голом numpy, ему нужен индекс у прогноза.
    y = pd.Series(y)
    best = None
    for trend, damped in ((None, False), ("add", True)):
        for seasonal in (None, "add"):
            m = ETSModel(y, error="add", trend=trend, damped_trend=damped,
                         seasonal=seasonal, seasonal_periods=12 if seasonal else None)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                f = m.fit(disp=False)
            if best is None or f.aicc < best[0]:
                best = (f.aicc, f, (trend, damped, seasonal))
    f = best[1]
    p80 = f.get_prediction(start=len(y), end=len(y) + h - 1).summary_frame(alpha=0.2)
    mean = np.asarray(p80["mean"], dtype=float)
    # Интервал аддитивной ETS симметричен в логарифме, поэтому стандартная
    # ошибка восстанавливается из нижней границы 80%.
    return mean, (mean - np.asarray(p80["pi_lower"])) / Z80


def sarima_order(y):
    """Порядок по AIC на данных до первого начала бэктеста."""
    from statsmodels.tsa.statespace.sarimax import SARIMAX
    best = None
    for p in range(3):
        for q in range(3):
            for P in range(2):
                for D in range(2):
                    for Q in range(2):
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore")
                            try:
                                f = SARIMAX(y, order=(p, 1, q),
                                            seasonal_order=(P, D, Q, 12)).fit(disp=False)
                            except Exception:          # невырожденность не гарантирована
                                continue
                        if np.isfinite(f.aic) and (best is None or f.aic < best[0]):
                            best = (f.aic, (p, 1, q), (P, D, Q, 12))
    return best[1], best[2]


def sarima(y, h, order=None, **_):
    from statsmodels.tsa.statespace.sarimax import SARIMAX
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        f = SARIMAX(y, order=order[0], seasonal_order=order[1]).fit(disp=False)
    fc = f.get_forecast(h)
    mean = np.asarray(fc.predicted_mean)
    return mean, np.sqrt(np.asarray(fc.var_pred_mean))


FUNCS = {"rw": rw, "snaive": snaive, "ets": ets, "sarima": sarima}


# ---------------------------------------------------------------------
def backtest(months, series):
    n = len(months)
    first = n - 1 - RULE["origins"]          # последнее известное в первом окне
    rows, orders = [], {}
    for name, level in series.items():
        y = np.log(level)
        orders[name] = sarima_order(y[:first + 1])
        log.info("%s: SARIMA%s%s", name, orders[name][0], orders[name][1])
        for t in range(first, n - 1):
            h = min(RULE["horizons"], n - 1 - t)
            for model in MODELS:
                mean, sd = FUNCS[model](y[:t + 1], h, order=orders[name])
                for i in range(h):
                    actual = level[t + 1 + i]
                    m, s = mean[i], sd[i]
                    rows.append({
                        "series": name, "model": model,
                        "origin": months[t].isoformat()[:7], "h": i + 1,
                        "actual": round(actual, 2),
                        "forecast": round(math.exp(m), 2),
                        "lo80": round(math.exp(m - Z80 * s), 2),
                        "hi80": round(math.exp(m + Z80 * s), 2),
                        "lo95": round(math.exp(m - Z95 * s), 2),
                        "hi95": round(math.exp(m + Z95 * s), 2),
                    })
    return rows, orders


def summarize(rows):
    """MAPE по горизонтам и покрытие, по ряду и модели."""
    out = {}
    for r in rows:
        k = (r["series"], r["model"])
        d = out.setdefault(k, {"ape": {}, "in80": [], "in95": []})
        d["ape"].setdefault(r["h"], []).append(abs(r["forecast"] - r["actual"]) / r["actual"])
        d["in80"].append(r["lo80"] <= r["actual"] <= r["hi80"])
        d["in95"].append(r["lo95"] <= r["actual"] <= r["hi95"])
    table = {}
    for (s, m), d in out.items():
        by_h = {h: 100 * float(np.mean(v)) for h, v in sorted(d["ape"].items())}
        table.setdefault(s, {})[m] = {
            "mape_by_h": {h: round(v, 2) for h, v in by_h.items()},
            "mape": round(float(np.mean(list(by_h.values()))), 3),
            "cov80": round(float(np.mean(d["in80"])), 3),
            "cov95": round(float(np.mean(d["in95"])), 3),
            "n": len(d["in80"]),
        }
    return table


def choose(table) -> dict:
    """Правило из шапки модуля, буквально."""
    sel = ["nominal_1", "nominal_2"]
    mape = {m: float(np.mean([table[s][m]["mape"] for s in sel])) for m in MODELS}
    cov80 = {m: float(np.mean([table[s][m]["cov80"] for s in sel])) for m in MODELS}
    bench = min(RULE["benchmarks"], key=lambda m: mape[m])
    lo, hi = RULE["coverage80"]
    ok = [m for m in RULE["challengers"]
          if mape[m] <= (1 - RULE["min_rel_gain"]) * mape[bench] and lo <= cov80[m] <= hi]
    chosen = min(ok, key=lambda m: mape[m]) if ok else bench
    return {"mape": {m: round(v, 3) for m, v in mape.items()},
            "cov80": {m: round(v, 3) for m, v in cov80.items()},
            "benchmark": bench, "qualified": ok, "chosen": chosen}


def add_months(d: date, k: int) -> date:
    m = d.month - 1 + k
    return date(d.year + m // 12, m % 12 + 1, 1)


def forecast(months, series, model, orders):
    out = {}
    h = RULE["horizons"]
    for name, level in series.items():
        y = np.log(level)
        mean, sd = FUNCS[model](y, h, order=orders[name])
        out[name] = [{
            "month": add_months(months[-1], i + 1).isoformat()[:7],
            "forecast": round(math.exp(mean[i])),
            "lo80": round(math.exp(mean[i] - Z80 * sd[i])),
            "hi80": round(math.exp(mean[i] + Z80 * sd[i])),
            "lo95": round(math.exp(mean[i] - Z95 * sd[i])),
            "hi95": round(math.exp(mean[i] + Z95 * sd[i])),
        } for i in range(h)]
    return out


def decompose(months, series):
    """STL на логарифме: размах сезонности и месяцы пика и дна.

    Размах считается за последние три полных года, потому что сезонность
    рынка аренды меняется: в 2015 и в 2024 это разные рынки."""
    from statsmodels.tsa.seasonal import STL
    out = {}
    for name, level in series.items():
        res = STL(np.log(level), period=12, robust=True).fit()
        seas = np.asarray(res.seasonal)
        last = [i for i, m in enumerate(months) if m.year >= months[-1].year - 3
                and m.year < months[-1].year]
        by_month = {}
        for i in last:
            by_month.setdefault(months[i].month, []).append(seas[i])
        avg = {mo: float(np.mean(v)) for mo, v in by_month.items()}
        out[name] = {
            "amplitude_pct": round(100 * (math.exp(max(avg.values()) - min(avg.values())) - 1), 2),
            "peak_month": max(avg, key=avg.get),
            "trough_month": min(avg, key=avg.get),
            "by_month_pct": {mo: round(100 * v, 2) for mo, v in sorted(avg.items())},
            "yoy_last_pct": round(100 * (level[-1] / level[-13] - 1), 2),
        }
    return out


def main() -> int:
    from etl import db
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with db.connect() as conn:
        months, series = load(conn)
    log.info("Ряд Росстата: %d месяцев, %s .. %s", len(months), months[0], months[-1])
    rows, orders = backtest(months, series)
    table = summarize(rows)
    decision = choose(table)
    log.info("Выбор по правилу: %s", decision)
    fc = forecast(months, series, decision["chosen"], orders)
    dec = decompose(months, series)

    RESULTS.mkdir(exist_ok=True)
    with open(RESULTS / "backtest.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    payload = {
        "source": "Росстат, ЕМИСС: средняя плата за аренду у частных лиц, Москва",
        "months": len(months),
        "first_month": months[0].isoformat()[:7],
        "last_month": months[-1].isoformat()[:7],
        "rule": RULE,
        "sarima_orders": {k: [list(v[0]), list(v[1])] for k, v in orders.items()},
        "backtest": table,
        "decision": decision,
        "forecast": fc,
        "decomposition": dec,
        "last_level": {k: round(float(v[-1])) for k, v in series.items()},
    }
    (RESULTS / "forecast.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    log.info("results/forecast.json, results/backtest.csv")
    from src.index_figures import fig_forecast
    fig_forecast({k: list(zip(months, v)) for k, v in series.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
