"""Пересчитать условия платежа по уже сохранённым описаниям.

К площадке не обращается вовсе: текст описания лежит в панели, и оба
поля пересчитываются из него на месте. Поэтому, в отличие от добора
животных, этот проход можно делать задним числом сколько угодно — он не
спрашивает рынок о том, каким тот был вчера, а перечитывает сохранённое.

Что пересчитывается и зачем:

* `prepay_months` — площадка поле `terms.prepayMonths` не заполняет
  никогда, а условие в тексте встречается. Разбор добавлен позже сбора,
  поэтому уже собранные дни надо догнать.
* `deposit_months` — разбор был, но связка в правиле пускала через
  границу предложения, и «Договор 11 месяцев» читалось как залог в
  одиннадцать месячных плат. Правило исправлено, старые значения надо
  переписать: 202 наблюдения на панели из пяти дат.

Запускается вручную после правок разбора:

    python -m etl.terms_backfill --dry-run
    python -m etl.terms_backfill
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter

from etl import db
from etl.parse import deposit_months_from_text, prepay_months_from_text

log = logging.getLogger("terms_backfill")

SELECT_SQL = """
select source, offer_id, snapshot_date, description, prepay_months, deposit_months
from core.offer_observation
where description is not null
  and (%(date)s::date is null or snapshot_date = %(date)s::date)
"""

UPDATE_SQL = """
update core.offer_observation
   set prepay_months = %s, deposit_months = %s
 where source = %s and offer_id = %s and snapshot_date = %s
"""


def _same(old, new) -> bool:
    if old is None or new is None:
        return old is None and new is None
    return float(old) == float(new)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Условия платежа из уже сохранённых описаний")
    ap.add_argument("--date", default=None,
                    help="дата снимка; по умолчанию все даты")
    ap.add_argument("--dry-run", action="store_true",
                    help="посчитать, но не записывать")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    prepay_hist: Counter = Counter()
    deposit_hist: Counter = Counter()
    changed = 0

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(SELECT_SQL, {"date": args.date})
            rows = cur.fetchall()
        log.info("Описаний к разбору: %d", len(rows))

        updates = []
        for source, offer_id, snapshot_date, description, prepay_old, dep_old in rows:
            prepay = prepay_months_from_text(description)
            deposit = deposit_months_from_text(description)
            if prepay is not None:
                prepay_hist[prepay] += 1
            if deposit is not None:
                deposit_hist[deposit] += 1
            if not _same(prepay_old, prepay) or not _same(dep_old, deposit):
                changed += 1
                updates.append((prepay, deposit, source, offer_id, snapshot_date))

        if updates and not args.dry_run:
            with conn.cursor() as cur:
                cur.executemany(UPDATE_SQL, updates)
            conn.commit()

    log.info("Просмотрено %d, изменено %d%s", len(rows), changed,
             " (прогон вхолостую)" if args.dry_run else "")
    log.info("Предоплата, месяцев: %s",
             ", ".join(f"{k:g}→{v}" for k, v in sorted(prepay_hist.items())) or "нет")
    log.info("Залог, месяцев: %s",
             ", ".join(f"{k:g}→{v}" for k, v in sorted(deposit_hist.items())) or "нет")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
