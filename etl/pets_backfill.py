"""Повторить проход с фильтром «можно с животными» по уже пройденным корзинам.

Зачем это нужно. Разметка животных по анкете площадки жила только в
core: проход с фильтром намеренно не сохранял карточки повторно, а сам
факт попадания под фильтр никуда больше не писался. Пересборка core из
raw эту метку стирала — молча, потому что raw считался самодостаточным.
Теперь членство пишется в `raw.pets_hit`, и пересборка его возвращает.
Но дни, собранные до этой правки, восстановить из базы нечем: там
членства попросту нет.

Здесь оно добирается повторным запросом — и именно повторным проходом
по УЖЕ сохранённым корзинам, а не новым обходом. Нарезка лежит в
`raw.bucket` вместе с заявленными числами, так что делить корзины заново
не нужно: первые страницы всех корзин уже были запрошены сегодня, и
спрашивать их второй раз было бы лишней нагрузкой на площадку.

Оговорка, которую надо держать в голове: ответ площадки берётся сейчас,
а снимок сделан раньше. Для сегодняшнего дня это одно и то же состояние
рынка; для вчерашнего — уже нет, и добирать задним числом нельзя.
"""
from __future__ import annotations

import argparse
import logging
import math

from etl import config, db
from etl.browser_client import BrowserClient
from etl.cian_client import Blocked, PageWithoutData
from etl.partition import Bucket

log = logging.getLogger("pets_backfill")

BUCKETS_SQL = """
select b.bucket_id, b.district_id, b.district, b.okrug, b.rooms,
       b.price_min, b.price_max, b.claimed_count, b.run_id
from raw.bucket b
join raw.collection_run r using (run_id)
where r.snapshot_date = %s
  and b.pets_filter
  and b.error is null
order by b.bucket_id
"""


def pets_buckets(conn, snapshot_date):
    with conn.cursor() as cur:
        cur.execute(BUCKETS_SQL, (snapshot_date,))
        return cur.fetchall()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="дата снимка, YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with db.connect() as conn:
        buckets = pets_buckets(conn, args.date)
        if not buckets:
            log.error("За %s нет корзин с фильтром животных. Нечего повторять.",
                      args.date)
            return 1

        log.info("Корзин с фильтром за %s: %d", args.date, len(buckets))
        if args.dry_run:
            for b in buckets[:5]:
                log.info("  %s -> заявлено %s", Bucket(*b[1:7]).label(), b[7])
            log.info("Холостой прогон: запросов не делалось.")
            return 0

        client = BrowserClient()
        all_ids: set[str] = set()
        complete = True
        failed = 0

        try:
            for i, (bucket_id, district_id, district, okrug, rooms,
                    price_min, price_max, claimed, run_id) in enumerate(buckets, 1):
                bucket = Bucket(district_id=district_id, district=district,
                                okrug=okrug, rooms=rooms,
                                price_min=price_min, price_max=price_max)
                try:
                    offers, declared = client.search(bucket, pets_only=True)
                except PageWithoutData as err:
                    log.warning("[%d/%d] %s — страница без данных (%s)",
                                i, len(buckets), bucket.label(), err)
                    complete = False
                    failed += 1
                    if failed >= 10:
                        log.error("Десять сбойных корзин подряд. Останавливаюсь.")
                        complete = False
                        break
                    continue

                ids = {str(o["id"]) for o in offers if o.get("id") is not None}
                pages = min(math.ceil((declared or 0) / config.PAGE_SIZE),
                            config.MAX_PAGE)
                for page in range(2, pages + 1):
                    more, _ = client.search(bucket, page=page, pets_only=True)
                    if not more:
                        break
                    ids.update(str(o["id"]) for o in more if o.get("id") is not None)

                db.record_pets_hits(conn, run_id, bucket_id, args.date, sorted(ids))
                all_ids.update(ids)
                log.info("[%d/%d] %s -> с животными %d из %d",
                         i, len(buckets), bucket.label(), len(ids), claimed)
        except Blocked as err:
            log.error("Площадка закрыла доступ: %s. Останавливаюсь.", err)
            complete = False
        finally:
            client.close()

        log.info("Собрано объявлений с галочкой: %d", len(all_ids))

        # Пометить «не разрешено» у остальных можно только если проход
        # покрыл все корзины. Иначе собственный пробел превратится в факт
        # о рынке.
        for run_id in {b[8] for b in buckets}:
            db.set_pets_pass_complete(conn, run_id, complete)
        marked = db.mark_pets_allowed(conn, sorted(all_ids), args.date,
                                      mark_rest_false=complete)
        log.info("Помечено «можно с животными»: %d%s", marked,
                 "" if complete else " (проход неполный, остальные не трогаем)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
