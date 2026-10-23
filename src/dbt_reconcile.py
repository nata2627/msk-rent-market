"""Сверка витрин dbt со старыми витринами marts.

Старые витрины не трогаются, пока новые не сойдутся с ними числом. Число
строк совпасть может и у неверной витрины, поэтому сравниваются сами
строки: md5 от текста каждой строки, разность мультимножеств в обе
стороны. Ноль в обе стороны значит, что витрины совпадают построчно,
включая повторы.

Текст строки, а не сравнение по колонкам: в offer_latest есть колонки
json, у которых в Postgres нет оператора равенства, и EXCEPT по ним
падает. Обе витрины считаются одним и тем же SQL, поэтому и запись
дробных чисел в тексте у них одна.

Запуск: `make dbt-reconcile` после `make marts` и `make dbt-build`, на
одной и той же базе. Иначе старая витрина окажется вчерашней, и
расхождение будет про время, а не про логику.
"""
from __future__ import annotations

import sys

from etl import db

OLD, NEW = "marts", "dbt_marts"


def marts(conn) -> list[str]:
    """Все витрины старой схемы: обычные и материализованные представления."""
    with conn.cursor() as cur:
        cur.execute("""
            select c.relname from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = %s and c.relkind in ('v', 'm')
            order by 1
        """, (OLD,))
        return [r[0] for r in cur.fetchall()]


def compare(conn, name: str) -> tuple[int, int | None, int | None, int | None]:
    with conn.cursor() as cur:
        cur.execute(f"select count(*) from {OLD}.{name}")
        old_n = cur.fetchone()[0]
        cur.execute("select to_regclass(%s)", (f"{NEW}.{name}",))
        if cur.fetchone()[0] is None:
            return old_n, None, None, None
        cur.execute(f"select count(*) from {NEW}.{name}")
        new_n = cur.fetchone()[0]
        diff = []
        for a, b in ((OLD, NEW), (NEW, OLD)):
            cur.execute(f"""
                select count(*) from (
                    select md5(t::text) from {a}.{name} t
                    except all
                    select md5(t::text) from {b}.{name} t) d
            """)
            diff.append(cur.fetchone()[0])
        return old_n, new_n, diff[0], diff[1]


def main() -> int:
    print("| витрина | строк в marts | строк в dbt | только в marts | только в dbt | итог |")
    print("|---|---:|---:|---:|---:|---|")
    bad = 0
    with db.connect() as conn:
        for name in marts(conn):
            old_n, new_n, only_old, only_new = compare(conn, name)
            if new_n is None:
                verdict, bad = "нет в dbt", bad + 1
            elif only_old == only_new == 0:
                verdict = "сходится"
            else:
                verdict, bad = "РАСХОДИТСЯ", bad + 1
            cells = ["—" if v is None else f"{v:,}".replace(",", " ")
                     for v in (old_n, new_n, only_old, only_new)]
            print(f"| {name} | " + " | ".join(cells) + f" | {verdict} |")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
