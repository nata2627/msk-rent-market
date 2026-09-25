"""Гедоническая регрессия: сколько стоит разрешение держать животное.

Что такое гедоническая модель. Квартира — набор признаков, и её цена
складывается из вкладов этих признаков. Оценивая

    log(цена) = a + b·животные + контроли + эффект района + остаток

мы спрашиваем: две одинаковые квартиры в одном районе, одна с
разрешёнными животными, другая без — на сколько отличается ставка.
Логарифм слева не для красоты: ставки распределены примерно логнормально,
а коэффициент при логарифме читается как процент.

**Это не причинный эффект, и выдавать его за причинный нельзя.** Никто не
бросал монетку, решая, разрешить ли животных. Квартиры с разрешением
систематически другие, и часть различий не наблюдается вовсе: состояние
мебели, терпимость хозяина, возраст дома внутри одного района. Контроли и
фиксированные эффекты района убирают то, что видно; остальное остаётся в
оценке. Поэтому правильная формулировка — «разница в ставке между
сопоставимыми объявлениями», а не «надбавка за животных».

Что здесь считается:

1. Признак площадки (галочка «можно с животными») — определён у всех
   объявлений до единого, поэтому отбора по измерению нет.
2. То же с фиксированными эффектами района.
3. То же с полным набором контролей — основная оценка.
4. Текстовый контраст: «разрешено» против «запрещено», только там, где
   текст говорит. Контраст резче, но выборка уже не случайная.
5. Четыре состояния сразу, база — «молчит».
6. Точное сопоставление: сравниваем только внутри ячеек
   «район × комнатность × площадь × метро».

Разница между (1) и (3) показывает, сколько наблюдаемой разницы съедают
контроли. Если она велика — значит велика и та часть, что осталась в
ненаблюдаемом.
"""
from __future__ import annotations

import argparse
import logging
import math

import numpy as np

from etl import db
from src.linreg import fit

log = logging.getLogger("hedonic")

CURRENT_YEAR = 2026

SQL = """
select price, total_area, rooms_count, floor, floors_total, build_year,
       building_material, metro_walk_min, large_park_distance_m, inside_mkad,
       is_apartments, has_furniture, has_washer, has_dishwasher,
       has_conditioner, has_lift, photos_count, seller_type, flat_type,
       district, pets_state, pets_source, pets_platform_allowed
from marts.offer_segment
where source = 'cian'
  and snapshot_date = %s
  and not is_daily
  and not is_duplicate
  and not is_price_outlier
  and price is not null and price > 0
  and total_area is not null and total_area > 0
  and district is not null
  and rooms_count is not null
  and floor is not null and floors_total is not null
"""


def load(conn, snapshot_date: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(SQL, (snapshot_date,))
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------
def _dummies(values, base, prefix):
    """Фиктивные переменные с явной базовой категорией.

    Базу задаём руками, а не «первую попавшуюся»: иначе коэффициенты
    меняют смысл при каждом новом сборе, когда меняется частотность.
    """
    levels = sorted({v for v in values if v is not None and v != base})
    cols, names = [], []
    for level in levels:
        cols.append(np.array([1.0 if v == level else 0.0 for v in values]))
        names.append(f"{prefix}:{level}")
    missing = [v is None for v in values]
    if any(missing):
        cols.append(np.array([1.0 if m else 0.0 for m in missing]))
        names.append(f"{prefix}:нет данных")
    return cols, names


def _numeric_with_missing(values, name, transform=lambda v: float(v)):
    """Числовой признак, у которого бывают пропуски.

    Пропуск не выбрасывается и не заменяется нулём: пропущенные значения
    заполняются медианой, а рядом ставится флаг «здесь было пусто». Так
    объявление остаётся в выборке, а модель может учесть, что отсутствие
    данных само по себе о чём-то говорит.
    """
    present = [transform(v) for v in values if v is not None]
    median = float(np.median(present)) if present else 0.0
    filled = np.array([transform(v) if v is not None else median for v in values])
    cols, names = [filled], [name]
    if any(v is None for v in values):
        cols.append(np.array([1.0 if v is None else 0.0 for v in values]))
        names.append(f"{name}:нет данных")
    return cols, names


def controls(rows: list[dict]) -> tuple[list[np.ndarray], list[str]]:
    cols: list[np.ndarray] = []
    names: list[str] = []

    cols.append(np.array([math.log(float(r["total_area"])) for r in rows]))
    names.append("log площадь")

    rooms = [min(int(r["rooms_count"]), 4) if r["rooms_count"] is not None else None
             for r in rows]
    c, n = _dummies(rooms, base=1, prefix="комнат")
    cols += c; names += n

    cols.append(np.array([1.0 if r["floor"] == 1 else 0.0 for r in rows]))
    names.append("первый этаж")
    cols.append(np.array([1.0 if r["floor"] == r["floors_total"] else 0.0 for r in rows]))
    names.append("последний этаж")
    cols.append(np.array([math.log(max(int(r["floors_total"]), 1)) for r in rows]))
    names.append("log этажей в доме")

    # Возраст дома, а не год постройки: так коэффициент читается
    # напрямую, и его знак не зависит от начала отсчёта.
    c, n = _numeric_with_missing([r["build_year"] for r in rows], "возраст дома",
                                 lambda v: float(CURRENT_YEAR - int(v)))
    cols += c; names += n

    c, n = _numeric_with_missing([r["metro_walk_min"] for r in rows],
                                 "минут до метро")
    cols += c; names += n

    cols.append(np.array([math.log1p(float(r["large_park_distance_m"] or 0))
                          for r in rows]))
    names.append("log до крупного парка")

    cols.append(np.array([1.0 if r["inside_mkad"] else 0.0 for r in rows]))
    names.append("внутри МКАД")

    c, n = _dummies([r["building_material"] for r in rows], base="panel", prefix="материал")
    cols += c; names += n
    c, n = _dummies([r["seller_type"] for r in rows], base="realtor_based",
                    prefix="продавец")
    cols += c; names += n

    for field, label in (("is_apartments", "апартаменты"),
                         ("has_furniture", "мебель"),
                         ("has_washer", "стиральная машина"),
                         ("has_dishwasher", "посудомойка"),
                         ("has_conditioner", "кондиционер"),
                         ("has_lift", "лифт")):
        cols.append(np.array([1.0 if r[field] else 0.0 for r in rows]))
        names.append(label)

    c, n = _numeric_with_missing([r["photos_count"] for r in rows], "фотографий")
    cols += c; names += n
    return cols, names


def fixed_effects(values, prefix: str):
    """Фиктивные переменные по территории или по ячейке сопоставления."""
    levels = sorted(set(values))[1:]          # первый уровень — база
    cols = [np.array([1.0 if v == lv else 0.0 for v in values]) for lv in levels]
    return cols, [f"{prefix}:{lv}" for lv in levels]


def build(rows, treat_col, treat_name, use_controls=True, fe=None, fe_prefix="район"):
    """Матрица плана. Порядок столбцов имеет значение — см. linreg._full_rank_columns."""
    y = np.array([math.log(float(r["price"])) for r in rows])
    cols = [np.ones(len(rows)), treat_col]
    names = ["константа", treat_name]
    if use_controls:
        c, n = controls(rows)
        cols += c; names += n
    if fe is not None:
        c, n = fixed_effects(fe, fe_prefix)
        cols += c; names += n
    return y, np.column_stack(cols), names


def report(title: str, f, treat_name: str, note: str = "") -> None:
    b, se = f.get(treat_name)
    effect = 100 * (math.exp(b) - 1)
    lo = 100 * (math.exp(b - 1.96 * se) - 1)
    hi = 100 * (math.exp(b + 1.96 * se) - 1)
    stars = "***" if abs(b / se) > 2.58 else "**" if abs(b / se) > 1.96 else ""
    print(f"\n{title}")
    if note:
        print(f"  {note}")
    print(f"  наблюдений {f.n}, признаков {f.k}, R² {f.r2:.3f}")
    print(f"  эффект: {effect:+.1f}% [{lo:+.1f}%; {hi:+.1f}%]  "
          f"коэфф. {b:+.4f} ± {se:.4f} {stars}")


def cell_key(r) -> str:
    """Ячейка точного сопоставления.

    Площадь и метро огрубляются: требовать совпадения до метра и до минуты
    значит остаться без пар. Шаг выбран так, чтобы внутри ячейки квартиры
    были взаимозаменяемы для съёмщика, а пар хватило.
    """
    area_bin = int(float(r["total_area"]) // 10)
    metro = r["metro_walk_min"]
    metro_bin = "нет" if metro is None else str(min(int(metro) // 5, 6))
    return f'{r["district"]}|{min(int(r["rooms_count"]), 4)}|{area_bin}|{metro_bin}'


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Гедоническая регрессия ставки аренды")
    parser.add_argument("--date", default=None,
                        help="дата снимка; по умолчанию последняя")
    parser.add_argument("--coefficients", action="store_true",
                        help="показать все коэффициенты основной модели")
    args = parser.parse_args()

    with db.connect() as conn:
        if args.date is None:
            with conn.cursor() as cur:
                cur.execute("select max(snapshot_date) from core.offer_observation "
                            "where source='cian'")
                args.date = str(cur.fetchone()[0])
        rows = load(conn, args.date)

    if len(rows) < 500:
        log.error("Наблюдений всего %d — оценивать нечего.", len(rows))
        return 1

    districts = [r["district"] for r in rows]
    print(f"\nСнимок {args.date}. Объявлений в оценке: {len(rows)}, "
          f"районов: {len(set(districts))}")
    print("Из выборки исключены дубли, выбросы по цене и посуточная аренда.")

    ticked = np.array([1.0 if r["pets_platform_allowed"] else 0.0 for r in rows])
    share = 100 * ticked.mean()
    print(f"Галочка «можно с животными» стоит у {ticked.sum():.0f} объявлений "
          f"({share:.1f}%).")

    print("\n" + "=" * 78)
    print("ПРИЗНАК ПЛОЩАДКИ: галочка стоит против «не стоит». Определён у всех.")
    print("=" * 78)

    y, X, names = build(rows, ticked, "животные", use_controls=False)
    report("(1) Без контролей — просто разница средних",
           fit(y, X, names, cluster=np.array(districts)), "животные",
           "столько видно «в лоб», вместе со всеми различиями квартир")

    y, X, names = build(rows, ticked, "животные", use_controls=False, fe=districts)
    report("(2) Фиксированные эффекты района",
           fit(y, X, names, cluster=np.array(districts)), "животные",
           "сравнение внутри района: география больше ни при чём")

    y, X, names = build(rows, ticked, "животные", use_controls=True, fe=districts)
    main_fit = fit(y, X, names, cluster=np.array(districts))
    report("(3) Контроли и район — основная оценка", main_fit, "животные",
           "площадь, комнатность, этаж, возраст дома, метро, материал, "
           "техника, тип продавца")

    cells = [cell_key(r) for r in rows]
    y, X, names = build(rows, ticked, "животные", use_controls=True,
                        fe=cells, fe_prefix="ячейка")
    report("(6) Точное сопоставление внутри ячеек "
           "«район × комнатность × площадь × метро»",
           fit(y, X, names, cluster=np.array(districts)), "животные",
           f"ячеек: {len(set(cells))}; сравниваются только соседи по ячейке")

    print("\n" + "=" * 78)
    print("ТЕКСТ ОБЪЯВЛЕНИЯ: «разрешено» против «запрещено». Выборка неполная.")
    print("=" * 78)

    spoken = [r for r in rows if r["pets_state"] in ("allowed", "forbidden")]
    print(f"\nТекст говорит о животных у {len(spoken)} объявлений из {len(rows)} "
          f"({100*len(spoken)/len(rows):.0f}%).")
    if len(spoken) >= 500:
        t = np.array([1.0 if r["pets_state"] == "allowed" else 0.0 for r in spoken])
        d = [r["district"] for r in spoken]
        y, X, names = build(spoken, t, "животные", use_controls=True, fe=d)
        report("(4) Контроли и район, только говорящие объявления",
               fit(y, X, names, cluster=np.array(d)), "животные",
               "контраст резче, но молчащие 57% выброшены — это отбор")

    print("\n" + "=" * 78)
    print("ВСЕ ЧЕТЫРЕ СОСТОЯНИЯ СРАЗУ. База — «текст молчит и галочки нет».")
    print("=" * 78)

    states = [r["pets_state"] for r in rows]
    y = np.array([math.log(float(r["price"])) for r in rows])
    cols = [np.ones(len(rows))]
    names = ["константа"]
    for level in ("allowed", "forbidden", "negotiable"):
        cols.append(np.array([1.0 if s == level else 0.0 for s in states]))
        names.append(f"животные:{level}")
    c, n = controls(rows); cols += c; names += n
    c, n = fixed_effects(districts, "район"); cols += c; names += n
    f4 = fit(y, np.column_stack(cols), names, cluster=np.array(districts))
    print(f"\n(5) Четыре состояния, контроли и район")
    print(f"  наблюдений {f4.n}, признаков {f4.k}, R² {f4.r2:.3f}")
    print(f4.table(only=[f"животные:{s}" for s in
                        ("allowed", "forbidden", "negotiable")]))

    if args.coefficients:
        print("\n" + "=" * 78)
        print("ВСЕ КОЭФФИЦИЕНТЫ ОСНОВНОЙ МОДЕЛИ (3), кроме районных")
        print("=" * 78)
        print(main_fit.table(only=[n for n in main_fit.names
                                   if not n.startswith("район:")]))

    print("\nЧто из этого нельзя говорить: что разрешение животных «приносит»")
    print("столько-то процентов. Это разница между объявлениями, а не эффект")
    print("решения. Ненаблюдаемое — состояние квартиры, терпимость хозяина —")
    print("в контроли не попало и сидит в оценке.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
