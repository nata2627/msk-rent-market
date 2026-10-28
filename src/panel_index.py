"""Индекс аренды по собственной панели как текущий замер.

    make index

Это замер, а не прогноз. Панель идет с 22.09.2026, и задача здесь в том,
чтобы честно показать, что она уже видит и чего еще не видит.

Протокол записан до расчета, вместе с правилом прогноза (src/forecast.py).

Выборка. Собственный сбор, 1 и 2 комнаты, дни с успешным прогоном, без
посуточных, дублей и выбросов по цене. Одна строка на объявление и день.

Два способа.
  chain     цепной индекс по совпавшим объявлениям: шаг между соседними
            собранными днями равен среднему ln(p_t / p_{t-1}) по
            объявлениям, бывшим в выдаче в оба дня. Пропущенный день не
            рвет цепочку, шаг просто охватывает два дня.
  hedonic   гедоническая регрессия log цены на фиктивные по дате,
            признаки квартиры (те же, что в src/hedonic.py) и
            фиксированные эффекты района. Ошибки кластерные по
            объявлению, одно объявление наблюдается много дней.

Два способа меряют разное, и это ожидаемо. Цепной видит только тех, кто
остался, и только их пересмотры цены, а пересматривают почти всегда
вниз. Гедонический видит и новые объявления, входящие по новой цене.

Что считается «плоским». Изменение гедонического индекса от первого дня
до последнего, у которого 95%-интервал накрывает ноль.

С какого месяца панель начнет различать. Месячное изменение это разность
средних за два полных месяца. Его ошибка оценивается по дневным
значениям гедонического индекса с поправкой на автокорреляцию, отсюда
минимально различимое изменение MDE = 2,8 · SE (5% и мощность 80%).
Различать панель начинает, когда MDE меньше типичного месячного
изменения по Росстату, медианы |Δ log| за последние 36 месяцев. Первая
пара полных месяцев панели это октябрь и ноябрь 2026.

Трешки (с 28.10.2026). Тот же протокол отдельным рядом, только 3
комнаты: 4+ комнат мало, и они слишком разные. День засчитывается, если
в нем успешно обойдена корзина трешек. До 12.10 полный обход шел раз в
неделю, поэтому начало ряда идет недельными шагами, и различимость
месячного изменения считается только по дневному хвосту (соседние дни
не дальше двух суток). Своего ряда у Росстата для трешек нет (в
справочнике ЕМИСС аренда только одно- и двухкомнатной), поэтому
различимость сравнивается с типичным изменением ряда 1-2к, с пометкой.

Связка с Росстатом считается только по месяцам, где у панели полный
месяц и у Росстата уже опубликовано значение. Пока таких месяцев один
или ноль, результат помечается как предварительный и выводов из него
не делается.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import numpy as np

log = logging.getLogger("panel_index")

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
CURRENT_YEAR = 2026

# Дни, когда обе комнатности искомого сегмента обойдены успешным прогоном.
# Тот же смысл, что у marts.room_crawl_day: упавший прогон не доказывает
# ни присутствия, ни отсутствия.
def covered_sql(rooms) -> str:
    codes = ", ".join(f"'{r}'" for r in rooms)
    return f"""
    select r.snapshot_date
    from raw.collection_run r join raw.bucket b using (run_id)
    where r.source = 'cian' and r.status = 'ok' and b.error is null
      and b.rooms in ({codes})
    group by 1 having count(distinct b.rooms) = {len(rooms)}
"""


COVERED = covered_sql((1, 2))
SEGMENTS = {"12": (1, 2), "3": (3,)}

# Выброс по цене считается по цене наблюдения, а не по последней: та же
# формула, что is_price_outlier в marts.offer_segment.
def obs_sql(rooms) -> str:
    return OBS_TEMPLATE.format(rooms=", ".join(map(str, rooms)), covered=covered_sql(rooms))


OBS_TEMPLATE = """
    select ob.snapshot_date, ob.offer_id, ob.price::float8,
           o.total_area::float8, o.rooms_count, o.floor, o.floors_total,
           o.build_year, o.metro_walk_min, o.large_park_distance_m::float8,
           coalesce(o.inside_mkad, false), coalesce(o.is_apartments, false),
           coalesce(ob.has_furniture, false), coalesce(ob.has_washer, false),
           coalesce(ob.has_dishwasher, false), coalesce(ob.has_conditioner, false),
           coalesce(o.has_lift, false), ob.photos_count, ob.seller_type,
           o.building_material, o.district
    from core.offer_observation ob
    join core.offer o using (source, offer_id)
    left join core.offer_duplicate d using (source, offer_id)
    where ob.source = 'cian' and o.rooms_count in ({rooms})
      and ob.snapshot_date in ({covered})
      and not ob.is_daily and ob.price > 0
      and o.total_area > 0 and o.district is not null
      and o.floor is not null and o.floors_total is not null
      and coalesce(d.canonical_id, o.offer_id) = o.offer_id
      and not (ob.price < 10000 or ob.price > 5000000
               or ob.price / o.total_area not between 400 and 15000)
"""
OBS = obs_sql((1, 2))


def chain_steps(rows):
    """Шаги цепного индекса между соседними собранными днями.

    Соседний значит следующий по списку собранных, а не календарный:
    пропуск 09.10 дает шаг 08.10-10.10, и цепочка не рвется. Считается
    здесь, а не самосоединением в SQL: на полумиллионе строк планировщик
    уходил во вложенные циклы на десять минут."""
    by_day = {}
    for r in rows:
        by_day.setdefault(r[0], {})[r[1]] = math.log(r[2])
    days = sorted(by_day)
    out = []
    for prev, d in zip(days, days[1:]):
        a, b = by_day[d], by_day[prev]
        common = a.keys() & b.keys()
        g = np.array([a[k] - b[k] for k in common])
        out.append((d, prev, len(g), float(g.mean()), float(g.std(ddof=1)),
                    float((g < 0).mean()), float((g > 0).mean())))
    return out


def chain_index(steps):
    """Уровень из шагов: exp накопленной суммы средних логарифмов."""
    level, out = 0.0, []
    for d, prev, n, mean, sd, down, up in steps:
        level += mean
        out.append({"date": d.isoformat(), "prev": prev.isoformat(), "pairs": n,
                    "step_pct": round(100 * mean, 4),
                    "index": round(100 * math.exp(level), 3),
                    "share_down_pct": round(100 * down, 2),
                    "share_up_pct": round(100 * up, 2)})
    return out


# ---------------------------------------------------------------------
def _fill(values, transform=float):
    """Пропуск заполняется медианой, рядом флаг пропуска, как в hedonic."""
    present = np.array([transform(v) for v in values if v is not None])
    med = float(np.median(present)) if present.size else 0.0
    filled = np.array([transform(v) if v is not None else med for v in values])
    flag = np.array([v is None for v in values], dtype=float)
    return filled, flag


def _cats(values, base):
    levels = sorted({v for v in values if v is not None and v != base})
    arr = np.array(values, dtype=object)
    cols = [(arr == lv).astype(float) for lv in levels]
    names = [str(lv) for lv in levels]
    if any(v is None for v in values):
        cols.append(np.array([v is None for v in values], dtype=float))
        names.append("нет данных")
    return cols, names


def design(rows):
    """Признаки квартиры, те же по смыслу, что hedonic.controls, но
    столбцами numpy: строк здесь полмиллиона, и словари на каждую не
    помещаются в память макбука вместе с базой."""
    c = list(zip(*rows))
    (dates, offer, price, area, rooms, floor, floors, year, metro, park,
     mkad, apart, furn, wash, dish, cond, lift, photos, seller, material, district) = c
    cols, names = [], []
    def add(col, name):
        cols.append(np.asarray(col, dtype=float)); names.append(name)
    add(np.log(area), "log площадь")
    add(np.array(rooms) == 2, "двухкомнатная")
    floor, floors = np.array(floor), np.array(floors)
    add(floor == 1, "первый этаж")
    add(floor == floors, "последний этаж")
    add(np.log(np.maximum(floors, 1)), "log этажей")
    v, f = _fill(year, lambda y: float(CURRENT_YEAR - int(y)))
    add(v, "возраст дома"); add(f, "возраст дома: нет")
    v, f = _fill(metro)
    add(v, "минут до метро"); add(f, "метро: нет")
    add(np.log1p(np.array([p or 0.0 for p in park])), "log до парка")
    for col, name in ((mkad, "внутри МКАД"), (apart, "апартаменты"), (furn, "мебель"),
                      (wash, "стиральная"), (dish, "посудомойка"),
                      (cond, "кондиционер"), (lift, "лифт")):
        add(col, name)
    v, f = _fill(photos)
    add(v, "фотографий"); add(f, "фото: нет")
    for vals, base, prefix in ((seller, "realtor_based", "продавец"),
                               (material, "panel", "материал")):
        cc, nn = _cats(list(vals), base)
        for col, n in zip(cc, nn):
            add(col, f"{prefix}:{n}")
    y = np.log(np.array(price))
    return y, np.column_stack(cols), names, np.array(dates), np.array(offer), np.array(district)


def demean(a, groups):
    """Вычесть среднее по группе. Это поглощает фиксированные эффекты
    района (теорема Фриша-Во-Ловелла) без сотни фиктивных столбцов."""
    _, inv = np.unique(groups, return_inverse=True)
    cnt = np.bincount(inv).astype(float)
    if a.ndim == 1:
        return a - (np.bincount(inv, weights=a) / cnt)[inv]
    means = np.zeros((cnt.size, a.shape[1]))
    np.add.at(means, inv, a)
    return a - (means / cnt[:, None])[inv]


def cluster_ols(y, X, cluster, n_absorbed=0):
    """МНК с ошибками, кластерными по объявлению (CR1).

    n_absorbed это число поглощенных фиксированных эффектов: они съели
    степени свободы, хотя в X их нет."""
    xtx_inv = np.linalg.inv(X.T @ X)
    beta = xtx_inv @ (X.T @ y)
    u = y - X @ beta
    _, inv = np.unique(cluster, return_inverse=True)
    s = np.zeros((inv.max() + 1, X.shape[1]))
    np.add.at(s, inv, X * u[:, None])
    n, k, g = X.shape[0], X.shape[1] + n_absorbed, s.shape[0]
    vcov = xtx_inv @ (s.T @ s) @ xtx_inv * (g / (g - 1)) * ((n - 1) / (n - k))
    return beta, np.sqrt(np.diag(vcov))


def hedonic_index(rows):
    y, X, names, dates, offer, district = design(rows)
    days = sorted(set(dates))
    # Первый день база. Фиктивные по дате идут первыми, чтобы их
    # коэффициенты не зависели от того, какие признаки отпадут.
    D = np.column_stack([(dates == d).astype(float) for d in days[1:]])
    Xall = np.column_stack([D, X])
    keep = np.abs(np.linalg.qr(demean(Xall, district))[1].diagonal()) > 1e-9
    Xd = demean(Xall, district)[:, keep]
    beta, se = cluster_ols(demean(y, district), Xd, offer,
                           n_absorbed=len(set(district)))
    out = [{"date": days[0].isoformat(), "coef": 0.0, "se": 0.0, "index": 100.0,
            "lo95": 100.0, "hi95": 100.0, "n": int((dates == days[0]).sum())}]
    for i, d in enumerate(days[1:]):
        b, s = float(beta[i]), float(se[i])
        out.append({"date": d.isoformat(), "coef": round(b, 6), "se": round(s, 6),
                    "index": round(100 * math.exp(b), 3),
                    "lo95": round(100 * math.exp(b - 1.96 * s), 3),
                    "hi95": round(100 * math.exp(b + 1.96 * s), 3),
                    "n": int((dates == d).sum())})
    return out, {"rows": int(len(y)), "offers": int(len(set(offer))),
                 "districts": int(len(set(district))), "controls": len(names)}


def detectability(series, rosstat_abs_dlog):
    """Минимально различимое месячное изменение по дневному индексу.

    Дневной шум оценивается по первым разностям: если уровень за день
    меняется медленно, разность двух соседних дней это шум, умноженный
    на корень из двух. Автокорреляция отклонений от среднего уменьшает
    число независимых дней в месяце."""
    b = np.array([p["coef"] for p in series])
    sd_day = float(np.std(np.diff(b), ddof=1) / math.sqrt(2))
    dev = b - b.mean()
    rho = float(np.corrcoef(dev[:-1], dev[1:])[0, 1]) if len(b) > 3 else 0.0
    rho = max(rho, 0.0)
    n_eff = 30 * (1 - rho) / (1 + rho)
    se_month = sd_day / math.sqrt(n_eff)
    mde = 2.8 * math.sqrt(2) * se_month
    typical = float(np.median(rosstat_abs_dlog))
    return {"sd_day_pct": round(100 * sd_day, 3), "rho1": round(rho, 3),
            "n_eff_days": round(n_eff, 1), "se_month_pct": round(100 * se_month, 3),
            "mde_pct": round(100 * mde, 3),
            "rosstat_median_abs_mom_pct": round(100 * typical, 3),
            "distinguishes": bool(mde < typical)}


def daily_tail(series, max_gap=2):
    """Хвост ряда, где соседние дни не дальше max_gap суток: недельный
    шаг начала ряда трешек раздул бы дневной шум."""
    from datetime import date
    d = [date.fromisoformat(p["date"]) for p in series]
    i = len(d) - 1
    while i > 0 and (d[i] - d[i - 1]).days <= max_gap:
        i -= 1
    return series[i:]


def rosstat_abs_dlog(conn, months=36):
    with conn.cursor() as cur:
        cur.execute("""
            select rooms, price::float8 from core.rosstat_rent
            where region = 'Москва' order by rooms, period""")
        rows = cur.fetchall()
    out = []
    for r in (1, 2):
        p = np.array([v for k, v in rows if k == r])
        out.extend(np.abs(np.diff(np.log(p)))[-months:])
    return np.array(out)


def segment(rows, abs_dlog, tail_only=False) -> dict:
    chain = chain_index(chain_steps(rows))
    hed, meta = hedonic_index(rows)
    first, last = hed[0], hed[-1]
    total = {"from": first["date"], "to": last["date"],
             "change_pct": round(last["index"] - 100, 3),
             "lo95_pct": round(last["lo95"] - 100, 3),
             "hi95_pct": round(last["hi95"] - 100, 3)}
    total["flat"] = total["lo95_pct"] <= 0 <= total["hi95_pct"]
    tail = daily_tail(hed) if tail_only else hed
    det = detectability(tail, abs_dlog)
    if tail_only:
        det["days_used"] = len(tail)
        det["from"] = tail[0]["date"]
    return {
        "sample": meta,
        "covered_days": len(hed),
        "chain": chain,
        "chain_total_pct": round(chain[-1]["index"] - 100, 3) if chain else None,
        "hedonic": hed,
        "hedonic_total": total,
        "detectability": det,
    }


def main() -> int:
    from etl import db
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    rows = {}
    with db.connect() as conn:
        with conn.cursor() as cur:
            for key, rooms in SEGMENTS.items():
                cur.execute(obs_sql(rooms))
                rows[key] = cur.fetchall()
        abs_dlog = rosstat_abs_dlog(conn)
    payload = segment(rows["12"], abs_dlog)
    payload.update({"first_full_month_pair": ["2026-10", "2026-11"],
                    "rosstat_overlap_months": 0})
    three = segment(rows["3"], abs_dlog, tail_only=True)
    three["detectability"]["rosstat_series"] = "1-2 комнаты, у Росстата нет ряда трешек"
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "panel_index.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    (RESULTS / "panel_index_3.json").write_text(json.dumps(three, ensure_ascii=False, indent=1))
    for name, p in (("1-2к", payload), ("3к", three)):
        log.info("%s. Гедонический: %s; цепной: %s%%", name, p["hedonic_total"], p["chain_total_pct"])
        log.info("%s. Различимость: %s", name, p["detectability"])
    from src.index_figures import fig_panel_index, fig_panel_index_3
    fig_panel_index()
    fig_panel_index_3()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
