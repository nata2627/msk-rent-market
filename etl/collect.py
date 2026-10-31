"""Точка входа сбора.

    python -m etl.collect --mode full     # еженедельный полный обход Москвы
    python -m etl.collect --mode narrow   # ежедневный узкий срез, 1-2 комнаты

Обход рекурсивный. Для каждой корзины спрашиваем первую страницу, смотрим
заявленное площадкой число результатов и решаем: листать целиком или делить
корзину дальше. Всё, что успели забрать, сохраняется даже у разделённой
корзины — выбрасывать собранное незачем, слой raw только пополняется.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import date

from etl import config, db
from etl.browser_client import BrowserClient
from etl.cian_client import Blocked, CianClient, PageWithoutData
from etl.parse import parse_offer
from etl.partition import Bucket, root_buckets, split

log = logging.getLogger("collect")

# Сколько сбойных корзин терпеть, прежде чем признать происходящее
# блокировкой. Одна-две — обычные сетевые неприятности; десяток подряд
# означает, что площадка перестала отдавать данные, и продолжать незачем.
MAX_FAILED_BUCKETS = 10

DISTRICTS_CACHE = config.REFERENCE / "moscow_districts.json"


# ---------------------------------------------------------------------
def load_districts(client: CianClient, refresh: bool = False) -> list[dict]:
    """Справочник районов: из кеша или с площадки.

    Кешируется в файл, потому что состав районов Москвы меняется раз в
    несколько лет, а тратить на него запрос при каждом обходе — расточительно
    по отношению к площадке.
    """
    if DISTRICTS_CACHE.exists() and not refresh:
        return json.loads(DISTRICTS_CACHE.read_text(encoding="utf-8"))

    districts = client.districts_tree()
    if districts:
        DISTRICTS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        DISTRICTS_CACHE.write_text(
            json.dumps(districts, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    return districts


def initial_buckets(mode: str, districts: list[dict],
                    rooms: list[str] | None = None) -> list[Bucket]:
    # Явно заданная комнатность: добрать то, чего ещё нет за сегодня,
    # не гоняя весь обход заново. Дата снимка та же, поэтому уже
    # собранное не задваивается — первичный ключ панели не даст.
    if rooms:
        return [Bucket(rooms=code) for code in rooms]
    if mode == "narrow":
        # Узкий срез — это комнатность сегмента и вся Москва. Ни цены, ни
        # животных в запросе нет: сужать выборку по тому, что изучаем,
        # нельзя даже в ежедневном режиме. Отбор по площади, животным и
        # парку делается запросом к собранным данным, в marts.offer_segment.
        return [Bucket(rooms=code) for code in config.NARROW_ROOM_CODES]
    return root_buckets(districts)


# ---------------------------------------------------------------------
def run(mode: str, snapshot_date: date, max_requests: int | None,
        dry_run: bool, refresh_districts: bool, pets_pass: bool = True,
        transport: str | None = None, rooms: list[str] | None = None,
        resume: bool = False, resume_keys: set | None = None) -> int:
    # 'browser' — настоящий Chrome через Playwright. Понадобился потому,
    # что площадка отвечает заглушкой на запросы из Python, но нормально
    # отдаёт страницы браузеру с того же адреса.
    client = (BrowserClient() if transport == "browser"
              else CianClient(transport=transport))
    log.info("Транспорт: %s", client.transport)

    try:
        districts = load_districts(client, refresh=refresh_districts)
    except (Blocked, RuntimeError) as err:
        log.warning("Справочник районов не получен (%s). "
                    "Нарезка пойдёт по комнатности и цене — запросов будет больше, "
                    "но покрытие останется полным.", err)
        districts = []

    log.info("Районов в справочнике: %d", len(districts))

    conn_ctx = db.connect() if not dry_run else _NullConn()
    with conn_ctx as conn:
        run_id = db.start_run(conn, mode, snapshot_date) if not dry_run else -1
        log.info("Запуск #%s, режим %s, дата снимка %s", run_id, mode, snapshot_date)

        stack: list[tuple[Bucket, int | None]] = [
            (b, None) for b in reversed(initial_buckets(mode, districts, rooms))
        ]
        seen_offers: set[str] = set()
        pets_ids: set[str] = set()
        pets_complete = pets_pass
        pets_skipped = 0
        buckets_total = 0
        buckets_underfilled = 0
        buckets_resplit = 0
        buckets_failed = 0
        buckets_skipped = 0
        status, note = "ok", None

        # Продолжение прерванного обхода. Пока площадка отдаёт выдачу
        # рывками, обход падает через несколько минут, и без этого каждый
        # следующий заход заново перемалывает те же первые районы.
        #
        # Набор ключей можно передать готовым: откуда он взялся, обходу
        # безразлично, а проверять логику пропуска так можно без базы.
        if resume_keys is None and resume and not dry_run:
            resume_keys = db.collected_bucket_keys(conn, snapshot_date)
        if resume_keys:
            log.info("Продолжение: за %s уже собрано корзин %d, их пропущу",
                     snapshot_date, len(resume_keys))

        try:
            while stack:
                if max_requests and client.requests_made >= max_requests:
                    status, note = "aborted", f"достигнут предел в {max_requests} запросов"
                    log.warning(note)
                    break

                bucket, parent_id = stack.pop()

                if resume_keys is not None:
                    key = (bucket.district_id, bucket.rooms,
                           bucket.price_min, bucket.price_max)
                    if key in resume_keys:
                        buckets_skipped += 1
                        continue

                buckets_total += 1
                query = client.describe_query(bucket)
                bucket_id = (db.insert_bucket(conn, run_id, bucket, query, parent_id)
                             if not dry_run else buckets_total)

                try:
                    offers, claimed = client.search(bucket)
                except PageWithoutData as err:
                    # Корзина не прочиталась даже после повторов. Пропускаем
                    # её, но не молча: помечаем сбойной, считаем и идём
                    # дальше. Ронять из-за одной корзины весь обход — вот
                    # так и потерялись 34% прошлого прогона.
                    buckets_failed += 1
                    log.warning("Корзина %s не прочиталась: %s", bucket.label(), err)
                    if not dry_run:
                        db.close_bucket(conn, bucket_id, error=str(err)[:500])
                    if buckets_failed >= MAX_FAILED_BUCKETS:
                        status = "aborted"
                        note = (f"{buckets_failed} корзин подряд не читаются — "
                                f"похоже на блокировку")
                        log.error(note)
                        break
                    continue
                log.info("[%d] %s -> заявлено %d", buckets_total, bucket.label(), claimed)

                # Первую страницу сохраняем всегда, даже если корзину сейчас
                # разделим: она уже получена, и терять её незачем.
                collected = _store(conn, run_id, bucket_id, snapshot_date,
                                   offers, page=1, dry_run=dry_run)
                bucket_offer_ids = _ids(offers)
                seen_offers.update(bucket_offer_ids)

                if claimed == 0:
                    if not dry_run:
                        db.close_bucket(conn, bucket_id, claimed=0,
                                        collected=0, pages=1)
                    continue

                # Корзина упирается в предел выдачи — делим.
                if claimed > config.SPLIT_THRESHOLD:
                    children = split(bucket, districts)
                    if children:
                        if not dry_run:
                            db.close_bucket(conn, bucket_id, claimed=claimed,
                                            collected=collected, pages=1, was_split=True)
                        stack.extend((c, bucket_id) for c in reversed(children))
                        continue
                    # Делить нечем: оси кончились. Заберём сколько пустит и
                    # честно пометим корзину как усечённую — эта пометка
                    # попадёт в раздел ограничений README, а не потеряется.
                    log.warning("Корзина %s больше предела выдачи и не делится. "
                                "Будет усечена.", bucket.label())

                pages_needed = min(math.ceil(claimed / config.PAGE_SIZE), config.MAX_PAGE)
                page_failed = False
                for page in range(2, pages_needed + 1):
                    if max_requests and client.requests_made >= max_requests:
                        break
                    try:
                        page_offers, _ = client.search(bucket, page=page)
                    except PageWithoutData as err:
                        # Страница в середине корзины не далась. Раньше это
                        # исключение летело наружу и роняло весь обход: день
                        # 17.10.2026 собрал нормальные 16 161 объявление и
                        # всё равно закрылся как failed из-за одной страницы
                        # на сороковой корзине, а значит не покрыл сутки и
                        # выпал из расчёта снятий.
                        #
                        # Правильно — бросить эту корзину, но не обход:
                        # собранное уже записано, недобор посчитается сам
                        # и при большой разнице включит переделение.
                        log.warning("Корзина %s: страница %d не далась (%s). "
                                    "Листание прерываю, собранное оставляю.",
                                    bucket.label(), page, err)
                        page_failed = True
                        break
                    if not page_offers:
                        break
                    collected += _store(conn, run_id, bucket_id, snapshot_date,
                                        page_offers, page=page, dry_run=dry_run)
                    bucket_offer_ids |= _ids(page_offers)
                    seen_offers.update(_ids(page_offers))

                if page_failed:
                    # Пометка нужна, чтобы корзину перепросили: без неё она
                    # попала бы в набор продолжения как собранная, и недобор
                    # закрепился бы на весь день.
                    buckets_failed += 1
                    if not dry_run:
                        db.close_bucket(conn, bucket_id, claimed=claimed,
                                        collected=collected, pages=page,
                                        error="листание прервано: страница не далась")
                    continue

                truncated = claimed > config.BUCKET_CAPACITY
                missing = claimed - collected

                # Корзина отдала заметно меньше обещанного — делим и
                # проходим заново.
                #
                # Решение делить принимается до листания, по заявленному
                # числу, и этого мало: 04.10.2026 корзина четырёхкомнатных
                # заявила 1231, в порог уложилась и отдала 633. Листание
                # прошло без единой ошибки, страницы были не пустые —
                # площадка просто повторяла уже показанное. Поймать такое
                # можно только задним числом, по разнице обещанного и
                # собранного. Пороги и таблица, из которой они взяты, —
                # в config.RESPLIT_RATIO.
                #
                # Собранное не выбрасывается и не теряется: оно уже
                # записано, а ключ панели не даст задвоить его детьми.
                #
                # Усечённые корзины сюда не попадают: у них недобор
                # объясняется пределом выдачи, они уже помечены, и делить
                # их бесполезно — делить было нечем, иначе бы поделили.
                if (not truncated
                        and missing >= config.RESPLIT_MIN_MISSING
                        and collected < claimed * config.RESPLIT_RATIO):
                    children = split(bucket, districts)
                    if children:
                        log.warning("Корзина %s отдала %d из %d (%.0f%%) — "
                                    "делю и прохожу заново",
                                    bucket.label(), collected, claimed,
                                    100 * collected / claimed)
                        buckets_resplit += 1
                        if not dry_run:
                            db.close_bucket(conn, bucket_id, claimed=claimed,
                                            collected=collected, pages=pages_needed,
                                            was_split=True,
                                            split_after_underfill=True)
                        stack.extend((c, bucket_id) for c in reversed(children))
                        continue
                    # Делить нечем — остаётся честно признать недобор.
                    log.warning("Корзина %s отдала %d из %d и не делится — "
                                "недобор остаётся", bucket.label(), collected, claimed)

                if collected < claimed and not truncated:
                    buckets_underfilled += 1
                if not dry_run:
                    db.close_bucket(conn, bucket_id, claimed=claimed,
                                    collected=collected, pages=pages_needed,
                                    is_truncated=truncated)

                # Второй проход по той же корзине — с фильтром площадки
                # «можно с животными». Объявления уже собраны, нужен только
                # список тех, кого площадка относит к разрешающим.
                #
                # Корзину, где все объявления классифицированы раньше,
                # пропускаем: разрешение держать животных — свойство
                # квартиры, а не дня, и повторный запрос по ней ничего не
                # добавит. Известные метки переносятся на сегодняшний день
                # отдельно, после обхода. На ежедневном прогоне это
                # экономит пятую часть времени: новых объявлений в день —
                # единицы процентов.
                if pets_pass and collected:
                    fresh = (db.offers_without_pets_verdict(conn, sorted(bucket_offer_ids))
                             if not dry_run else len(bucket_offer_ids))
                    if fresh == 0:
                        log.info("    с животными: пропуск, все %d уже известны",
                                 len(bucket_offer_ids))
                        pets_skipped += 1
                    else:
                        try:
                            ok, ids = _pets_pass(conn, client, run_id, bucket,
                                                 snapshot_date, claimed,
                                                 seen_offers, dry_run)
                        except PageWithoutData as err:
                            # Страница фильтра не далась. Это сбой ОДНОЙ
                            # корзины, а не всего обхода: 27.09.2026 такая
                            # ошибка остановила дневной сбор на третьей
                            # корзине из почти трёхсот, и вместо 16 тысяч
                            # объявлений записались 111.
                            #
                            # Разметка животных за день при этом считается
                            # неполной: пометить необойдённые объявления
                            # как «животные не разрешены» значило бы выдать
                            # собственный промах за факт о рынке.
                            log.warning("    с животными: корзина не далась (%s)", err)
                            buckets_failed += 1
                            pets_complete = False
                        else:
                            pets_ids.update(ids)
                            pets_complete &= ok

        except Blocked as err:
            # Площадка закрыла доступ. Не ускоряемся, не меняем отпечаток,
            # не идём через прокси — останавливаемся. Всё, что собрано до
            # этого момента, уже лежит в базе и не теряется.
            status, note = "aborted", str(err)
            log.error("%s", err)
        except KeyboardInterrupt:
            status, note = "aborted", "прервано вручную"
            log.warning(note)
        except Exception as err:                      # noqa: BLE001
            # Обход идёт полтора часа через браузер, и упасть он может по
            # причинам, к площадке не относящимся: браузер закрыли, кончилась
            # память, оборвалась сеть. Без этой ветки запуск навсегда остался
            # бы в статусе "running", а собранное до сбоя выглядело бы как
            # незавершённый обход неизвестной полноты.
            status = "failed"
            note = f"{type(err).__name__}: {err}"
            log.exception("Непредвиденная ошибка, обход остановлен")

        # Обход, где не прочиталась ни одна корзина, успешным не считается.
        # Иначе день попадает в покрытие: прогон 149 от 12.10.2026 закрылся
        # как 'ok' с двумя корзинами, обе сбойные, ноль объявлений — и
        # 12.10 стал «собранным днём» для одно- и двухкомнатных. Порог
        # MAX_FAILED_BUCKETS тут не спасает: он ловит десять сбоев подряд,
        # а корзин в узком срезе всего две.
        if status == "ok" and buckets_total > 0 and buckets_failed == buckets_total:
            status = "failed"
            note = (f"ни одна из {buckets_total} корзин не прочиталась — "
                    f"собирать было нечего")
            log.error(note)

        if not dry_run and pets_pass and status == "ok":
            db.set_pets_pass_complete(conn, run_id, pets_complete)
            marked = db.mark_pets_allowed(conn, sorted(pets_ids), snapshot_date,
                                          mark_rest_false=pets_complete)
            # Порядок важен: сначала метки сегодняшнего прохода, потом
            # перенос известных. Иначе перенесённые значения были бы тут же
            # затёрты обнулением «всех остальных».
            inherited = db.inherit_pets(conn, snapshot_date)
            log.info("Животные: помечено проходом %d, унаследовано %d, "
                     "корзин пропущено %d", marked, inherited, pets_skipped)

        if not dry_run:
            db.finish_run(conn, run_id, status,
                          buckets_total=buckets_total,
                          buckets_underfilled=buckets_underfilled,
                          offers_seen=len(seen_offers),
                          requests_made=client.requests_made,
                          note=note)

            # Исчезновения фиксируются только по ПОЛНОМУ обходу: узкий срез
            # смотрит в один сегмент, и всё остальное «пропало» бы просто
            # потому, что мы туда не заглядывали.
            #
            # Отмечаются только те комнатности, которые обход собирал.
            #
            # Раньше здесь стояло `mode == "full" and rooms is None`, и
            # отметка не ставилась ни разу: обхода всех комнатностей
            # сразу в проекте не случилось. Узкий срез покрывает одно- и
            # двухкомнатные целиком, и вывод об их исчезновении столь же
            # законен; а вот записать в снятые трёхкомнатные, на которые
            # не смотрели, нельзя — это и защищает список комнатностей.
            if status == "ok":
                collected_rooms = rooms or (config.NARROW_ROOM_CODES
                                            if mode == "narrow"
                                            else config.ROOM_CODES)
                gone = db.mark_disappeared(conn, snapshot_date, collected_rooms)
                log.info("Перестали появляться в выдаче: %d объявлений "
                         "(это снятие с публикации, а не сдача квартиры)", gone)
            # Витрины здесь больше не пересобираются: с 27.10.2026 их
            # строит dbt отдельным шагом прогона (make marts на макбуке,
            # scripts/box_marts.sh на боксе), и у него свое окружение.

        log.info("Итог: корзин %d, пропущено собранных %d, недобрано в %d, "
                 "переделено по недобору %d, не прочиталось %d, "
                 "уникальных объявлений %d, запросов %d, статус %s",
                 buckets_total, buckets_skipped, buckets_underfilled,
                 buckets_resplit, buckets_failed, len(seen_offers),
                 client.requests_made, status)

    # После Ctrl-C посреди вызова playwright его синхронная обвязка сломана:
    # context.close() крутится вечно на полном CPU. 30.10.2026 так процесс
    # провисел четыре с половиной часа, держал daily.lock, и хвост дня не
    # запустился. Без close процесс выходит сразу, браузер гаснет вместе с
    # драйвером; всё нужное в базе уже записано finish_run.
    if hasattr(client, "close") and note != "прервано вручную":
        client.close()
    return 0 if status == "ok" else 1


# ---------------------------------------------------------------------
def _pets_pass(conn, client, run_id, bucket, snapshot_date, claimed_total,
               seen_offers: set[str], dry_run: bool) -> tuple[bool, set[str]]:
    """Обойти корзину повторно с фильтром «можно с животными».

    Возвращает (получилось ли, множество идентификаторов). Первый элемент
    важен: если фильтр не сузил выдачу, значит он не применился, и пометить
    остальные объявления как «не разрешено» было бы враньём — мы бы выдали
    собственный промах за факт о рынке.

    Payload объявлений повторно не сохраняется: он байт в байт тот же, что
    уже лежит в raw после основного прохода. Исключение — объявления,
    которых в основном проходе не было: они могли появиться в промежутке
    между двумя запросами, и их снимок сохраняется.
    """
    query = client.describe_query(bucket, pets_only=True)
    bucket_id = (db.insert_bucket(conn, run_id, bucket, query, None, pets_filter=True)
                 if not dry_run else None)

    offers, claimed = client.search(bucket, pets_only=True)

    # Проверка, что фильтр вообще сработал.
    if claimed >= claimed_total:
        log.warning("Фильтр животных не сузил корзину %s (%d против %d). "
                    "Разметка по этой корзине пропущена.",
                    bucket.label(), claimed, claimed_total)
        if not dry_run:
            db.close_bucket(conn, bucket_id, claimed=claimed, collected=0, pages=1,
                            error="фильтр животных не сузил выдачу")
        return False, set()

    ids = _ids(offers)
    _store_new(conn, run_id, bucket_id, snapshot_date, offers, 1, seen_offers, dry_run)

    pages_needed = min(math.ceil(claimed / config.PAGE_SIZE), config.MAX_PAGE)
    for page in range(2, pages_needed + 1):
        page_offers, _ = client.search(bucket, page=page, pets_only=True)
        if not page_offers:
            break
        ids.update(_ids(page_offers))
        _store_new(conn, run_id, bucket_id, snapshot_date, page_offers, page,
                   seen_offers, dry_run)

    if not dry_run:
        db.close_bucket(conn, bucket_id, claimed=claimed, collected=len(ids),
                        pages=pages_needed)
        # Членство в выдаче под фильтром — единственное, что этот проход
        # добавляет к raw. Без него пересборка core теряет разметку.
        db.record_pets_hits(conn, run_id, bucket_id, snapshot_date, sorted(ids))
    log.info("    с животными: %d из %d", len(ids), claimed_total)
    return True, ids


def _store_new(conn, run_id, bucket_id, snapshot_date, offers, page,
               seen_offers: set[str], dry_run: bool) -> None:
    """Сохранить только те объявления, которых основной проход не видел."""
    fresh = [o for o in offers if str(o.get("id")) not in seen_offers]
    if fresh:
        _store(conn, run_id, bucket_id, snapshot_date, fresh, page, dry_run)
        seen_offers.update(_ids(fresh))


def _ids(offers: list[dict]) -> set[str]:
    return {str(o["id"]) for o in offers if o.get("id") is not None}


def _store(conn, run_id, bucket_id, snapshot_date, offers, page, dry_run) -> int:
    if not offers:
        return 0
    if dry_run:
        return len(offers)

    db.save_snapshots(conn, run_id, bucket_id, snapshot_date, offers, page)
    parsed = [p for p in (parse_offer(o, snapshot_date) for o in offers) if p]
    db.upsert_offers(conn, parsed, snapshot_date)
    db.upsert_observations(conn, parsed, run_id)
    db.upsert_metro(conn, parsed)
    db.upsert_photos(conn, parsed)
    return len(offers)


class _NullConn:
    """Заглушка соединения для --dry-run: проверить нарезку без записи в базу."""

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Сбор объявлений об аренде с ЦИАНа")
    parser.add_argument("--mode", choices=["full", "narrow"], default="narrow")
    parser.add_argument("--snapshot-date", default=None,
                        help="дата снимка, по умолчанию сегодня")
    parser.add_argument("--max-requests", type=int, default=None,
                        help="предохранитель: остановиться после N запросов")
    parser.add_argument("--dry-run", action="store_true",
                        help="не писать в базу — проверить нарезку и доступность")
    parser.add_argument("--refresh-districts", action="store_true",
                        help="перечитать справочник районов с площадки")
    parser.add_argument("--transport", choices=["html", "api", "browser"], default=None,
                        help="browser — настоящий Chrome (работает, когда "
                             "площадка закрыта для Python), html — публичная "
                             "страница, api — внутренний эндпоинт")
    parser.add_argument("--rooms", default=None,
                        help="через запятую, например 3,4,5,6,7,9 — собрать "
                             "только эти комнатности")
    parser.add_argument("--resume", action="store_true",
                        help="продолжить прерванный обход: пропустить корзины, "
                             "уже собранные за эту дату снимка")
    parser.add_argument("--no-pets-pass", action="store_true",
                        help="не делать второй проход с фильтром «можно с животными». "
                             "Запросов будет меньше, но про животных придётся "
                             "судить только по тексту описания")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    snapshot = (date.fromisoformat(args.snapshot_date)
                if args.snapshot_date else date.today())
    return run(args.mode, snapshot, args.max_requests,
               args.dry_run, args.refresh_districts,
               pets_pass=not args.no_pets_pass, transport=args.transport,
               rooms=[r.strip() for r in args.rooms.split(",")] if args.rooms else None,
               resume=args.resume)


if __name__ == "__main__":
    sys.exit(main())
