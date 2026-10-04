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

Запуск: `make drops-analysis` или `python -m src.drops`.
"""
from __future__ import annotations

import argparse
import logging
import math
import numpy as np

from etl import db
from src.hedonic import _dummies, _numeric_with_missing, fixed_effects
from src.linreg import fit
from src.survival import km

log = logging.getLogger("drops")

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
def share_table(rows, key, title, order_by_share=True) -> None:
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


def quintiles(rows, value, title) -> None:
    """Доля снизивших по пятинам числового признака."""
    # Ключ сортировки задан явно: при равных значениях Python иначе
    # полезет сравнивать сами словари и упадёт.
    vals = sorted(((value(r), r) for r in rows if value(r) is not None),
                  key=lambda t: t[0])
    if len(vals) < 500:
        return
    size = len(vals) // 5
    print(f"\n  {title}")
    print(f"    {'пятина':<8} {'от':>7} {'до':>7} {'объявлений':>11} {'снизили':>9}")
    for i in range(5):
        chunk = vals[i * size: (i + 1) * size if i < 4 else len(vals)]
        share = 100 * sum(1 for _, r in chunk if r["cut"]) / len(chunk)
        print(f"    {i + 1:<8} {chunk[0][0]:>7.2f} {chunk[-1][0]:>7.2f} "
              f"{len(chunk):>11} {share:>8.1f}%")


def lpm_report(rows) -> None:
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
    cols.append(np.array([float(r["first_to_market"]) for r in rows]))
    names.append("первая цена к похожим")

    districts = [r["district"] or "не указан" for r in rows]
    c, n = fixed_effects(districts, "район")
    cols += c; names += n

    f = fit(y, np.column_stack(cols), names, cluster=np.array(districts))
    print(f"\n  наблюдений {f.n}, признаков {f.k}, R² {f.r2:.3f}, "
          f"база — собственник")
    print(f"    {'признак':<26} {'п.п.':>8} {'ошибка':>8} {'t':>7}  {'95% интервал':>18}")
    for name in names:
        if not (name.startswith("продавец") or name == "первая цена к похожим"
                or name.startswith("животные") or name.startswith("комнат")):
            continue
        if name not in f.names:
            continue
        b, se = f.get(name)
        t = b / se if se else float("nan")
        print(f"    {name:<26} {100 * b:>+8.1f} {100 * se:>8.1f} {t:>7.1f}  "
              f"[{100 * (b - 1.96 * se):>+7.1f}; {100 * (b + 1.96 * se):>+7.1f}]")


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

    total_cuts = sum(r["cuts"] for r in rows)
    total_raises = sum(r["raises"] for r in rows)
    print(f"Комнатность {args.rooms}. Объявлений {len(rows)}.")
    print(f"Снижений {total_cuts}, повышений {total_raises} — "
          f"{total_cuts / max(total_raises, 1):.1f} к одному.")
    deep = [float(r["deepest_pct"]) for r in rows if r["deepest_pct"] is not None]
    if deep:
        deep.sort()
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
    quintiles(long, lambda r: r["last_to_market"],
              "НЕВЕРНО — по ПОСЛЕДНЕЙ цене (она уже учла снижение)")
    quintiles(long, lambda r: r["first_to_market"],
              "ВЕРНО — по ПЕРВОЙ увиденной цене")
    print("\n  Первый расчёт даёт сильный наклон и неверный знак: снизившие")
    print("  объявления из-за самого снижения попадают в дешёвые пятины.")
    print("  Во втором наклон почти исчезает. Это не слабый результат, а")
    print("  правильный: цена относительно рынка почти не говорит о торге.")

    print("\n" + "=" * 78)
    print("КТО СНИЖАЕТ")
    print("=" * 78)
    share_table(long, lambda r: r["seller_type"] or "не указан", "По типу продавца")
    share_table(long, lambda r: f'{r["rooms_count"]} комн.', "По комнатности")
    share_table(long, lambda r: r["pets_state"] or "нет данных", "По животным")
    share_table(long, lambda r: ("дорогой район" if (r["ppm_vs_city"] or 1) >= 1.1
                                 else "дешёвый район" if (r["ppm_vs_city"] or 1) < 0.9
                                 else "средний район"), "По уровню района")

    print("\n" + "=" * 78)
    print("ВЫДЕРЖИВАЕТ ЛИ РАЗРЫВ ПОПРАВКУ НА ВСЁ ОСТАЛЬНОЕ")
    print("=" * 78)
    lpm_report([r for r in long if r["total_area"] and r["seller_type"]])

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
    if half:
        print(f"\n  Половина объявлений доходит до первого снижения к {half}-му дню.")
    print("  Читать это надо так: среди тех, кто ДОЖИЛ, снижение — обычное дело.")
    print("  Объявление, снятое на третий день, в эту статистику не попадает:")
    print("  оно цензурировано, а не «не снизило».")

    print("\nЧто из этого нельзя говорить: что агентство «готово торговаться».")
    print("Мы видим изменение цены в объявлении, а не разговор с арендатором.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
