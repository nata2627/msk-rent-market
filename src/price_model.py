"""Модель справедливой цены аренды и ее эксплуатация.

    make price-model

Зачем своя модель, если у площадки есть оценка. Три отличия, ради них
все и делается.

1. Покрытие. Оценка площадки есть меньше чем у половины объявлений.
2. Цель. Площадка оценивает запрашиваемую цену. Панель видит еще и то,
   что с рынка ушло, поэтому вторая модель учится на цене в последний
   день перед уходом. Уход не равен сдаче (см. README), и это цена
   последнего объявления, а не договора.
3. Объяснение. К оценке приложено, сколько дней такое объявление
   обычно висит.

Протокол
--------
Единица наблюдения это объявление, цена и признаки в день первой
встречи. Студия кодируется нулем комнат, берутся 0-4 комнаты, без
посуточных и без выбросов по цене (выбросы показываются отдельно).

Разбиение по времени и по группам дублей. Проверка это объявления,
впервые увиденные в последние TEST_DAYS дней. Группа дублей из
core.offer_duplicate целиком уходит в ту часть, где появился ее первый
представитель, иначе одна квартира оказалась бы и в обучении, и в
проверке. Последние CALIB_DAYS дней обучения отложены под конформную
калибровку.

Соперники. Ориентир это гедоническая регрессия на признаках
src/hedonic.py с эффектами района. Соперник это CatBoost на тех же
признаках плюс то, что регрессия не берет: координаты, район как
категория, кухня и жилая площадь, условия сделки, техника.

Метрики MAE в рублях и MAPE, по комнатности и по квартилям цены.
Интервал 80% через раздельное конформное предсказание по модулю
логарифма остатка на калибровочной части, покрытие проверяется на
отложенной.

Дрейф. Модель, обученная на всем, что было до недели W, оценивается на
объявлениях, впервые увиденных в неделю W. Порог тревоги выводится из
первых четырех недель (среднее плюс два стандартных отклонения MAPE), а
не назначается.
"""
from __future__ import annotations

import csv
import json
import logging
import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np

log = logging.getLogger("price_model")

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
MODELS_DIR = ROOT / "data" / "models"
MLRUNS = ROOT / "data" / "mlruns"

TEST_DAYS = 10
CALIB_DAYS = 7
ALPHA = 0.2                 # интервал 80%
DRIFT_WEEKS_FOR_THRESHOLD = 4
CURRENT_YEAR = 2026

SQL = """
with first_obs as (
    select distinct on (ob.offer_id) ob.*
    from core.offer_observation ob
    where ob.source = 'cian' and not ob.is_daily and ob.price > 0
      and coalesce(ob.lease_term_type, 'longTerm') = 'longTerm'
    order by ob.offer_id, ob.snapshot_date),
last_obs as (
    select distinct on (ob.offer_id) ob.offer_id, ob.price as last_price,
           ob.snapshot_date as last_date
    from core.offer_observation ob
    where ob.source = 'cian' and not ob.is_daily and ob.price > 0
    order by ob.offer_id, ob.snapshot_date desc)
select f.offer_id, f.snapshot_date as first_date, f.price::float8,
       l.last_price::float8, l.last_date,
       case when o.flat_type = 'studio' then 0 else o.rooms_count end as rooms,
       o.total_area::float8, o.living_area::float8, o.kitchen_area::float8,
       o.floor, o.floors_total, o.build_year, o.building_material,
       coalesce(o.has_lift, false) as has_lift, o.lat, o.lon, o.district,
       o.metro_walk_min, o.metro_real_walk_min, o.large_park_distance_m,
       coalesce(o.inside_mkad, false) as inside_mkad,
       coalesce(o.is_apartments, false) as is_apartments,
       o.published_at::date as published, o.disappeared_date,
       coalesce(d.canonical_id, o.offer_id) as dup_group,
       f.seller_type, f.photos_count, f.deposit_months::float8,
       f.client_fee_pct::float8, f.prepay_months, f.utilities_included,
       f.pets_state, f.children_allowed, f.owner_verified,
       f.seller_offers_count, length(coalesce(f.description, '')) as descr_len,
       coalesce(f.has_furniture, false) as has_furniture,
       coalesce(f.has_washer, false) as has_washer,
       coalesce(f.has_dishwasher, false) as has_dishwasher,
       coalesce(f.has_conditioner, false) as has_conditioner,
       coalesce(f.has_fridge, false) as has_fridge,
       coalesce(f.has_tv, false) as has_tv,
       coalesce(f.has_internet, false) as has_internet,
       coalesce(f.balconies_count, 0) + coalesce(f.loggias_count, 0) as balconies,
       f.price_estimate_low::float8 as cian_low, f.price_estimate_high::float8 as cian_high
from first_obs f
join core.offer o using (source, offer_id)
join last_obs l using (offer_id)
left join core.offer_duplicate d using (source, offer_id)
where o.total_area > 0 and o.district is not null
  and o.floor is not null and o.floors_total is not null
  and (o.rooms_count between 1 and 4 or o.flat_type = 'studio')
"""


def is_outlier(price: float, area: float) -> bool:
    """Та же формула, что is_price_outlier в marts.offer_segment."""
    return price < 10000 or price > 5_000_000 or not (400 <= price / area <= 15000)


# ---------------------------------------------------------------------
# Разбиение
def split(rows: list[dict], last_day: date, test_days=TEST_DAYS, calib_days=CALIB_DAYS):
    """Метка части для каждой строки: train, calib или test.

    Решение принимается по группе дублей: по дню первой встречи ее
    самого раннего представителя. Так одна квартира, выставленная двумя
    агентствами, не попадет по разные стороны границы."""
    test_from = last_day - timedelta(days=test_days - 1)
    calib_from = test_from - timedelta(days=calib_days)
    group_first: dict[str, date] = {}
    for r in rows:
        g = r["dup_group"]
        group_first[g] = min(group_first.get(g, r["first_date"]), r["first_date"])
    parts = []
    for r in rows:
        d = group_first[r["dup_group"]]
        parts.append("test" if d >= test_from else "calib" if d >= calib_from else "train")
    return np.array(parts)


# ---------------------------------------------------------------------
# Признаки
def _num(rows, key, transform=float):
    vals = [r[key] for r in rows]
    present = [transform(v) for v in vals if v is not None]
    med = float(np.median(present)) if present else 0.0
    return (np.array([transform(v) if v is not None else med for v in vals]),
            np.array([v is None for v in vals], dtype=float))


def hedonic_matrix(rows, districts: list[str]):
    """Признаки ориентира: смысл тот же, что hedonic.controls, плюс
    фиктивные по району из списка обучения. Район, которого в обучении
    не было, получает нулевой эффект, то есть средний по городу."""
    cols, names = [np.ones(len(rows))], ["константа"]
    def add(c, n):
        cols.append(np.asarray(c, dtype=float)); names.append(n)
    add([math.log(r["total_area"]) for r in rows], "log площадь")
    for k in (0, 2, 3, 4):
        add([r["rooms"] == k for r in rows], f"комнат:{k}")
    add([r["floor"] == 1 for r in rows], "первый этаж")
    add([r["floor"] == r["floors_total"] for r in rows], "последний этаж")
    add([math.log(max(r["floors_total"], 1)) for r in rows], "log этажей")
    v, f = _num(rows, "build_year", lambda y: float(CURRENT_YEAR - y))
    add(v, "возраст дома"); add(f, "возраст: нет")
    v, f = _num(rows, "metro_walk_min")
    add(v, "минут до метро"); add(f, "метро: нет")
    add([math.log1p(r["large_park_distance_m"] or 0) for r in rows], "log до парка")
    for k in ("inside_mkad", "is_apartments", "has_furniture", "has_washer",
              "has_dishwasher", "has_conditioner", "has_lift"):
        add([bool(r[k]) for r in rows], k)
    v, f = _num(rows, "photos_count")
    add(v, "фотографий"); add(f, "фото: нет")
    for s in ("homeowner", "realtor_not_commerce"):
        add([r["seller_type"] == s for r in rows], f"продавец:{s}")
    for m in ("brick", "monolith", "monolithBrick", "block", "wood", "stalin", "old"):
        add([r["building_material"] == m for r in rows], f"материал:{m}")
    for dname in districts[1:]:
        add([r["district"] == dname for r in rows], f"район:{dname}")
    return np.column_stack(cols), names


CAT_FEATURES = ["district", "building_material", "seller_type", "pets_state"]
NUM_FEATURES = ["rooms", "total_area", "living_area", "kitchen_area", "floor",
                "floors_total", "build_year", "lat", "lon", "metro_walk_min",
                "metro_real_walk_min", "large_park_distance_m", "photos_count",
                "deposit_months", "client_fee_pct", "prepay_months",
                "seller_offers_count", "descr_len", "balconies"]
BOOL_FEATURES = ["has_lift", "inside_mkad", "is_apartments", "utilities_included",
                 "children_allowed", "owner_verified", "has_furniture", "has_washer",
                 "has_dishwasher", "has_conditioner", "has_fridge", "has_tv",
                 "has_internet"]


def boost_frame(rows):
    """Таблица для CatBoost. Пропуски оставлены пропусками: деревья
    умеют с ними работать, и заполнение медианой стерло бы смысл."""
    import pandas as pd
    data = {}
    for k in CAT_FEATURES:
        data[k] = [r[k] if r[k] is not None else "нет данных" for r in rows]
    for k in NUM_FEATURES:
        data[k] = [float(r[k]) if r[k] is not None else np.nan for r in rows]
    for k in BOOL_FEATURES:
        data[k] = [float(r[k]) if r[k] is not None else np.nan for r in rows]
    return pd.DataFrame(data)


# ---------------------------------------------------------------------
# Модели
class Hedonic:
    name = "hedonic"

    def fit(self, rows, y):
        self.districts = sorted({r["district"] for r in rows})
        X, self.names = hedonic_matrix(rows, self.districts)
        self.beta = np.linalg.lstsq(X, y, rcond=None)[0]
        return self

    def predict(self, rows):
        X, _ = hedonic_matrix(rows, self.districts)
        return X @ self.beta


class Boost:
    name = "catboost"

    def __init__(self, iterations=1500, seed=0):
        self.params = dict(iterations=iterations, depth=6, learning_rate=0.05,
                           loss_function="RMSE", random_seed=seed, verbose=False,
                           thread_count=2, allow_writing_files=False)

    def fit(self, rows, y):
        from catboost import CatBoostRegressor
        self.model = CatBoostRegressor(**self.params)
        self.model.fit(boost_frame(rows), y, cat_features=CAT_FEATURES)
        return self

    def predict(self, rows):
        return self.model.predict(boost_frame(rows))


# ---------------------------------------------------------------------
# Метрики
def metrics(actual, pred):
    ape = np.abs(pred - actual) / actual
    return {"n": int(actual.size), "mae_rub": round(float(np.mean(np.abs(pred - actual)))),
            "mape_pct": round(100 * float(np.mean(ape)), 2),
            "median_ape_pct": round(100 * float(np.median(ape)), 2)}


def by_group(actual, pred, groups):
    return {str(g): metrics(actual[groups == g], pred[groups == g])
            for g in sorted(set(groups.tolist())) if (groups == g).sum() >= 30}


def conformal_q(log_resid_calib: np.ndarray, alpha=ALPHA) -> float:
    """Квантиль модуля логарифма остатка с поправкой конечной выборки:
    гарантия покрытия не ниже 1 - alpha при обмениваемости."""
    n = log_resid_calib.size
    k = min(n, math.ceil((n + 1) * (1 - alpha)))
    return float(np.sort(np.abs(log_resid_calib))[k - 1])


def price_band(price: np.ndarray, edges) -> np.ndarray:
    labels = ["до " + str(int(edges[0] / 1000)) + " тыс",
              f"{int(edges[0] / 1000)}-{int(edges[1] / 1000)} тыс",
              f"{int(edges[1] / 1000)}-{int(edges[2] / 1000)} тыс",
              "от " + str(int(edges[2] / 1000)) + " тыс"]
    return np.array([labels[i] for i in np.searchsorted(edges, price, side="right")])


# ---------------------------------------------------------------------
def load(conn):
    with conn.cursor() as cur:
        cur.execute(SQL)
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.execute("select max(snapshot_date) from core.offer_observation where source='cian'")
        last_day = cur.fetchone()[0]
    return rows, last_day


def evaluate(rows, last_day):
    """Обучение на train, калибровка на calib, проверка на test."""
    clean = [r for r in rows if not is_outlier(r["price"], r["total_area"])]
    outliers = len(rows) - len(clean)
    parts = split(clean, last_day)
    y = np.log([r["price"] for r in clean])
    idx = {p: np.where(parts == p)[0] for p in ("train", "calib", "test")}
    sub = lambda p: [clean[i] for i in idx[p]]
    actual = np.exp(y[idx["test"]])
    edges = np.quantile(np.exp(y[idx["train"]]), [0.25, 0.5, 0.75])
    rooms = np.array([r["rooms"] for r in sub("test")])
    band = price_band(actual, edges)
    out = {"split": {p: int(v.size) for p, v in idx.items()},
           "outliers_excluded": outliers, "models": {}}
    preds = {}
    for M in (Hedonic, Boost):
        m = M().fit(sub("train"), y[idx["train"]])
        q = conformal_q(y[idx["calib"]] - m.predict(sub("calib")))
        p_log = m.predict(sub("test"))
        pred = np.exp(p_log)
        preds[m.name] = pred
        inside = np.abs(y[idx["test"]] - p_log) <= q
        out["models"][m.name] = {
            "test": metrics(actual, pred),
            "by_rooms": by_group(actual, pred, rooms),
            "by_price_band": by_group(actual, pred, band),
            "conformal_q_log": round(q, 4),
            "interval_halfwidth_pct": round(100 * (math.exp(q) - 1), 1),
            "coverage80": round(float(inside.mean()), 3),
            "coverage80_by_rooms": {str(k): round(float(inside[rooms == k].mean()), 3)
                                    for k in sorted(set(rooms.tolist()))},
        }
    out["cian"] = compare_cian(sub("test"), preds["catboost"])
    return out, clean, parts


def compare_cian(test_rows, pred):
    """Сравнение двух моделей, а не проверка по истине."""
    lo = np.array([r["cian_low"] or np.nan for r in test_rows], dtype=float)
    hi = np.array([r["cian_high"] or np.nan for r in test_rows], dtype=float)
    price = np.array([r["price"] for r in test_rows])
    has = ~np.isnan(lo) & ~np.isnan(hi)
    mid = (lo + hi) / 2
    rooms = np.array([r["rooms"] for r in test_rows])
    out = {"test_offers": int(len(test_rows)), "with_cian_estimate": int(has.sum()),
           "coverage_share_pct": round(100 * float(has.mean()), 1),
           "ours_inside_cian_range_pct": round(100 * float(((pred >= lo) & (pred <= hi))[has].mean()), 1),
           "ask_inside_cian_range_pct": round(100 * float(((price >= lo) & (price <= hi))[has].mean()), 1),
           "ours_vs_cian_mid_median_pct": round(100 * float(np.median(pred[has] / mid[has] - 1)), 2),
           "by_rooms_ours_vs_cian_mid_pct": {
               str(k): round(100 * float(np.median(pred[has & (rooms == k)] / mid[has & (rooms == k)] - 1)), 2)
               for k in sorted(set(rooms.tolist())) if (has & (rooms == k)).sum() >= 30}}
    return out


def exit_model(clean, last_day):
    """Вторая модель: цена в последний день перед подтвержденным уходом.

    Обучается на ушедших, проверяется сравнением с моделью запрашиваемой
    цены на одних и тех же объявлениях: держат ли данные гипотезу, что
    уходит дешевле, чем просят на входе."""
    cutoff = last_day - timedelta(days=3)
    gone = [r for r in clean if r["disappeared_date"] is not None
            and r["disappeared_date"] <= cutoff]
    ratio = np.array([r["last_price"] / r["price"] for r in gone])
    parts = split(gone, cutoff)
    tr = [r for r, p in zip(gone, parts) if p != "test"]
    te = [r for r, p in zip(gone, parts) if p == "test"]
    ask_all = [r for r in clean]
    m_exit = Boost().fit(tr, np.log([r["last_price"] for r in tr]))
    m_ask = Boost().fit([r for r in ask_all if r["first_date"] < min(x["first_date"] for x in te)],
                        np.log([r["price"] for r in ask_all
                                if r["first_date"] < min(x["first_date"] for x in te)]))
    rel = np.exp(m_exit.predict(te) - m_ask.predict(te))
    return {"exited_offers": len(gone),
            "last_vs_first_price_median_pct": round(100 * float(np.median(ratio) - 1), 2),
            "share_with_cut_pct": round(100 * float((ratio < 1).mean()), 1),
            "test_exited": len(te),
            "exit_model_vs_ask_model_median_pct": round(100 * float(np.median(rel) - 1), 2),
            "exit_model_vs_ask_model_mean_pct": round(100 * float(np.mean(rel) - 1), 2),
            "test_exit_mape_pct": metrics(np.array([r["last_price"] for r in te]),
                                          np.exp(m_exit.predict(te)))["mape_pct"]}


def drift_history(clean, last_day):
    """MAPE модели, обученной до недели, на объявлениях этой недели.

    Неделя первой встречи 22.09 исключена из проверки: в первый день
    панели «новыми» выглядят все объявления, висевшие на рынке."""
    first = min(r["first_date"] for r in clean)
    starts, d = [], first + timedelta(days=7)
    while d + timedelta(days=6) <= last_day:
        starts.append(d)
        d += timedelta(days=7)
    out = []
    for s in starts:
        e = s + timedelta(days=7)
        tr = [r for r in clean if r["first_date"] < s]
        te = [r for r in clean if s <= r["first_date"] < e]
        if len(te) < 200:
            continue
        m = Boost(iterations=800).fit(tr, np.log([r["price"] for r in tr]))
        mt = metrics(np.array([r["price"] for r in te]), np.exp(m.predict(te)))
        out.append({"week_start": s.isoformat(), "train": len(tr), **mt})
    return out


def drift_threshold(history) -> float | None:
    first = [h["mape_pct"] for h in history[:DRIFT_WEEKS_FOR_THRESHOLD]]
    if len(first) < DRIFT_WEEKS_FOR_THRESHOLD:
        return None
    return round(float(np.mean(first) + 2 * np.std(first, ddof=1)), 2)


# ---------------------------------------------------------------------
# Эксплуатация: финальная модель, таблицы для бота, журнал прогонов
def typical_days(clean, ratio):
    """Медиана срока жизни по Каплану-Мейеру с поздним вхождением для
    комнатности и положения цены относительно оценки. Отсюда строка
    «такое обычно висит N дней» в ответе бота."""
    from src.survival import km, median_age, spans
    last = max(r["first_date"] for r in clean)
    cutoff = last - timedelta(days=3)
    band = np.where(ratio < 0.95, "дешевле", np.where(ratio > 1.05, "дороже", "в рынке"))
    out = {}
    for rooms in sorted({r["rooms"] for r in clean}):
        for b in ("дешевле", "в рынке", "дороже"):
            sel = [{"published": r["published"], "first_seen": r["first_date"],
                    "disappeared": r["disappeared_date"]}
                   for r, bb in zip(clean, band) if r["rooms"] == rooms and bb == b
                   and r["published"] is not None]
            if len(sel) < 100:
                continue
            out[(rooms, b)] = median_age(km(spans(sel, cutoff)))
    return out, band


def grid_rows(clean, model, q, version):
    """Оценка по словам: типичная квартира района. Ячейка района и
    комнатности публикуется только от пяти объявлений, как все агрегаты."""
    from collections import Counter
    by_cell: dict[tuple, list] = {}
    for r in clean:
        by_cell.setdefault((r["district"], r["rooms"]), []).append(r)
    areas = {k: np.quantile([r["total_area"] for r in clean if r["rooms"] == k], [0.05, 0.95])
             for k in range(5)}
    out, probe = [], []
    for (district, rooms), rs in by_cell.items():
        if len(rs) < 5:
            continue
        base = {}
        for k in NUM_FEATURES + BOOL_FEATURES:
            vals = [r[k] for r in rs if r[k] is not None]
            base[k] = float(np.median([float(v) for v in vals])) if vals else None
        for k in CAT_FEATURES:
            vals = [r[k] for r in rs if r[k] is not None]
            base[k] = Counter(vals).most_common(1)[0][0] if vals else None
        lo, hi = areas[rooms]
        for a in range(int(lo // 5 * 5), int(hi) + 1, 5):
            if a < 10:
                continue
            row = dict(base, district=district, rooms=rooms, total_area=float(a),
                       living_area=None, kitchen_area=None)
            probe.append(row)
            out.append((district, rooms, a, len(rs)))
    est = np.exp(model.predict(probe))
    return [(d, r, a, version, round(e), round(e * math.exp(-q)), round(e * math.exp(q)), n)
            for (d, r, a, n), e in zip(out, est)]


def write_tables(conn, version, today, est_rows, grid, drift, threshold):
    sql = (ROOT / "sql" / "schema" / "05_ml.sql").read_text()
    with conn.cursor() as cur:
        cur.execute(sql)
        # Пересчитывается целиком: старые оценки после переобучения не
        # нужны, а смешение версий в одной таблице сбивало бы бота.
        cur.execute("truncate ml.price_estimate, ml.price_grid")
        cur.executemany("""insert into ml.price_estimate values
            (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", est_rows)
        cur.executemany("insert into ml.price_grid values (%s,%s,%s,%s,%s,%s,%s,%s)", grid)
        for h in drift:
            cur.execute("""insert into ml.price_drift values (%s,%s,%s,%s,%s,%s)
                on conflict (week_start) do update set model_version = excluded.model_version,
                n = excluded.n, mae_rub = excluded.mae_rub, mape_pct = excluded.mape_pct,
                threshold_pct = excluded.threshold_pct""",
                        (h["week_start"], version, h["n"], h["mae_rub"], h["mape_pct"], threshold))
    conn.commit()


def log_mlflow(version, report, params):
    """MLflow без сервера, хранилище одним файлом SQLite: сервер на машине
    сбора не поместится. Каталог mlruns в MLflow 3 переведен в режим
    поддержки и без особого флага не открывается, SQLite это
    рекомендованная замена и тоже просто файл. В репозиторий идет
    сводка results/price_runs.csv, сама база MLflow нет."""
    import os
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    import mlflow
    MLRUNS.mkdir(parents=True, exist_ok=True)
    mlflow.set_tracking_uri(f"sqlite:///{(MLRUNS / 'mlflow.db').resolve()}")
    mlflow.set_experiment("fair_price")
    with mlflow.start_run(run_name=version):
        mlflow.log_params(params)
        for name, m in report["models"].items():
            mlflow.log_metric(f"{name}_mae_rub", m["test"]["mae_rub"])
            mlflow.log_metric(f"{name}_mape_pct", m["test"]["mape_pct"])
            mlflow.log_metric(f"{name}_coverage80", m["coverage80"])


def append_runs(version, report, drift, threshold):
    path = RESULTS / "price_runs.csv"
    new = not path.exists()
    with open(path, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["version", "model", "test_n", "mae_rub", "mape_pct",
                        "coverage80", "last_week_mape_pct", "drift_threshold_pct"])
        last = drift[-1]["mape_pct"] if drift else ""
        for name, m in report["models"].items():
            w.writerow([version, name, m["test"]["n"], m["test"]["mae_rub"],
                        m["test"]["mape_pct"], m["coverage80"], last,
                        threshold if threshold is not None else ""])


def main() -> int:
    import argparse
    from etl import db
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-write", action="store_true", help="не писать в схему ml")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with db.connect() as conn:
        rows, last_day = load(conn)
        version = f"price-{last_day:%Y%m%d}"
        log.info("Объявлений: %d, последний день %s", len(rows), last_day)
        report, clean, _ = evaluate(rows, last_day)
        log.info("Проверка: %s", {k: (v["test"], v["coverage80"]) for k, v in report["models"].items()})
        report["exit"] = exit_model(clean, last_day)
        log.info("Цена ухода: %s", report["exit"])
        drift = drift_history(clean, last_day)
        threshold = drift_threshold(drift)
        report["drift"] = {"weeks": drift, "threshold_pct": threshold}
        log.info("Дрейф: %s, порог %s", drift, threshold)

        # Финальные модели на всем чистом, интервал из калибровки выше.
        q = report["models"]["catboost"]["conformal_q_log"]
        final = Boost().fit(clean, np.log([r["price"] for r in clean]))
        cutoff = last_day - timedelta(days=3)
        gone = [r for r in clean if r["disappeared_date"] is not None
                and r["disappeared_date"] <= cutoff]
        final_exit = Boost().fit(gone, np.log([r["last_price"] for r in gone]))
        est_all = np.exp(final.predict(clean))
        ratio = np.array([r["price"] for r in clean]) / est_all
        days, _ = typical_days(clean, ratio)
        report["typical_days"] = {f"{k[0]}|{k[1]}": v for k, v in days.items()}

        with conn.cursor() as cur:
            cur.execute("select offer_id from core.offer where source='cian' and is_active")
            active = {r[0] for r in cur.fetchall()}
        live = [r for r in clean if r["offer_id"] in active]
        e = np.exp(final.predict(live))
        ex = np.exp(final_exit.predict(live))
        est_rows = []
        for r, est, exv in zip(live, e, ex):
            rel = r["last_price"] / est
            b = "дешевле" if rel < 0.95 else "дороже" if rel > 1.05 else "в рынке"
            est_rows.append((r["offer_id"], version, last_day, r["rooms"], r["district"],
                             round(r["total_area"], 1), round(r["last_price"]), round(est),
                             round(est * math.exp(-q)), round(est * math.exp(q)), round(exv),
                             r["cian_low"], r["cian_high"], days.get((r["rooms"], b))))
        grid = grid_rows(clean, final, q, version)
        report["serving"] = {"estimates": len(est_rows), "grid_cells": len(grid)}
        if not args.no_write:
            write_tables(conn, version, last_day, est_rows, grid, drift, threshold)

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    final.model.save_model(str(MODELS_DIR / f"{version}.cbm"))
    RESULTS.mkdir(exist_ok=True)
    report["version"] = version
    (RESULTS / "price_model.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    append_runs(version, report, drift, threshold)
    log_mlflow(version, report, {"iterations": 1500, "depth": 6, "lr": 0.05,
                                 "test_days": TEST_DAYS, "calib_days": CALIB_DAYS,
                                 "alpha": ALPHA, "rows": len(clean)})
    log.info("results/price_model.json, ml.price_estimate %d, ml.price_grid %d",
             len(est_rows), len(grid))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
