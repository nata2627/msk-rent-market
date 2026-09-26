"""Сбор исторических объявлений из веб-архива.

    python -m etl.wayback --limit 30

Зачем. Собственной истории у проекта нет и взяться ей неоткуда: площадки
отдают только срез «прямо сейчас». Но страницы выдачи ЦИАНа попадали в
веб-архив, а страница выдачи несёт с собой полное состояние приложения —
те же карточки по 137 полей, что отдаёт живой сайт. Значит из архива
достаются НАСТОЯЩИЕ объявления с ценами за прошлые годы.

И главное: снимки сделаны в разные даты. Одно и то же объявление,
встреченное в снимках за сентябрь и за январь, даёт две точки панели —
то есть реальное изменение цены, которое иначе не восстановить ничем.

Чего этот источник НЕ даёт, и это важно записать сразу:

* выборка — это то, что кто-то когда-то заархивировал, а не рынок.
  Архивируются случайные поисковые запросы живых людей, часто с фильтрами
  (апартаменты, конкретная улица, конкретное метро). Считать по архиву
  медианы по рынку нельзя;
* покрытие рваное: месяцы без единого снимка — норма;
* дата снимка это дата архивирования, а не дата, когда объявление
  появилось или исчезло. Отсутствие объявления в снимке ничего не значит:
  снимок мог быть сделан по другому запросу.

Поэтому источник пишется отдельно как 'cian_wayback' и в аналитике не
смешивается с собранным самостоятельно.
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import random
import sys
import time
import urllib.parse
from datetime import date, datetime
from pathlib import Path

import requests

from etl import config, db
from etl.parse import parse_offer
from etl.serp_html import SerpNotFound, extract_offers

log = logging.getLogger("wayback")

SOURCE = "cian_wayback"
CDX_URL = "http://web.archive.org/cdx/search/cdx"
# id_ отдаёт исходный ответ без служебной обвязки архива: без панели
# навигации и без переписанных ссылок. Разбор состояния страницы работает
# только с ним — обычная версия ломает встроенный JavaScript.
REPLAY_URL = "https://web.archive.org/web/{timestamp}id_/{url}"

CACHE = config.DATA / "raw" / "wayback"

# Архив — общественный ресурс, и ведём себя с ним так же вежливо, как с
# площадкой: один поток, пауза с джиттером.
PAUSE_MIN, PAUSE_MAX = 2.0, 4.0
RETRIES = 3
BACKOFF_BASE = 10.0

# Сколько неудач подряд считать отказом. Первая версия этого не имела и при
# отказе архива в соединении неслась дальше без пауз, заваливая его
# запросами, — ровно то, за что стоит ругать чужие парсеры.
MAX_CONSECUTIVE_FAILURES = 5


class ArchiveUnavailable(RuntimeError):
    """Архив перестал отвечать. Продолжать нельзя — только ждать."""


# ---------------------------------------------------------------------
CDX_PAGE_LIMIT = 20_000


def _cdx_window(frm: str, to: str) -> list[list[str]]:
    """Снимки выдачи за окно дат. Пустой список, если архив не осилил.

    Отбор делается на нашей стороне, а не фильтрами CDX: несколько
    регулярных выражений подряд архив не тянет и отвечает 503.
    """
    params = {
        "url": "cian.ru/cat.php*",
        "output": "json",
        "from": frm,
        "to": to,
        "filter": ["statuscode:200", "mimetype:text/html"],
        "limit": CDX_PAGE_LIMIT,
        "fl": "timestamp,original,length",
    }
    # Индекс архива отвечает не всегда и не целиком: густые окна он
    # считает дольше таймаута, а иногда обрывает выдачу на середине JSON.
    # Повторы с отступлением — иначе обход падает на случайном месяце,
    # уже скачав половину.
    for attempt in range(RETRIES):
        try:
            resp = requests.get(CDX_URL, params=params, timeout=180)
            resp.raise_for_status()
            rows = resp.json()
            data = rows[1:] if rows and rows[0][0] == "timestamp" else rows
            return data
        except (requests.RequestException, json.JSONDecodeError) as err:
            log.warning("%s..%s: индекс архива не ответил (%s), попытка %d из %d",
                        frm, to, type(err).__name__, attempt + 1, RETRIES)
            time.sleep(BACKOFF_BASE * (2 ** attempt))
    return []


def _cdx_month(year: int, month: int) -> list[list[str]]:
    """Снимки за календарный месяц, при нужде — по декадам.

    Густой месяц архив отдаёт с обрывом или не отдаёт вовсе, поэтому при
    неудаче окно дробится на трети. Дробление, а не отказ: ноябрь 2025 —
    самый насыщенный месяц панели, и терять его молча нельзя.
    """
    data = _cdx_window(f"{year}{month:02d}01", f"{year}{month:02d}31")
    if data and len(data) < CDX_PAGE_LIMIT:
        return data

    if not data:
        log.warning("%04d-%02d целиком не дался, беру по декадам", year, month)
    else:
        # Молча обрезанный ответ — худший исход: выглядит как полный.
        log.warning("%04d-%02d упёрся в предел в %d записей, беру по декадам",
                    year, month, CDX_PAGE_LIMIT)

    out: list[list[str]] = []
    for first, last in (("01", "10"), ("11", "20"), ("21", "31")):
        part = _cdx_window(f"{year}{month:02d}{first}", f"{year}{month:02d}{last}")
        if not part:
            log.warning("%04d-%02d, дни %s-%s: пусто или не отдалось — "
                        "снимки этого окна в счёт не вошли", year, month, first, last)
        out.extend(part)
        time.sleep(random.uniform(0.5, 1.2))
    return out


def find_snapshots(year_from: int = 2022, year_to: int = 2026,
                   limit: int = 4000, months: set[str] | None = None) -> list[dict]:
    """Снимки страниц выдачи длительной аренды квартир в Москве.

    Индекс архива запрашивается ПОМЕСЯЧНО, а не одним запросом на все
    годы. Причина дорогая: CDX отдаёт не больше запрошенного предела и
    молча обрезает остальное, никак об этом не сообщая. Одним запросом
    за 2025-2026 возвращалось ровно столько строк, сколько указано в
    limit, — и сколько бы ни поставить, ответ упирался в предел.
    Выглядело это как полная картина: команда --list уверенно печатала
    «снимков длительной аренды по Москве: 210», тогда как за один только
    ноябрь 2025 их 379. Помесячные окна в предел не упираются, и счёт
    становится настоящим.

    Заданные месяцы отсекаются ДО обращения к архиву, а не после.
    Отбор после стоил дорого: чтобы взять сентябрь-декабрь, обход честно
    выгребал ещё и май с июнем — самые густые месяцы года, по пять-семь
    тысяч снимков, — и выбрасывал их.
    """
    out = []
    for year in range(year_from, year_to + 1):
        for month in range(1, 13):
            if months and f"{year}{month:02d}" not in months:
                continue
            out.extend(_keep_moscow_long_term(_cdx_month(year, month)))
            # Пауза между окнами: запросов стало двенадцать на год вместо
            # одного, и архив — общественный ресурс.
            time.sleep(random.uniform(0.5, 1.2))
    return out


def _keep_moscow_long_term(data) -> list[dict]:
    out = []
    for timestamp, url, length in data:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if params.get("deal_type", [""])[0] != "rent":
            continue
        if params.get("offer_type", [""])[0] != "flat":
            continue
        if params.get("type", [""])[0] != "4":        # 4 — длительная аренда
            continue
        region = params.get("region", params.get("location[0]", ["1"]))[0]
        if region != "1":                             # Москва
            continue
        out.append({
            "timestamp": timestamp,
            "url": url,
            "length": int(length),
            "params": {k: v for k, v in params.items()},
        })
    return out


def snapshot_date(timestamp: str) -> date:
    """20250614123350 -> дата снимка."""
    return datetime.strptime(timestamp[:8], "%Y%m%d").date()


# ---------------------------------------------------------------------
def fetch(snapshot: dict, use_cache: bool = True) -> str | None:
    """Скачать архивную страницу. Кешируется на диск.

    Кеш не оптимизация, а правило приличия: повторный запуск не должен
    заново тянуть с архива сотни мегабайт того, что уже скачано.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{snapshot['timestamp']}.html.gz"

    if use_cache and path.exists():
        return gzip.decompress(path.read_bytes()).decode("utf-8", "replace")

    url = REPLAY_URL.format(timestamp=snapshot["timestamp"], url=snapshot["url"])

    resp = None
    for attempt in range(RETRIES):
        try:
            resp = requests.get(url, timeout=120,
                                headers={"User-Agent": config.USER_AGENT})
        except requests.RequestException as err:
            # Пауза делается и при неудаче тоже. Без неё цикл повторов
            # превращается в обстрел: именно так первая версия и довела
            # архив до отказа в соединении.
            wait = BACKOFF_BASE * (2 ** attempt)
            log.warning("Снимок %s не скачался (%s), пауза %.0f с",
                        snapshot["timestamp"], type(err).__name__, wait)
            time.sleep(wait)
            resp = None
            continue

        if resp.status_code in (429, 503, 504) or resp.status_code >= 500:
            wait = BACKOFF_BASE * (2 ** attempt)
            log.warning("Снимок %s: HTTP %s, пауза %.0f с",
                        snapshot["timestamp"], resp.status_code, wait)
            time.sleep(wait)
            resp = None
            continue
        break

    if resp is None:
        return None
    if resp.status_code != 200:
        log.info("Снимок %s: HTTP %s, пропускаем", snapshot["timestamp"], resp.status_code)
        time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))
        return None

    raw = resp.content
    # Архив отдаёт исходный ответ как есть, включая исходное сжатие.
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)

    path.write_bytes(gzip.compress(raw))
    time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))
    return raw.decode("utf-8", "replace")


# ---------------------------------------------------------------------
def harvest(snapshots: list[dict], dry_run: bool = False,
            use_cache: bool = True) -> dict:
    stats = {"snapshots": 0, "empty": 0, "offers": 0, "unique": set(), "dates": set()}

    conn_ctx = db.connect() if not dry_run else _Null()
    with conn_ctx as conn:
        run_id = db.start_run(conn, "archive", date.today(), source=SOURCE) if not dry_run else -1

        consecutive_failures = 0
        for snap in snapshots:
            html = fetch(snap, use_cache=use_cache)
            stats["snapshots"] += 1
            if not html:
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log.error("%d неудач подряд — архив не отвечает. Останавливаемся.",
                              consecutive_failures)
                    if not dry_run:
                        db.finish_run(conn, run_id, "aborted",
                                      buckets_total=stats["snapshots"],
                                      buckets_underfilled=stats["empty"],
                                      offers_seen=len(stats["unique"]),
                                      requests_made=stats["snapshots"],
                                      note="архив перестал отвечать")
                    return stats
                continue
            consecutive_failures = 0

            try:
                offers, total = extract_offers(html)
            except SerpNotFound as err:
                log.info("  %s: состояния выдачи нет (%s)", snap["timestamp"], err)
                stats["empty"] += 1
                continue

            if not offers:
                stats["empty"] += 1
                continue

            when = snapshot_date(snap["timestamp"])
            stats["offers"] += len(offers)
            stats["unique"].update(str(o["id"]) for o in offers if o.get("id"))
            stats["dates"].add(when)
            log.info("  %s (%s): %d объявлений, заявлено %s",
                     snap["timestamp"], when, len(offers), total)

            if dry_run:
                continue

            bucket_id = _record_bucket(conn, run_id, snap, len(offers), total)
            db.save_snapshots(conn, run_id, bucket_id, when, offers, page=1, source=SOURCE)
            parsed = [p for p in (parse_offer(o, when, source=SOURCE) for o in offers) if p]
            db.upsert_offers(conn, parsed, when)
            db.upsert_observations(conn, parsed, run_id)
            db.upsert_metro(conn, parsed)
            db.upsert_photos(conn, parsed)

        if not dry_run:
            db.finish_run(conn, run_id, "ok",
                          buckets_total=stats["snapshots"],
                          buckets_underfilled=stats["empty"],
                          offers_seen=len(stats["unique"]), requests_made=stats["snapshots"],
                          note=f"веб-архив, дат: {len(stats['dates'])}")
    return stats


def _record_bucket(conn, run_id: int, snap: dict, collected: int, claimed) -> int:
    """Снимок как корзина: сохраняем адрес, чтобы смещение было прослеживаемо.

    В архив попадают чужие поисковые запросы, часто с фильтрами — по
    апартаментам, улице, станции метро. Без записанного адреса потом не
    объяснить, почему выборка выглядит именно так.
    """
    from etl.partition import Bucket

    bucket_id = db.insert_bucket(conn, run_id, Bucket(), {"archive": snap["params"]}, None)
    with conn.cursor() as cur:
        cur.execute(
            """update raw.bucket
               set archive_url = %s, archive_timestamp = %s,
                   claimed_count = %s, collected_count = %s, pages_fetched = 1
               where bucket_id = %s""",
            (snap["url"], snap["timestamp"],
             int(claimed or 0), collected, bucket_id),
        )
    conn.commit()
    return bucket_id


class _Null:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Сбор исторических объявлений из веб-архива")
    parser.add_argument("--limit", type=int, default=30,
                        help="сколько снимков обработать (страницы крупные)")
    parser.add_argument("--year-from", type=int, default=2022)
    parser.add_argument("--year-to", type=int, default=2026)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--months", default=None,
                        help="через запятую, например 202409,202410. "
                             "Панель растёт не от числа снимков, а от того, "
                             "встречается ли одно объявление в разные даты, — "
                             "поэтому выгоднее брать плотный куст соседних "
                             "месяцев, чем разрозненные снимки за годы")
    parser.add_argument("--list", action="store_true",
                        help="только показать, что нашлось, и выйти")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    wanted = {m.strip() for m in args.months.split(",")} if args.months else None
    snapshots = find_snapshots(args.year_from, args.year_to, months=wanted)
    log.info("Снимков длительной аренды по Москве: %d", len(snapshots))

    if args.list:
        from collections import Counter
        by_month = Counter(s["timestamp"][:6] for s in snapshots)
        for month, n in sorted(by_month.items()):
            log.info("  %s-%s: %d", month[:4], month[4:], n)
        return 0

    if wanted:
        log.info("Месяцы %s, снимков: %d", sorted(wanted), len(snapshots))

    # Крупные страницы первыми: на них больше объявлений, а значит больше
    # шансов встретить одно и то же объявление в снимках за разные даты —
    # ради чего архив и нужен.
    snapshots.sort(key=lambda s: -s["length"])
    stats = harvest(snapshots[:args.limit], args.dry_run, use_cache=not args.no_cache)

    log.info("Обработано снимков: %d, без данных: %d", stats["snapshots"], stats["empty"])
    log.info("Объявлений всего: %d, уникальных: %d, дат наблюдения: %d",
             stats["offers"], len(stats["unique"]), len(stats["dates"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
