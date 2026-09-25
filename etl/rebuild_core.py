"""Пересобрать панель из сырых снимков.

    python -m etl.rebuild_core --dry-run
    python -m etl.rebuild_core

Ради этой возможности слой raw и хранит payload целиком. Если разбор поля
оказался неверным или площадка отдала поле, которого разбор не знал, —
core пересобирается заново, без повторного обращения к площадке. Так
нашлись и добавились goodPrice, homeownerProofs и identicalOffers: они
лежали в сырых снимках с самого начала, просто не разбирались.

Что пересобирается: core.offer, core.offer_observation, core.offer_metro,
core.offer_photo. Что НЕ трогается: raw (он источник), и результаты
обогащения — расстояния до парков, флаг МКАД, дедупликация. Обогащение
считается отдельными командами по координатам, которые пересборка
восстанавливает.
"""
from __future__ import annotations

import argparse
import logging
import sys

import psycopg

from etl import db
from etl.config import DB
from etl.parse import parse_offer

log = logging.getLogger("rebuild")
BATCH = 2000



def restore_pets(conn, source: str) -> None:
    """Вернуть разметку животных по журналу прохода с фильтром.

    Разбор текста восстанавливается из payload сам, а вот факт попадания
    под фильтр площадки в payload не лежит — он записан отдельно, в
    raw.pets_hit. Без этого шага пересборка core молча стирает самую
    надёжную из четырёх меток, и заметно это становится только когда
    регрессия показывает ноль объявлений с галочкой.

    Дни, собранные до появления raw.pets_hit, восстановить нечем:
    членство тогда нигде не сохранялось. Такие дни остаются с разметкой
    из текста, и это видно в журнале.
    """
    with conn.cursor() as cur:
        cur.execute("select distinct snapshot_date from raw.pets_hit "
                    "where source = %s order by 1", (source,))
        dates = [r[0] for r in cur.fetchall()]

    if not dates:
        log.warning("Журнал прохода с фильтром пуст: разметка животных "
                    "осталась только из текста.")
        return

    for snapshot_date in dates:
        ids = db.pets_hits_for_date(conn, snapshot_date, source=source)
        complete = db.pets_pass_was_complete(conn, snapshot_date, source=source)
        marked = db.mark_pets_allowed(conn, sorted(ids), snapshot_date,
                                      source=source, mark_rest_false=complete)
        log.info("  животные за %s: помечено %d%s", snapshot_date, marked,
                 "" if complete else " (проход неполный, остальные не помечены)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Пересобрать core из raw")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--source", default="cian")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    with psycopg.connect(**DB) as conn:
        with conn.cursor() as cur:
            cur.execute("""select count(*), count(distinct offer_id),
                                  count(distinct snapshot_date)
                           from raw.offer_snapshot where source = %s""", (args.source,))
            total, offers, dates = cur.fetchone()
        log.info("В сыром слое: %d снимков, %d объявлений, %d дат",
                 total, offers, dates)

        if args.dry_run:
            log.info("--dry-run: ничего не изменено")
            return 0

        # Идём по (объявление, дата) и берём ПОСЛЕДНИЙ снимок этого дня —
        # ровно так же, как это делает сбор: наблюдение панели равно
        # последнему чтению суток.
        done = 0
        offset = 0
        while True:
            with conn.cursor() as cur:
                cur.execute("""
                    select distinct on (offer_id, snapshot_date)
                           payload, snapshot_date, run_id
                    from raw.offer_snapshot
                    where source = %s
                    order by offer_id, snapshot_date, snapshot_id desc
                    limit %s offset %s""", (args.source, BATCH, offset))
                rows = cur.fetchall()
            if not rows:
                break

            by_run: dict[int, list] = {}
            for payload, snapshot_date, run_id in rows:
                parsed = parse_offer(payload, snapshot_date, source=args.source)
                if parsed:
                    by_run.setdefault((run_id, snapshot_date), []).append(parsed)

            for (run_id, snapshot_date), parsed in by_run.items():
                db.upsert_offers(conn, parsed, snapshot_date)
                db.upsert_observations(conn, parsed, run_id)
                db.upsert_metro(conn, parsed)
                db.upsert_photos(conn, parsed)
            done += len(rows)
            offset += BATCH
            log.info("  пересобрано наблюдений: %d", done)

        restore_pets(conn, args.source)

        log.info("Готово. Обогащение (парки, МКАД, дубли) считается отдельно: "
                 "make parks && make dedup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
