"""Кто снижает цену и через сколько дней.

Третий вопрос проекта. Нужен он не из любопытства: если торг возможен не
со всеми одинаково, то знать, у кого он вероятнее, — это прямая экономия
при выборе, с кем вообще разговаривать.

Ловушка, в которую легко попасть
--------------------------------

Напрашивающийся вопрос — снижают ли те, кто изначально просил выше
рынка. Напрашивающийся способ посчитать — взять отношение цены
объявления к медиане похожих и посмотреть долю снизивших по пятинам.

Так делать нельзя, и ответ получается не просто неточный, а с
перевёрнутым знаком. В витрине лежит ПОСЛЕДНЯЯ цена. Объявление, которое
уже снизило цену, из-за этого выглядит дешёвым относительно рынка — и
попадает в пятину «дешевле всех» именно потому, что снижало. Получается
«дешёвые снижают чаще», причём с убедительным наклоном.

Лечится тем, что признак берётся предопределённым: не последняя цена, а
ПЕРВАЯ, которую мы застали. Обе версии считаются и печатаются рядом —
разница между ними и есть весь размер ловушки.

Экспозиция
----------

Объявление, которое висит дольше, имеет больше шансов снизить цену хотя
бы раз. Если это не выровнять, ответом на «кто снижает» будет «кто дольше
висит». Поэтому доли считаются по объявлениям, прожившим у нас в
наблюдении не меньше MIN_DAYS дней, а срок до снижения — Каплан-Мейером,
который с разной длиной наблюдения умеет обращаться сам.

Снимок
------

Числа разбора живут в results/drops.json с датой данных. Оттуда их берут
README и methods/drops.md (make readme-numbers), обложка README, превью
ссылки и обе картинки разбора. До 18.11.2026 README и methods держали
числа 04.10, а обложка считала разрыв по витрине на свежих данных, и
рядом стояли +9,0 и +10,6 п.п. одного и того же вопроса. Пересчет
осознанный, как у модельных чисел: свежие данные меняли здесь вывод, а
не только число (наклон по первой цене).

Запуск: `make drops-analysis` или `python -m src.drops`.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
from datetime import date
from pathlib import Path

import numpy as np

from etl import db
from src.hedonic import _dummies, _numeric_with_missing, fixed_effects
from src.linreg import fit
from src.survival import km

log = logging.getLogger("drops")

RESULTS = Path(__file__).resolve().parents[1] / "results"
OUT = RESULTS / "drops.json"

# Подписи как на витрине (src/site_data.py SELLERS), чтобы одно и то же
# называлось одинаково на обложке, в README и на сайте.
LABELS = {"homeowner": "собственник", "realtor_based": "агентство",
          "realtor_not_commerce": "частный агент",
          "allowed": "разрешено", "forbidden": "запрещено",
          "negotiable": "по договоренности", "unknown": "не указано"}

# Сколько дней объявление должно у нас прожить, чтобы попасть в доли.
# Семь, а не три: за три дня снижают единицы, и группы различались бы
# шумом. И не десять: тогда из выборки выпадает треть сегмента.
MIN_DAYS = 7

SQL = """
with seq as (
    select offer_id, snapshot_date, price,
           lag(price) over (partition by offer_id order by snapshot_date) as prev
    from core.offer_observation
    where source = 'cian' and price is not null and price > 0
),
firsts as (
    select distinct on (offer_id) offer_id, price as first_price
    from core.offer_observation
    where source = 'cian' and price is not null and price > 0
    order by offer_id, snapshot_date
),
moves as (
    select offer_id,
           min(snapshot_date) filter (where price < prev)        as first_cut_date,
           count(*) filter (where price < prev)                  as cuts,
           count(*) filter (where price > prev)                  as raises,
           min(100.0 * (price - prev) / prev)
               filter (where price < prev)                       as deepest_pct
    from seq
    where prev is not null and price <> prev
    group by offer_id
)
select s.offer_id, s.district, s.rooms_count, s.total_area, s.photos_count,
       s.seller_type, s.pets_state, s.build_year, s.metro_real_walk_min,
       s.med_price, s.ppm_vs_city,
       s.price                     as last_price,
       f.first_price,
       s.first_seen_date, s.last_seen_date,
       o.published_at::date        as published,
       m.first_cut_date,
       coalesce(m.cuts, 0)         as cuts,
       coalesce(m.raises, 0)       as raises,
       m.deepest_pct
from marts.offer_scored s
join firsts f on f.offer_id = s.offer_id
join core.offer o on o.source = s.source and o.offer_id = s.offer_id
left join moves m on m.offer_id = s.offer_id
where s.source = 'cian'
  and s.rooms_count = any(%s)
  and coalesce(s.is_duplicate, false) = false
  and coalesce(s.is_price_outlier, false) = false
  and s.med_price > 0
"""


def load(conn, rooms: list[int]) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(SQL, (rooms,))
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------
def share_table(rows, key, title, order_by_share=True) -> list[dict]:
    """Доля снизивших цену в разрезе одного признака."""
    buckets: dict[str, list] = {}
    for r in rows:
        buckets.setdefault(key(r), []).append(r)
    items = [(k, len(v), 100 * sum(1 for x in v if x["cut"]) / len(v))
             for k, v in buckets.items() if len(v) >= 50]
    items.sort(key=lambda t: -t[2] if order_by_share else t[0])
    print(f"\n  {title}")
    print(f"    {'значение':<24} {'объявлений':>11} {'снизили':>9}")
    for name, n, share in items:
        print(f"    {name:<24} {n:>11} {share:>8.1f}%")
    return [{"value": LABELS.get(name, name), "offers": n, "share_pct": round(share, 1)}
            for name, n, share in items]


def quintile_of(rows, value) -> dict[int, int]:
    """Номер пятины (0-4) по признаку для каждого объявления, id(r) -> пятина.
    Тот же разрез, что в таблице пятин, чтобы модель и таблица говорили об
    одних и тех же группах."""
    vals = sorted(((value(r), r) for r in rows if value(r) is not None),
                  key=lambda t: t[0])
    size = len(vals) // 5
    return {id(r): min(i // size, 4) for i, (_, r) in enumerate(vals)}


def quintiles(rows, value, title) -> list[dict]:
    """Доля снизивших по пятинам числового признака."""
    # Ключ сортировки задан явно: при равных значениях Python иначе
    # полезет сравнивать сами словари и упадёт.
    vals = sorted(((value(r), r) for r in rows if value(r) is not None),
                  key=lambda t: t[0])
    if len(vals) < 500:
        return []
    size = len(vals) // 5
    print(f"\n  {title}")
    print(f"    {'пятина':<8} {'от':>7} {'до':>7} {'объявлений':>11} {'снизили':>9}")
    out = []
    for i in range(5):
        chunk = vals[i * size: (i + 1) * size if i < 4 else len(vals)]
        share = 100 * sum(1 for _, r in chunk if r["cut"]) / len(chunk)
        print(f"    {i + 1:<8} {chunk[0][0]:>7.2f} {chunk[-1][0]:>7.2f} "
              f"{len(chunk):>11} {share:>8.1f}%")
        out.append({"quintile": i + 1, "from": round(chunk[0][0], 3),
                    "to": round(chunk[-1][0], 3), "offers": len(chunk),
                    "share_pct": round(share, 1)})
    return out


def lpm_report(rows) -> dict:
    """Линейная вероятностная модель с фиксированными эффектами района.

    Зависимая переменная — ноль или единица, поэтому коэффициент читается
    в процентных пунктах вероятности, а не в процентах. Обычный МНК на
    двоичном исходе даёт смещённые стандартные ошибки по построению,
    отсюда ошибки Уайта и кластеризация по району: объявления одного
    района не независимы.

    Модель здесь нужна не ради предсказания, а ради одного вопроса — не
    подменяет ли «агентство» что-то другое. Если разрыв между агентством
    и собственником держится при одинаковых районе, комнатности, площади
    и сроке висения, значит дело действительно в том, кто продаёт.
    """
    y = np.array([1.0 if r["cut"] else 0.0 for r in rows])
    cols = [np.ones(len(rows))]
    names = ["константа"]

    c, n = _dummies([r["seller_type"] for r in rows],
                    base="homeowner", prefix="продавец")
    cols += c; names += n

    cols.append(np.array([float(r["days_seen"]) for r in rows]))
    names.append("дней в наблюдении")
    cols.append(np.array([math.log(float(r["total_area"])) for r in rows]))
    names.append("лог площади")
    c, n = _dummies([r["rooms_count"] for r in rows], base=1, prefix="комнат")
    cols += c; names += n
    c, n = _dummies([r["pets_state"] for r in rows], base="unknown", prefix="животные")
    cols += c; names += n
    c, n = _numeric_with_missing([r["photos_count"] for r in rows], "фотографий")
    cols += c; names += n
    c, n = _numeric_with_missing([r["metro_real_walk_min"] for r in rows], "до метро")
    cols += c; names += n
    # Первая цена к похожим пятинами, а не числом. Числом ее держали до
    # 18.11.2026, и на свежей панели хвост до отношения 1300 (цена с
    # лишними нулями) делал коэффициент бессмысленным: «+0,1 п.п. на
    # единицу отношения». Пятины к хвосту равнодушны и читаются так же,
    # как таблица ловушки.
    q = quintile_of(rows, lambda r: r["first_to_market"])
    c, n = _dummies([f"пятина {q[id(r)] + 1}" for r in rows],
                    base="пятина 1", prefix="первая цена")
    cols += c; names += n

    districts = [r["district"] or "не указан" for r in rows]
    c, n = fixed_effects(districts, "район")
    cols += c; names += n

    f = fit(y, np.column_stack(cols), names, cluster=np.array(districts))
    print(f"\n  наблюдений {f.n}, признаков {f.k}, R² {f.r2:.3f}, "
          f"база — собственник")
    print(f"    {'признак':<26} {'п.п.':>8} {'ошибка':>8} {'t':>7}  {'95% интервал':>18}")
    terms = {}
    for name in names:
        if not (name.startswith("продавец") or name.startswith("первая цена")
                or name.startswith("животные") or name.startswith("комнат")):
            continue
        if name not in f.names:
            continue
        b, se = f.get(name)
        t = b / se if se else float("nan")
        print(f"    {name:<26} {100 * b:>+8.1f} {100 * se:>8.1f} {t:>7.1f}  "
              f"[{100 * (b - 1.96 * se):>+7.1f}; {100 * (b + 1.96 * se):>+7.1f}]")
        terms[name] = {"pp": round(100 * b, 2), "se": round(100 * se, 2),
                       "t": round(t, 1), "lo95": round(100 * (b - 1.96 * se), 2),
                       "hi95": round(100 * (b + 1.96 * se), 2)}
    return {"n": f.n, "k": f.k, "r2": round(f.r2, 3), "terms": terms}


# ---------------------------------------------------------------------
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Кто снижает цену и через сколько дней")
    parser.add_argument("--rooms", default="1,2")
    args = parser.parse_args()
    rooms = [int(x) for x in args.rooms.split(",")]

    with db.connect() as conn:
        rows = load(conn, rooms)

    for r in rows:
        r["cut"] = r["cuts"] > 0
        r["days_seen"] = (r["last_seen_date"] - r["first_seen_date"]).days
        r["first_to_market"] = float(r["first_price"]) / float(r["med_price"])
        r["last_to_market"] = float(r["last_price"]) / float(r["med_price"])

    snapshot = max(r["last_seen_date"] for r in rows)
    total_cuts = sum(r["cuts"] for r in rows)
    total_raises = sum(r["raises"] for r in rows)
    print(f"Комнатность {args.rooms}. Объявлений {len(rows)}.")
    print(f"Снижений {total_cuts}, повышений {total_raises} — "
          f"{total_cuts / max(total_raises, 1):.1f} к одному.")
    # Глубина со знаком минус, поэтому четверть самых глубоких внизу списка.
    deep = sorted(float(r["deepest_pct"]) for r in rows if r["deepest_pct"] is not None)
    if deep:
        print(f"Глубина снижения: медиана {deep[len(deep) // 2]:.1f}%, "
              f"четверть глубже {deep[len(deep) // 4]:.1f}%.")

    long = [r for r in rows if r["days_seen"] >= MIN_DAYS]
    print(f"\nДальше — только объявления, прожившие в наблюдении "
          f"{MIN_DAYS} дней и больше: таких {len(long)}.")
    print("Экспозиция выровнена, иначе ответом было бы «снижает тот, кто дольше висит».")

    print("\n" + "=" * 78)
    print("ЛОВУШКА. Один и тот же вопрос, два способа посчитать.")
    print("=" * 78)
    print("\n  Вопрос: чаще ли снижают те, кто просил выше рынка?")
    q_last = quintiles(long, lambda r: r["last_to_market"],
                       "НЕВЕРНО — по ПОСЛЕДНЕЙ цене (она уже учла снижение)")
    q_first = quintiles(long, lambda r: r["first_to_market"],
                        "ВЕРНО — по ПЕРВОЙ увиденной цене")
    print("\n  Первый расчёт завышает долю снизивших в дешёвых пятинах:")
    print("  объявление попадает туда именно потому, что снизило цену.")
    print("  Сравнивать надо наклоны двух таблиц, а не одну из них.")

    print("\n" + "=" * 78)
    print("КТО СНИЖАЕТ")
    print("=" * 78)
    shares = {
        "seller": share_table(long, lambda r: r["seller_type"] or "не указан",
                              "По типу продавца"),
        "rooms": share_table(long, lambda r: f'{r["rooms_count"]} комн.', "По комнатности"),
        "pets": share_table(long, lambda r: r["pets_state"] or "нет данных", "По животным"),
        "district": share_table(long, lambda r: ("дорогой" if (r["ppm_vs_city"] or 1) >= 1.1
                                                 else "дешевый" if (r["ppm_vs_city"] or 1) < 0.9
                                                 else "средний"), "По уровню района"),
    }

    print("\n" + "=" * 78)
    print("ВЫДЕРЖИВАЕТ ЛИ РАЗРЫВ ПОПРАВКУ НА ВСЁ ОСТАЛЬНОЕ")
    print("=" * 78)
    lpm = lpm_report([r for r in long if r["total_area"] and r["seller_type"]])

    print("\n" + "=" * 78)
    print("ЧЕРЕЗ СКОЛЬКО ДНЕЙ")
    print("=" * 78)
    # Возраст считается от публикации, а наблюдать мы начали позже —
    # то же позднее вхождение, что и в сроке жизни, и та же машинка.
    # Здесь берутся ВСЕ объявления, а не только долгожители: Каплан-Мейер
    # с разной длиной наблюдения справляется сам, а отбор по сроку жизни
    # сместил бы ответ в сторону тех, кто успел снизить.
    sp = []
    for r in rows:
        entry = (r["first_seen_date"] - r["published"]).days
        end = r["first_cut_date"] or r["last_seen_date"]
        exit_ = (end - r["published"]).days
        if entry < 0 or exit_ < entry:
            continue
        sp.append((entry, exit_, r["first_cut_date"] is not None))
    curve = km(sp, max_age=60)
    print(f"\n  {'возраст':>8} {'в риске':>9} {'снизили':>9} {'ещё не снижали':>16}")
    for age, n, d, s in curve:
        if age in (3, 7, 14, 21, 30, 45, 60) and n > 0:
            print(f"  {age:>8} {n:>9} {d:>9} {100 * s:>15.1f}%")
    half = next((a for a, n, d, sv in curve if sv <= 0.5 and n > 0), None)
    ages = sorted({3, 7, 14, 21, 30, 45} | ({half} if half else set()))
    if half:
        print(f"\n  Половина объявлений доходит до первого снижения к {half}-му дню.")
    print("  Читать это надо так: среди тех, кто ДОЖИЛ, снижение — обычное дело.")
    print("  Объявление, снятое на третий день, в эту статистику не попадает:")
    print("  оно цензурировано, а не «не снизило».")

    print("\nЧто из этого нельзя говорить: что агентство «готово торговаться».")
    print("Мы видим изменение цены в объявлении, а не разговор с арендатором.")

    seller = {r["value"]: r["share_pct"] for r in shares["seller"]}
    agency = lpm["terms"]["продавец:realtor_based"]
    payload = {
        "data_date": snapshot.isoformat(),
        "computed_on": date.today().isoformat(),
        "rooms": rooms,
        "panel_days": (snapshot - min(r["first_seen_date"] for r in rows)).days + 1,
        "offers": len(rows),
        "cuts": total_cuts, "raises": total_raises,
        "depth_median_pct": round(deep[len(deep) // 2], 1) if deep else None,
        "depth_q25_pct": round(deep[len(deep) // 4], 1) if deep else None,
        "min_days": MIN_DAYS, "long_offers": len(long),
        "shares": shares,
        # Главное число вопроса. Разрыв с поправками, а сырой рядом: так
        # видно, что поправки его почти не двигают.
        "gap": {"adjusted_pp": agency["pp"], "lo95": agency["lo95"], "hi95": agency["hi95"],
                "raw_pp": round(seller["агентство"] - seller["собственник"], 1)},
        "lpm": lpm,
        "quintiles": {"last": q_last, "first": q_first},
        "first_cut": {"median_day": half,
                      "curve": [{"age": a, "at_risk": n, "not_cut_pct": round(100 * s, 1)}
                                for a, n, _, s in curve if a in ages and n > 0]},
    }
    RESULTS.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"\n{OUT}")
    from src import figures
    figures.fig_who_cuts(payload)
    figures.fig_trap(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
