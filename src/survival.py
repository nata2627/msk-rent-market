"""Сколько живёт объявление до снятия с публикации.

Второй вопрос проекта. Сразу и навсегда оговорка, без которой всё
остальное читается неправильно: **снятие с публикации это не сдача
квартиры**. Объявление снимают, когда квартиру сдали, когда передумали
сдавать, когда переложили к другому агенту и когда у публикации просто
вышел срок. Различить эти причины по выдаче нельзя.

Почему нельзя просто взять среднее по снятым
--------------------------------------------

Две ловушки, и обе дают ответ, который выглядит правдоподобно.

**Цензурирование справа.** Объявление, которое висит до сих пор, прожило
«не меньше чем», а сколько проживёт, неизвестно. Выбросить такие значит
оставить только короткоживущие и занизить срок вдвое. Поэтому
Каплан-Мейер, а не среднее.

**Левое усечение.** Панель началась 22.09.2026, и объявления, успевшие
появиться и исчезнуть до этого дня, нам не попались вовсе. Среди
доживших до начала наблюдения долгожителей больше, чем на рынке в целом.
Лечится поздним вхождением: объявление входит в группу риска не с
рождения, а с того возраста, в котором мы его впервые застали.

Поздним вхождением можно пользоваться потому, что дата публикации
известна у всех объявлений и она не двигается. Проверено прямо:
объявлений, у которых первая встреча РАНЬШЕ даты публикации, ноль из
38 990. Если бы площадка обновляла дату при поднятии объявления, такие
случаи были бы — запись в панель идёт через coalesce и новое значение
затёрло бы старое.

Что считается
-------------

* кривая дожития с поздним вхождением по всем объявлениям сегмента;
* она же по когорте, которую видели с рождения, — проверка на
  неоднородность: если доживающие до начала панели устойчивее новичков,
  две кривые разойдутся;
* снятия по точному возрасту — там виден срок жизни самой публикации.

Запуск: `make survival` или `python -m src.survival`.
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter
from datetime import timedelta

from etl import db
from etl.db import DISAPPEARED_AFTER_DAYS

log = logging.getLogger("survival")

SQL = """
select o.offer_id,
       o.published_at::date               as published,
       o.first_seen_date                  as first_seen,
       o.disappeared_date                 as disappeared,
       o.is_active,
       o.rooms_count,
       obs.seller_type,
       obs.pets_state
from core.offer o
left join lateral (
    select seller_type, pets_state
    from core.offer_observation
    where source = o.source and offer_id = o.offer_id
    order by snapshot_date desc limit 1
) obs on true
where o.source = 'cian'
  and o.published_at is not null
  and o.rooms_count = any(%s)
"""


def load(conn, rooms: list[int]) -> tuple[list[dict], object]:
    with conn.cursor() as cur:
        cur.execute(SQL, (rooms,))
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        # Граница наблюдения — не последний собранный день, а он же минус
        # срок подтверждения снятия. Объявление, пропавшее позавчера, ещё
        # не помечено снятым, и если считать его живым, последние дни
        # кривой задерутся вверх.
        cur.execute("select max(snapshot_date) from core.offer_observation "
                    "where source = 'cian'")
        last_day = cur.fetchone()[0]
    cutoff = last_day - timedelta(days=DISAPPEARED_AFTER_DAYS)
    return rows, cutoff


# ---------------------------------------------------------------------
def spans(rows: list[dict], cutoff) -> list[tuple[int, int, bool]]:
    """Отрезок наблюдения каждого объявления в возрастах.

    Возвращает (возраст входа, возраст выхода, случилось ли снятие).
    Возраст считается от даты публикации, а не от первой встречи: иначе
    у объявления, которое мы застали двухнедельным, срок жизни начался бы
    с нуля, и все сроки оказались бы занижены.
    """
    out = []
    for r in rows:
        pub = r["published"]
        entry = (r["first_seen"] - pub).days
        event = r["disappeared"] is not None and r["disappeared"] <= cutoff
        end = r["disappeared"] if event else cutoff
        exit_ = (end - pub).days
        # Публикация «из будущего» и отрицательные отрезки — это мусор
        # данных, а не наблюдения. Их единицы, но в группе риска они
        # ломают знаменатель.
        if entry < 0 or exit_ < entry:
            continue
        out.append((entry, exit_, event))
    return out


def km(spans_: list[tuple[int, int, bool]], max_age: int = 120):
    """Каплан-Мейер с поздним вхождением.

    В группе риска в возрасте t те, кого мы в этом возрасте наблюдали:
    вошли не позже t и вышли не раньше t. Это и есть поправка на левое
    усечение — объявление не числится в риске в возрасте, когда мы на
    него ещё не смотрели.
    """
    risk = Counter()
    events = Counter()
    for entry, exit_, event in spans_:
        for age in range(entry, min(exit_, max_age) + 1):
            risk[age] += 1
        if event and exit_ <= max_age:
            events[exit_] += 1

    curve, s = [], 1.0
    for age in range(0, max_age + 1):
        n, d = risk[age], events[age]
        if n > 0:
            s *= (1 - d / n)
        curve.append((age, n, d, s))
    return curve


def median_age(curve) -> float | None:
    """Первый возраст, в котором доживает уже меньше половины."""
    for age, n, _, s in curve:
        if s <= 0.5 and n > 0:
            return age
    return None


# ---------------------------------------------------------------------
def print_curve(curve, ages) -> None:
    print(f"  {'возраст':>8} {'в риске':>9} {'снято':>7} {'доживает':>10}")
    for age, n, d, s in curve:
        if age in ages and n > 0:
            print(f"  {age:>8} {n:>9} {d:>7} {100 * s:>9.1f}%")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Срок жизни объявления до снятия")
    parser.add_argument("--rooms", default="1,2",
                        help="комнатности через запятую; по умолчанию сегмент")
    args = parser.parse_args()
    rooms = [int(x) for x in args.rooms.split(",")]

    with db.connect() as conn:
        rows, cutoff = load(conn, rooms)

    sp = spans(rows, cutoff)
    if len(sp) < 500:
        log.error("Наблюдений всего %d — считать нечего.", len(sp))
        return 1

    events = sum(1 for _, _, e in sp if e)
    print(f"Комнатность {args.rooms}. Объявлений {len(sp)}, из них снято {events} "
          f"({100 * events / len(sp):.0f}%).")
    print(f"Наблюдение обрезано {cutoff}: снятие подтверждается только на "
          f"{DISAPPEARED_AFTER_DAYS}-й собранный день отсутствия,")
    print("и события последних дней ещё не проставлены.")

    print("\n" + "=" * 78)
    print("КРИВАЯ ДОЖИТИЯ. Все объявления, поздним вхождением.")
    print("=" * 78)
    curve = km(sp)
    print_curve(curve, {1, 3, 5, 7, 10, 14, 21, 30, 31, 35, 45, 60, 90})
    med = median_age(curve)
    print(f"\n  Медиана срока жизни: {med} дней"
          if med else "\n  Медиана за пределами наблюдения")

    # Когорта, которую видели с рождения. Если риск снятия зависит только
    # от возраста, обе кривые обязаны совпасть. Если разойдутся — значит
    # дожившие до начала панели устойчивее новичков, и это надо сказать.
    born = [s for s in sp if s[0] == 0]
    print("\n" + "=" * 78)
    print("ПРОВЕРКА. Только те, кого видели с рождения.")
    print("=" * 78)
    print(f"  Таких {len(born)}, из них снято {sum(1 for _, _, e in born if e)}.")
    bc = km(born)
    print_curve(bc, {1, 3, 5, 7, 9, 11, 13})
    print("\n  Расхождение с общей кривой в тех же возрастах:")
    for age in (3, 5, 7, 9):
        a = next(s for g, n, d, s in curve if g == age)
        b = next(s for g, n, d, s in bc if g == age)
        print(f"    возраст {age:>2}: все {100 * a:.1f}%, "
              f"с рождения {100 * b:.1f}%, разница {100 * (a - b):+.1f} п.п.")

    # Срок жизни самой публикации. Если площадка снимает объявление по
    # истечении срока, это видно всплеском на круглом возрасте — и такие
    # снятия к сдаче квартиры отношения не имеют вовсе.
    print("\n" + "=" * 78)
    print("СНЯТИЯ ПО ТОЧНОМУ ВОЗРАСТУ. Ищем срок жизни публикации.")
    print("=" * 78)
    by_age = Counter(exit_ for _, exit_, e in sp if e)
    window = [(a, by_age[a]) for a in range(25, 36)]
    base = sorted(c for a, c in window if a not in (30, 31))
    typical = base[len(base) // 2] if base else 0
    print(f"  {'возраст':>8} {'снятий':>8}")
    for a, c in window:
        mark = "  <-- всплеск" if typical and c > 2 * typical else ""
        print(f"  {a:>8} {c:>8}{mark}")
    spike = by_age[30] + by_age[31]
    print(f"\n  На 30-31 день снято {spike} объявлений при обычных "
          f"~{typical} в день.")
    print("  Лишнее над фоном — это истечение публикации, а не сдача квартиры.")
    print(f"  Доля таких снятий от всех: {100 * max(0, spike - 2 * typical) / events:.1f}%")

    print("\nЧто из этого нельзя говорить: что медиана срока жизни — это срок")
    print("сдачи квартиры. Это срок, за который объявление исчезает из выдачи,")
    print("по любой причине, включая истечение публикации.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
