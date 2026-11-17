"""Данные публичной витрины: один файл site/data/site.json.

    make site-data

Только агрегаты. Объявления целиком, сырые снимки и выгрузки наружу не
идут: так требуют правила площадки и право изготовителя базы данных
(ст. 1334 ГК РФ). Каждая ячейка от порога проекта и выше: район от
десяти объявлений (как marts.district_level), точка кривой дожития от
пяти объявлений в группе риска, доля снизивших от пятидесяти. Срок жизни
и доля снизивших по району берутся с тем же порогом в пятьдесят, неделя
ряда цены района от десяти объявлений.

Витрина живет без машины сбора. Файл лежит в репозитории, сайт собирается
из него в GitHub Actions, и если выгрузка остановится, сайт покажет
последнюю дату данных и скажет, сколько дней назад она была.
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from matplotlib.path import Path as Ring

from etl import db
from etl.db import DISAPPEARED_AFTER_DAYS
from src import drops as drops_mod
from src import survival as surv
from src.panel_index import COVERED

log = logging.getLogger("site_data")

ROOT = Path(__file__).resolve().parents[1]
# На машине сбора код лежит копией без git, а наружу файлы уходят из
# отдельного клона репозитория. Поэтому оба пути можно переопределить.
OUT = Path(os.environ.get("SITE_OUT", ROOT / "site" / "data" / "site.json"))
RESULTS = Path(os.environ.get("RESULTS_DIR", ROOT / "results"))
# Индекс цен пересчитывается каждый день на машине сбора, а остальные
# результаты лежат в клоне. Отдельный путь не даёт смешать их в одном коммите.
INDEX_DIR = Path(os.environ.get("INDEX_DIR", RESULTS))
MIN_DISTRICT = 10
MIN_RISK = 5
MIN_SHARE = 50

SELLERS = {"homeowner": "собственник", "realtor_based": "агентство",
           "realtor_not_commerce": "частный агент"}


GEO = ROOT / "sql" / "geo" / "moscow_districts.geojson"
# Отбор объявлений тот же, что у marts.district_level.
SEGMENT = """from marts.offer_segment s left join pg_temp.remap r using (offer_id)
                    where s.source = 'cian' and s.price is not null and s.total_area > 0
                      and s.district is not null and s.rooms_count in (1, 2)
                      and not coalesce(s.is_duplicate, false)
                      and not coalesce(s.is_price_outlier, false)"""


def remap_new_moscow(cur) -> dict[str, str]:
    """Объявления районов без контура переносятся в контур по координатам
    дома. Так устроена Новая Москва: ЦИАН пишет прежние поселения
    (Сосенское, Московский), а в OpenStreetMap с 2023 года новые районы
    (Коммунарка, Филимонковский). Без переноса на карте пять серых пятен,
    а у шестнадцати районов с 5,5 тыс. объявлений нет места.

    Соответствие кладется во временную таблицу remap, и все расчеты по
    районам берут coalesce(r.district, s.district)."""
    geo = json.loads(GEO.read_text())
    rings: dict[str, list[Ring]] = {}
    for ft in geo["features"]:
        g, name = ft["geometry"], ft["properties"].get("name")
        if name:
            polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
            rings.setdefault(name, []).extend(Ring(poly[0]) for poly in polys)
    cur.execute("""select offer_id, lon, lat from marts.offer_segment
                    where source = 'cian' and rooms_count in (1, 2) and lat is not null
                      and district is not null and not (district = any(%s))""", (list(rings),))
    out = {}
    for oid, lon, lat in cur.fetchall():
        for name, rs in rings.items():
            if any(r.contains_point((lon, lat)) for r in rs):
                out[oid] = name
                break
    cur.execute("create temp table remap (offer_id text primary key, district text)")
    cur.executemany("insert into remap values (%s, %s)", list(out.items()))
    return out


def districts(cur) -> list[dict]:
    cur.execute(f"""with d as (
                        select coalesce(r.district, s.district) as district, count(*) as offers,
                               percentile_cont(0.5) within group (order by s.price / s.total_area) as ppm
                        {SEGMENT}
                        group by 1 having count(*) >= %s)
                    select d.district, d.offers, d.ppm::float8, (d.ppm / c.ppm)::float8
                      from d, (select percentile_cont(0.5) within group (order by ppm) as ppm from d) c
                     order by 1""", (MIN_DISTRICT,))
    return [{"district": d, "offers": n, "ppm": round(p), "vs_city": round(v, 3)}
            for d, n, p, v in cur.fetchall()]


def district_pets(cur) -> dict[str, dict]:
    """Четыре состояния животных по району. Отбор тот же, что у
    marts.district_level, поэтому знаменатель совпадает с числом
    объявлений района. «Не указано» остается остатком, а не запретом."""
    cur.execute(f"""select coalesce(r.district, s.district), count(*),
                          count(*) filter (where s.pets_state = 'allowed'),
                          count(*) filter (where s.pets_state = 'negotiable'),
                          count(*) filter (where s.pets_state = 'forbidden')
                    {SEGMENT}
                    group by 1""")
    return {d: {"pets_allowed_pct": round(100 * a / n, 1),
                "pets_negotiable_pct": round(100 * g / n, 1),
                "pets_forbidden_pct": round(100 * f / n, 1)}
            for d, n, a, g, f in cur.fetchall()}


def district_life(rows, cutoff) -> tuple[dict[str, int], int | None]:
    """Медиана срока жизни по району той же кривой, что и для города.
    Район берется от пятидесяти объявлений, и медиана засчитывается,
    только если в ее возрасте в группе риска не меньше пяти."""
    by: dict[str, list] = {}
    for r in rows:
        if r["district"]:
            by.setdefault(r["district"], []).append(r)
    out = {}
    for d, rs in by.items():
        if len(rs) < MIN_SHARE:
            continue
        curve = surv.km(surv.spans(rs, cutoff), max_age=60)
        med = next(((a, n) for a, n, _, s in curve if s <= 0.5 and n > 0), None)
        if med and med[1] >= MIN_RISK:
            out[d] = med[0]
    return out, surv.median_age(surv.km(surv.spans(rows, cutoff), max_age=60))


def district_cuts(rows) -> tuple[dict[str, dict], float | None]:
    """Доля снизивших цену по району. Только объявления, прожившие в
    наблюдении неделю и больше, как в разборе снижений: иначе район, где
    объявления висят дольше, выглядел бы районом, где чаще снижают."""
    rows = [r for r in rows
            if (r["last_seen_date"] - r["first_seen_date"]).days >= drops_mod.MIN_DAYS]
    by: dict[str, list[bool]] = {}
    for r in rows:
        if r["district"]:
            by.setdefault(r["district"], []).append(r["cuts"] > 0)
    out = {d: {"cut_pct": round(100 * sum(v) / len(v), 1), "cut_n": len(v)}
           for d, v in by.items() if len(v) >= MIN_SHARE}
    city = round(100 * sum(r["cuts"] > 0 for r in rows) / len(rows), 1) if rows else None
    return out, city


TREND_WEEKS = 8


def district_trend(cur, data_date: date) -> dict[str, dict]:
    """Медиана цены метра по неделям, отсчитанным назад от даты данных,
    чтобы последняя неделя была полной. Каждое объявление входит в неделю
    один раз, с последней ценой недели.

    Это медиана выдачи без поправки на состав: в районе на полсотни
    объявлений одна неделя с дорогими новостройками сдвигает ее на
    проценты. Поэтому рядом с изменением за четыре недели стоит грубая
    оценка шума: ошибка медианы по каждой неделе как 1,25 стандартной
    ошибки средней логарифма. Окна делят часть объявлений, так что оценка
    шума скорее завышена, чем занижена."""
    cur.execute("""
        with w as (
            select o.district, (%(d)s::date - ob.snapshot_date) / 7 as wk,
                   ob.price / o.total_area as ppm,
                   row_number() over (partition by ob.offer_id, (%(d)s::date - ob.snapshot_date) / 7
                                      order by ob.snapshot_date desc) as rn
              from core.offer_observation ob
              join (select s.offer_id, s.source, s.total_area, s.rooms_count, s.is_duplicate,
                           s.is_price_outlier, coalesce(r.district, s.district) as district
                      from marts.offer_segment s left join pg_temp.remap r using (offer_id)) o
                using (source, offer_id)
             where ob.source = 'cian' and not ob.is_daily and ob.price > 0
               and o.total_area > 0 and o.rooms_count in (1, 2) and o.district is not null
               and not coalesce(o.is_duplicate, false) and not coalesce(o.is_price_outlier, false)
               and ob.snapshot_date > %(d)s::date - 7 * %(k)s and ob.snapshot_date <= %(d)s::date)
        select district, wk, count(*),
               percentile_cont(0.5) within group (order by ppm)::float8, stddev(ln(ppm))::float8
          from w where rn = 1 and ppm between 400 and 15000
         group by 1, 2 having count(*) >= %(n)s""",
                {"d": data_date, "k": TREND_WEEKS, "n": MIN_DISTRICT})
    by: dict[str, dict[int, tuple]] = {}
    for d, wk, n, med, sd in cur.fetchall():
        by.setdefault(d, {})[wk] = (n, med, sd or 0.0)
    out = {}
    for d, weeks in by.items():
        item = {"weeks": [[(data_date - timedelta(days=7 * wk)).isoformat(), n, round(med)]
                          for wk, (n, med, _) in sorted(weeks.items(), reverse=True)]}
        if 0 in weeks and 4 in weeks:
            (n0, m0, s0), (n4, m4, s4) = weeks[0], weeks[4]
            diff = math.log(m0 / m4)
            se = math.hypot(1.25 * s0 / math.sqrt(n0), 1.25 * s4 / math.sqrt(n4))
            item["chg4_pct"] = round(100 * (math.exp(diff) - 1), 1)
            item["chg4_noise"] = abs(diff) <= 1.96 * se
        out[d] = item
    return out


def district_table(cur, data_date: date, life_rows, cutoff, drop_rows) -> tuple[list[dict], dict]:
    """Район со всеми показателями карты. Цена метра задает список
    районов (от десяти объявлений), остальные показатели приклеиваются,
    если прошли свой порог, иначе их нет и район на этом слое серый."""
    base = districts(cur)
    pets = district_pets(cur)
    life, city_life = district_life(life_rows, cutoff)
    cuts, city_cut = district_cuts(drop_rows)
    trend = district_trend(cur, data_date)
    for r in base:
        name = r["district"]
        r.update(pets.get(name, {}))
        if name in life:
            r["life_days"] = life[name]
        r.update(cuts.get(name, {}))
        r.update(trend.get(name, {}))
    cur.execute(f"""select percentile_cont(0.5) within group (order by s.price / s.total_area),
                          avg((s.pets_state = 'allowed')::int)
                    {SEGMENT}""")
    ppm, pets_share = cur.fetchone()
    city = {"ppm": round(ppm), "pets_allowed_pct": round(100 * float(pets_share), 1),
            "life_days": city_life, "cut_pct": city_cut}
    return base, city


def today(cur, data_date: date) -> dict:
    """Плашка «сегодня»: выдача последнего собранного дня и движение за
    неделю. Ушедшее засчитывается после трех собранных дней отсутствия,
    поэтому неделя уходов кончается на три дня раньше даты данных, и
    новые считаются за ту же неделю, чтобы их можно было сравнить."""
    cur.execute("""select rooms_count, offers, med_price::float8 from marts.panel_day
                    where snapshot_date = %s""", (data_date,))
    day = {r: (n, p) for r, n, p in cur.fetchall()}
    week_end = data_date - timedelta(days=DISAPPEARED_AFTER_DAYS)
    week_start = week_end - timedelta(days=6)
    cur.execute("""select count(*) filter (where first_seen_date = %(d)s),
                          count(*) filter (where first_seen_date between %(a)s and %(b)s),
                          count(*) filter (where disappeared_date between %(a)s and %(b)s)
                     from core.offer
                    where source = 'cian' and rooms_count in (1, 2)""",
                {"d": data_date, "a": week_start, "b": week_end})
    new_day, new_week, gone_week = cur.fetchone()
    cur.execute("""select count(*), count(*) filter (where ob.pets_state = 'allowed')
                     from core.offer_observation ob
                     join core.offer o using (source, offer_id)
                    where ob.source = 'cian' and ob.snapshot_date = %s
                      and o.rooms_count in (1, 2) and not ob.is_daily""", (data_date,))
    seen, pets = cur.fetchone()
    return {"offers": day.get(0, (None,))[0],
            "median_1": round(day[1][1]) if 1 in day else None,
            "median_2": round(day[2][1]) if 2 in day else None,
            "new_day": new_day,
            "week_start": week_start.isoformat(), "week_end": week_end.isoformat(),
            "new_week": new_week, "gone_week": gone_week,
            "pets_allowed_pct": round(100 * pets / seen, 1) if seen else None}


def survival_curve(rows, cutoff) -> list[dict]:
    curve = surv.km(surv.spans(rows, cutoff), max_age=60)
    return [{"age": a, "at_risk": n, "surv": round(s, 4)}
            for a, n, _, s in curve if n >= MIN_RISK]


def drop_shares(rows) -> list[dict]:
    """По продавцам, только объявления, видимые неделю и больше, как в
    разборе снижений и на слое карты (с 17.11.2026). Раньше здесь были
    все, и столбцы расходились с картой и с разрывом в README."""
    groups: dict[str, list[bool]] = {}
    for r in rows:
        if (r["last_seen_date"] - r["first_seen_date"]).days < drops_mod.MIN_DAYS:
            continue
        groups.setdefault(SELLERS.get(r["seller_type"], "не указан"), []).append(r["cuts"] > 0)
    return sorted([{"seller": k, "offers": len(v), "share_pct": round(100 * sum(v) / len(v), 1)}
                   for k, v in groups.items() if len(v) >= MIN_SHARE],
                  key=lambda x: -x["share_pct"])


def exit_prices(cur) -> dict[str, list]:
    """Последняя цена ушедших объявлений: дневная средняя и скользящая за
    семь календарных дней.

    День ухода последний, когда объявление видели в выдаче. По дням
    средняя гуляет на свою полосу, поэтому линия скользящая: окно из семи
    дней по день включительно, объявления окна сложены вместе, а не
    усреднены средние дней. Средняя и ошибка окна собираются из дневных
    точно: сумма цен равна n * avg, разброс восстанавливается из ошибки
    дня. Точка окна показывается, если в нем не меньше четырех дней уходов:
    после пропущенных дней сбора и в начале ряда окно иначе держалось бы
    на одном-двух днях."""
    cur.execute("""select rooms_count, exit_date, exits, avg_price, avg_price_se
                     from marts.exit_day
                    where rooms_count in (1, 2) and exits >= %s
                    order by rooms_count, exit_date""", (MIN_DISTRICT,))
    days: dict[int, list] = {}
    for r, d, n, avg, se in cur.fetchall():
        days.setdefault(r, []).append((d, n, avg, se))
    out: dict[str, list] = {"1": [], "2": []}
    for r, rows in days.items():
        for d, n_day, avg_day, _ in rows:
            win = [x for x in rows if 0 <= (d - x[0]).days < 7]
            point = [d.isoformat(), n_day, avg_day, None, None]
            if len(win) >= 4:
                n = sum(x[1] for x in win)
                mean = sum(x[1] * x[2] for x in win) / n
                # Квадраты отклонений от средней окна: внутри дня плюс между днями.
                ss = sum((x[1] - 1) * x[1] * x[3] ** 2 + x[1] * (x[2] - mean) ** 2 for x in win)
                point[3:] = [round(mean), round((ss / (n - 1) / n) ** 0.5)]
            out[str(r)].append(point)
    return out


def rosstat_history(cur) -> dict[str, list]:
    cur.execute("""select rooms, to_char(period, 'YYYY-MM'), price::float8
                   from core.rosstat_rent where region = 'Москва' and period >= '2021-01-01'
                   order by rooms, period""")
    out: dict[str, list] = {"1": [], "2": []}
    for r, m, p in cur.fetchall():
        out[str(r)].append([m, round(p)])
    return out


def quality(cur) -> dict:
    """Качество сбора по дням. Здесь только счетчики прогонов и корзин,
    объявлений в ячейках нет, поэтому порог ячейки не нужен."""
    cur.execute("""select snapshot_date, is_covered, runs_failed, completeness_pct::float8,
                          underfilled_pct::float8, offers_12, got_12, claimed_12,
                          seg_completeness_pct::float8, seg_1_pct::float8, seg_2_pct::float8
                     from marts.collection_quality order by snapshot_date""")
    days = [{"date": d.isoformat(), "covered": c, "runs_failed": f,
             "completeness_pct": p, "underfilled_pct": u, "offers": n,
             "got": g, "claimed": cl, "seg_pct": s, "seg_1_pct": s1, "seg_2_pct": s2}
            for d, c, f, p, u, n, g, cl, s, s1, s2 in cur.fetchall()]
    # Полнота дня по сегменту, как в README: медиана по всем дням, где
    # заявленное известно, включая прерванные (с 10.11.2026).
    seg = sorted(d["seg_pct"] for d in days if d["seg_pct"] is not None)
    covered = [d for d in days if d["covered"]]
    pct = sorted(d["completeness_pct"] for d in covered if d["completeness_pct"] is not None)
    und = sorted(d["underfilled_pct"] for d in covered if d["underfilled_pct"] is not None)
    # Насколько велик недобор там, где он есть: заявленное площадкой
    # число неточное, и мелкий недобор от него не отличить.
    cur.execute("""select percentile_disc(0.5) within group (order by b.claimed_count - b.collected_count)
                     from raw.collection_run r join raw.bucket b using (run_id)
                    where r.source = 'cian' and r.status = 'ok' and not b.was_split
                      and b.error is null and b.collected_count < b.claimed_count""")
    short = cur.fetchone()[0]
    return {"days": days,
            "days_total": len(days),
            "days_covered": len(covered),
            "missed": [d["date"] for d in days if not d["covered"]],
            "completeness_median": pct[len(pct) // 2] if pct else None,
            "completeness_min": pct[0] if pct else None,
            "underfilled_median": und[len(und) // 2] if und else None,
            "seg_median": seg[(len(seg) - 1) // 2] if seg else None,
            "seg_days": len(seg),
            "seg_days_ge95": sum(v >= 95 for v in seg),
            "shortfall_median": short}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(f"select max(snapshot_date) from ({COVERED}) c")
        data_date: date = cur.fetchone()[0]
        cur.execute("select count(distinct snapshot_date) from core.offer_observation "
                    "where source = 'cian'")
        days = cur.fetchone()[0]
        # Строки дожития и снижений нужны и городу, и районам, а грузятся
        # они дольше всего остального, поэтому читаются один раз.
        life_rows, cutoff = surv.load(conn, [1, 2])
        drop_rows = drops_mod.load(conn, [1, 2])
        remap = remap_new_moscow(cur)
        for r in (*life_rows, *drop_rows):
            r["district"] = remap.get(r["offer_id"], r["district"])
        district_rows, city = district_table(cur, data_date, life_rows, cutoff, drop_rows)
        payload = {
            "data_date": data_date.isoformat(),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
            "panel_days": days,
            "today": today(cur, data_date),
            "districts": district_rows,
            "city": city,
            "survival": survival_curve(life_rows, cutoff),
            "drops": drop_shares(drop_rows),
            "exit_prices": exit_prices(cur),
            "rosstat": rosstat_history(cur),
            "quality": quality(cur),
        }
    fc = json.loads((RESULTS / "forecast.json").read_text())
    payload["forecast"] = {"chosen": fc["decision"]["chosen"],
                           "last_month": fc["last_month"],
                           "series": {k: fc["forecast"][f"nominal_{k}"] for k in ("1", "2")}}
    ix = json.loads((INDEX_DIR / "panel_index.json").read_text())
    payload["panel_index"] = {
        "hedonic": [[p["date"], p["index"], p["lo95"], p["hi95"]] for p in ix["hedonic"]],
        "chain": [[ix["hedonic"][0]["date"], 100.0]] + [[p["date"], p["index"]] for p in ix["chain"]],
        "total": {k: ix["hedonic_total"][k] for k in ("change_pct", "lo95_pct", "hi95_pct", "flat")},
    }
    # Трешки отдельным рядом: до 12.10 точки раз в неделю, у Росстата
    # ряда трешек нет, поэтому прогноза для них на витрине нет.
    ix3_path = INDEX_DIR / "panel_index_3.json"
    if ix3_path.exists():
        ix3 = json.loads(ix3_path.read_text())
        payload["panel_index"]["hedonic3"] = [[p["date"], p["index"], p["lo95"], p["hi95"]]
                                              for p in ix3["hedonic"]]
    pm = json.loads((RESULTS / "price_model.json").read_text())
    payload["model_drift"] = [{"week": w["week_start"], "n": w["n"], "mape_pct": w["mape_pct"]}
                              for w in pm["drift"]["weeks"]]
    payload["model"] = {"version": pm["version"],
                        "mape_pct": pm["models"]["catboost"]["test"]["mape_pct"],
                        "coverage80": pm["models"]["catboost"]["coverage80"],
                        "threshold_pct": pm["drift"]["threshold_pct"]}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    log.info("%s: данные на %s, районов %d, точек дожития %d",
             OUT, data_date, len(payload["districts"]), len(payload["survival"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
