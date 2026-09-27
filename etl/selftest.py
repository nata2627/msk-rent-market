"""Сквозная проверка записи и панели на фикстуре, без обращения к площадке.

Зачем отдельно от pytest. Юнит-тесты проверяют чистые функции и не трогают
базу. Но самые дорогие ошибки живут не в разборе, а на стыке: идемпотентность
повторного запуска, вычисление изменения цены оконной функцией, фиксация
исчезновения. Проверить это можно только записью в настоящий PostgreSQL.

Данные пишутся с source='selftest' и в конце удаляются, поэтому проверка
не смешивается с собранными объявлениями ни при каком исходе.

    python -m etl.selftest
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

from etl import db
from etl.parse import parse_offer
from etl.rebuild_core import restore_pets
from etl.partition import Bucket, build_query

SOURCE = "selftest"
FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "offer_cian.json"

D1 = date(2026, 1, 10)
D2 = D1 + timedelta(days=3)
D3 = D2 + timedelta(days=4)

checks_passed = 0


def check(condition: bool, message: str) -> None:
    global checks_passed
    if condition:
        checks_passed += 1
        print(f"  ok   {message}")
    else:
        print(f"  ПРОВАЛ {message}")
        raise SystemExit(1)


def cleanup(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("delete from core.offer where source = %s", (SOURCE,))
        cur.execute("delete from raw.offer_snapshot where source = %s", (SOURCE,))
        cur.execute("delete from core.offer_observation where source = %s", (SOURCE,))
        cur.execute("delete from raw.pets_hit where source = %s", (SOURCE,))
        cur.execute("delete from raw.collection_run where source = %s", (SOURCE,))
    conn.commit()


def ingest(conn, payload: dict, snapshot_date: date, mode: str = "narrow") -> int:
    """Один обход одной корзины — тем же кодом, что и настоящий сбор."""
    run_id = db.start_run(conn, mode, snapshot_date, source=SOURCE)
    bucket = Bucket(rooms="2")
    bucket_id = db.insert_bucket(conn, run_id, bucket, build_query(bucket), None)

    offers = [payload] if payload else []
    db.save_snapshots(conn, run_id, bucket_id, snapshot_date, offers, page=1, source=SOURCE)
    parsed = [p for p in (parse_offer(o, snapshot_date, source=SOURCE) for o in offers) if p]
    db.upsert_offers(conn, parsed, snapshot_date)
    db.upsert_observations(conn, parsed, run_id)
    db.upsert_metro(conn, parsed)
    db.upsert_photos(conn, parsed)
    db.close_bucket(conn, bucket_id, claimed=len(offers), collected=len(offers), pages=1)
    db.finish_run(conn, run_id, "ok", offers_seen=len(offers), buckets_total=1,
                  buckets_underfilled=0, requests_made=1)
    return run_id


def scalar(conn, sql: str, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row[0] if row else None


def main() -> int:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    offer_id = str(payload["id"])

    with db.connect() as conn:
        cleanup(conn)
        try:
            print("1. Первый снимок")
            ingest(conn, payload, D1)
            check(scalar(conn, "select count(*) from core.offer where source=%s",
                         (SOURCE,)) == 1, "объявление попало в core.offer")
            check(scalar(conn, "select count(*) from core.offer_observation where source=%s",
                         (SOURCE,)) == 1, "наблюдение попало в панель")
            check(scalar(conn, "select count(*) from core.offer_metro where source=%s",
                         (SOURCE,)) == 3, "три станции метро сохранены отдельными строками")
            check(scalar(conn, "select first_seen_date from core.offer where source=%s",
                         (SOURCE,)) == D1, "first_seen_date = дата первого снимка")

            print("2. Повторный запуск в тот же день (идемпотентность)")
            ingest(conn, payload, D1)
            check(scalar(conn, "select count(*) from core.offer_observation where source=%s",
                         (SOURCE,)) == 1,
                  "повторный запуск не создал второе наблюдение за тот же день")
            check(scalar(conn, "select count(*) from raw.offer_snapshot where source=%s",
                         (SOURCE,)) == 2,
                  "а сырой слой при этом пополнился — он только добавляет")
            check(scalar(conn, "select count(*) from core.price_change where source=%s",
                         (SOURCE,)) == 0,
                  "и ложного изменения цены между двумя чтениями одних суток нет")

            print("3. Второй снимок: цена снижена с 95 000 до 88 000")
            cheaper = json.loads(json.dumps(payload))
            cheaper["bargainTerms"]["price"] = 88000.0
            ingest(conn, cheaper, D2)
            check(scalar(conn, "select count(*) from core.offer_observation where source=%s",
                         (SOURCE,)) == 2, "в панели два наблюдения одного объявления")
            with conn.cursor() as cur:
                cur.execute("""select prev_price, new_price, delta_rub, delta_pct,
                                      direction, days_between_observations
                               from core.price_change where source=%s""", (SOURCE,))
                change = cur.fetchone()
            check(change is not None, "изменение цены обнаружено")
            check(change[0] == 95000 and change[1] == 88000, "прежняя и новая цена верны")
            check(change[2] == -7000, "разница -7000 рублей")
            check(float(change[3]) == -7.37, "разница -7.37%")
            check(change[4] == "down", "направление — снижение")
            check(change[5] == 3, "интервал между наблюдениями — 3 дня, а не 1")

            print("4. Устойчивые поля не затираются пропуском в выдаче")
            stripped = json.loads(json.dumps(payload))
            stripped["totalArea"] = None
            stripped["bargainTerms"]["price"] = 88000.0
            ingest(conn, stripped, D2)
            # float(): numeric(8,2) приходит как Decimal('54.30'), и прямое
            # сравнение с 54.3 ложно — сравнивать надо в одном типе.
            check(float(scalar(conn, "select total_area from core.offer where source=%s",
                               (SOURCE,))) == 54.3,
                  "площадь сохранилась, хотя в снимке её не было")

            print("5. Объявление пропало из выдачи")
            # Одного пропуска мало: 5.5% объявлений исчезают на день-другой
            # из-за недобора обхода и возвращаются. Порог — три успешных
            # дня подряд без объявления.
            gone_days = [D2 + timedelta(days=i) for i in (1, 2, 3)]
            for i, day in enumerate(gone_days, start=1):
                run_id = db.start_run(conn, "narrow", day, source=SOURCE)
                db.insert_bucket(conn, run_id, Bucket(rooms="2"),
                                 {"проверка": day.isoformat()}, None)
                db.finish_run(conn, run_id, "ok", offers_seen=0, buckets_total=1,
                              buckets_underfilled=0, requests_made=1)
                affected = db.mark_disappeared(conn, day, ["2"], source=SOURCE)
                if i < 3:
                    check(affected == 0,
                          f"после {i} дня отсутствия объявление ещё не снято — "
                          "иначе недобор обхода обернулся бы тысячей мнимых снятий")
            with conn.cursor() as cur:
                cur.execute("""select is_active, disappeared_date, last_seen_date
                               from core.offer where source=%s""", (SOURCE,))
                is_active, disappeared, last_seen = cur.fetchone()
            check(is_active is False, "после трёх дней отсутствия объявление снято")
            check(disappeared == D2 + timedelta(days=1),
                  "дата снятия — первый день без объявления, а не день, когда заметили")
            check(last_seen == D2, "последний раз видели на предыдущем снимке")
            print("     (напоминание: это снятие с публикации, а НЕ сдача квартиры)")

            print("6. Объявление вернулось в выдачу")
            # День возвращения нужен и дальше, в проверке разметки
            # животных, поэтому он назван, а не посчитан дважды.
            back_day = D2 + timedelta(days=4)
            ingest(conn, payload, back_day)
            check(scalar(conn, "select is_active from core.offer where source=%s",
                         (SOURCE,)) is True, "снова активно")
            check(scalar(conn, "select disappeared_date from core.offer where source=%s",
                         (SOURCE,)) is None, "дата исчезновения снята")
            check(scalar(conn, "select first_seen_date from core.offer where source=%s",
                         (SOURCE,)) == D1,
                  "first_seen_date не сдвинулся — иначе срок жизни в анализе "
                  "выживаемости посчитался бы короче реального")

            print("7. Разметка животных переживает пересборку core из raw")
            # Проверка появилась после того, как пересборка молча стёрла
            # метку площадки у всех снимков: она жила только в core, а raw
            # считался самодостаточным. Теперь членство пишется в
            # raw.pets_hit, и это утверждение следит, чтобы так и осталось.
            last_run = scalar(conn, "select max(run_id) from raw.collection_run "
                                    "where source=%s", (SOURCE,))
            db.record_pets_hits(conn, last_run, None, back_day,
                                [offer_id], source=SOURCE)
            db.set_pets_pass_complete(conn, last_run, True)
            db.mark_pets_allowed(conn, [offer_id], back_day,
                                 source=SOURCE, mark_rest_false=True)
            check(scalar(conn, "select pets_platform_allowed from core.offer_observation "
                               "where source=%s and snapshot_date=%s",
                         (SOURCE, back_day)) is True,
                  "метка площадки проставлена")

            with conn.cursor() as cur:
                cur.execute("update core.offer_observation "
                            "set pets_platform_allowed = null, "
                            "    pets_state = 'unknown', pets_source = 'none' "
                            "where source = %s", (SOURCE,))
            conn.commit()
            restore_pets(conn, SOURCE)
            check(scalar(conn, "select pets_platform_allowed from core.offer_observation "
                               "where source=%s and snapshot_date=%s",
                         (SOURCE, back_day)) is True,
                  "и вернулась после пересборки — из raw.pets_hit, без запросов")
            check(scalar(conn, "select pets_state from core.offer_observation "
                               "where source=%s and snapshot_date=%s",
                         (SOURCE, back_day)) == "allowed",
                  "состояние животных восстановлено вместе с ней")

            print(f"\nСквозная проверка пройдена: {checks_passed} утверждений. "
                  f"Объявление {offer_id} удалено из базы.")
        finally:
            cleanup(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
