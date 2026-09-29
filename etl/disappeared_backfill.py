"""Пересчитать снятия с публикации по всей панели.

К площадке не обращается: в какие дни какая комнатность собиралась,
записано в raw.collection_run и raw.bucket, а больше для вывода о снятии
ничего не нужно. Поэтому проход можно повторять сколько угодно — он не
спрашивает рынок о том, каким тот был вчера, а перечитывает журнал
обхода.

Нужен после правки правила и после `make rebuild`: пересборка core
возвращает всем объявлениям is_active, и снятия приходится выводить
заново.

    python -m etl.disappeared_backfill --dry-run
    python -m etl.disappeared_backfill
"""
from __future__ import annotations

import argparse
import logging
from datetime import date

import psycopg

from etl import db
from etl.config import DB

log = logging.getLogger("disappeared_backfill")

# Снятия, датированные днём, в который эту комнатность никто не обходил.
# Ровно то, что чинится этим проходом; тот же запрос лежит в
# sql/checks/02_panel_integrity.sql, чтобы расхождение не завелось молча.
UNCOVERED = """
select count(*) from core.offer o
where o.source = %s and o.disappeared_date is not null
  and not exists (
      select 1 from raw.collection_run r join raw.bucket b using (run_id)
      where r.source = o.source and r.status = 'ok'
        and r.snapshot_date = o.disappeared_date
        and b.rooms = case when o.rooms_count between 1 and 6
                                then o.rooms_count::text
                           when o.flat_type = 'studio'   then '7'
                           when o.flat_type = 'openPlan' then '9'
                           end)
"""


def state(conn, source: str) -> tuple[int, int]:
    with conn.cursor() as cur:
        cur.execute("select count(*) from core.offer "
                    "where source = %s and not is_active", (source,))
        marked = cur.fetchone()[0]
        cur.execute(UNCOVERED, (source,))
        uncovered = cur.fetchone()[0]
    return marked, uncovered


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Пересчитать снятия с публикации")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--source", default="cian")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    with psycopg.connect(**DB) as conn:
        marked, uncovered = state(conn, args.source)
        log.info("Сейчас снятых: %d, из них датированы несобранным днём: %d",
                 marked, uncovered)

        if args.dry_run:
            log.info("--dry-run: ничего не изменено")
            return 0

        affected = db.remark_disappeared(conn, date.today(), source=args.source)
        marked, uncovered = state(conn, args.source)
        log.info("Пересчитано. Снятых: %d (отмечено этим проходом %d), "
                 "датированы несобранным днём: %d", marked, affected, uncovered)
        if uncovered:
            log.warning("Остались снятия на несобранные дни — правило и данные "
                        "разошлись, смотреть sql/checks/02_panel_integrity.sql")
        log.info("Напоминание: снятие с публикации — это НЕ сдача квартиры.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
