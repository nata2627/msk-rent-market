"""Удалить персональные данные из уже собранного.

    python -m etl.scrub_existing --dry-run   # посмотреть, сколько найдётся
    python -m etl.scrub_existing             # вычистить

Это единственный случай, когда слой raw изменяется. Правило «сюда только
добавляют, ничего не переписывают» существует ради воспроизводимости:
если разбор поля окажется неверным, core пересобирается из raw заново.
Удаление телефонов этому не мешает — ни один из трёх вопросов
исследования их не использует, — а вот хранение чужих телефонов в базе,
которая станет частью публичного портфолио, недопустимо.

Запускается один раз. Новые данные чистятся при записи, см. etl/privacy.py.
"""
from __future__ import annotations

import argparse
import logging
import sys

import psycopg
from psycopg.types.json import Jsonb

from etl.config import DB
from etl.privacy import hash_seller_id, scrub_payload, scrub_text

log = logging.getLogger("scrub")
BATCH = 2000


def count_dirty(cur) -> dict:
    cur.execute("""
        select count(*) filter (where payload ? 'phones'),
               count(*) filter (where payload->'user' ?| array['agencyName','companyName','name']),
               count(*)
        from raw.offer_snapshot""")
    phones, names, total = cur.fetchone()
    cur.execute("""select count(seller_name), count(seller_id) from core.offer_observation""")
    seller_names, seller_ids = cur.fetchone()
    return {"снимков всего": total, "с телефоном": phones,
            "с именем автора": names, "имён в панели": seller_names,
            "идентификаторов в панели": seller_ids}


def scrub_raw(conn) -> int:
    done = 0
    while True:
        with conn.cursor() as cur:
            # Условие поиска намеренно грубое — по всему тексту payload,
            # а не по известным путям. Идентификатор автора нашёлся во
            # вложенном agentAvailability, куда перечисление путей не
            # доставало; чужую структуру перечислять нельзя.
            cur.execute("""
                select snapshot_id, payload from raw.offer_snapshot
                where payload::text ~ '"(phones|phoneNumbers|profileUri|agencyName|companyName)"'
                   or payload::text ~ '"(userId|cianUserId|publishedUserId)": ?[0-9]{5,}'
                limit %s""", (BATCH,))
            rows = cur.fetchall()
        if not rows:
            return done
        with conn.cursor() as cur:
            cur.executemany(
                "update raw.offer_snapshot set payload = %s where snapshot_id = %s",
                [(Jsonb(scrub_payload(p)), sid) for sid, p in rows])
        conn.commit()
        done += len(rows)
        log.info("  вычищено снимков: %d", done)


def scrub_core(conn) -> tuple[int, int]:
    with conn.cursor() as cur:
        # Имя продавца удаляется целиком: у частных риелторов там их
        # собственное имя, а для анализа нужен только тип продавца.
        cur.execute("update core.offer_observation set seller_name = null "
                    "where seller_name is not null")
        names = cur.rowcount

        # Идентификатор заменяется солёным хешем: группировка «сколько
        # объявлений у одного автора» сохраняется, список людей — нет.
        cur.execute("""select distinct seller_id from core.offer_observation
                       where seller_id is not null and length(seller_id) <> 16""")
        ids = [r[0] for r in cur.fetchall()]
        cur.executemany(
            "update core.offer_observation set seller_id = %s where seller_id = %s",
            [(hash_seller_id(i), i) for i in ids])

        cur.execute("""select offer_id, snapshot_date, description
                       from core.offer_observation
                       where description ~ '(\\+?7|8)[\\s\\-(]?9\\d{2}'""")
        texts = cur.fetchall()
        cur.executemany(
            """update core.offer_observation set description = %s
               where offer_id = %s and snapshot_date = %s""",
            [(scrub_text(d), oid, sd) for oid, sd, d in texts])
    conn.commit()
    return names, len(ids)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Удалить персональные данные из базы")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    with psycopg.connect(**DB) as conn:
        with conn.cursor() as cur:
            before = count_dirty(cur)
        print("\nДо очистки:")
        for k, v in before.items():
            print(f"  {k:<28} {v:>8}")

        if args.dry_run:
            print("\n--dry-run: ничего не изменено")
            return 0

        log.info("Чищу сырой слой...")
        n = scrub_raw(conn)
        log.info("Чищу панель...")
        names, ids = scrub_core(conn)

        with conn.cursor() as cur:
            after = count_dirty(cur)
        print("\nПосле очистки:")
        for k, v in after.items():
            print(f"  {k:<28} {v:>8}")
        print(f"\nСнимков переписано: {n}, имён удалено: {names}, "
              f"идентификаторов захешировано: {ids}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
