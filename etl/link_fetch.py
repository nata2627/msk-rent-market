"""Одно объявление по ссылке из бота: открыть страницу, сохранить, посчитать балл.

    python -m etl.link_fetch 336070353

Зачем. По ссылке бот показывает карточку из базы, а объявления, вышедшего
после утреннего обхода, в базе нет до завтра. Тогда страница открывается
здесь, в настоящем Chrome, как при обходе (etl/browser_client.py), только
в своем профиле data/browser_profile_link. Второй браузер в профиле
обхода сломал бы сессию ему, а в своем профиле он обходу не мешает, и
ждать конца обхода не нужно.

В панель объявление не попадает. Страница ложится в raw.link_snapshot,
а не в raw.offer_snapshot, из которого пересобирается core: панель это
то, что видит обход, и случайная находка не должна менять «новых за
сутки» и срок жизни. Балл при этом тот же, что у всех: в одной
транзакции объявление записывается в core функциями обхода, по нему
считаются модели dbt offer_condition и offer_scored прямо из их файлов,
и транзакция откатывается. Второго определения балла нет.

Печатает одну строку JSON: status (ok, not_found, not_rent, blocked,
error) и, при ok, поля карточки в порядке bot.cards.CARD_COLS.
"""
from __future__ import annotations

import json
import logging
import random
import re
import sys
import time
from datetime import date
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from etl import config, db, parks
from etl.browser_client import BrowserClient
from etl.parse import parse_offer
from etl.privacy import scrub_payload
from etl.serp_html import SerpNotFound, extract_offer_card

log = logging.getLogger("link")

ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = config.DATA / "browser_profile_link"
OFFER_URL = "https://www.cian.ru/rent/flat/{}/"

# Поля страницы, которых нет у объявления в выдаче и которые ничего не
# говорят о квартире: служебные ссылки, разметка звонков, правка автором.
PAGE_NOISE = ("edit", "editPhoto", "newEdit", "exportLinks", "recoveryLink", "trackingData",
              "callTrackingProvider", "isEnabledCallTracking", "valueAddedServices",
              "promotion", "certificate", "similarOffersLink", "externalOfferUrl")

# Что значит repairType на странице. В выдаче поля нет, балл считается
# по словам описания (offer_condition), а это строка для человека рядом.
REPAIR = {"design": "дизайнерский", "euro": "евроремонт", "cosmetic": "косметический",
          "no": "без ремонта"}


# ---------------------------------------------------------------------
# Страница -> объявление в виде выдачи
def card_payload(data: dict, lines: dict[str, tuple]) -> dict:
    """offerData страницы -> объявление с полями выдачи для etl.parse.

    Отличий три. У станций метро на странице travelType и travelTime
    вместо transportType и time, а ветки и признака МЦК с диаметрами нет
    вовсе: они берутся из выдачи по id станции (lines). Автор лежит рядом
    с объявлением в agent, а не внутри в user. Адреса страницы в выдаче
    нет, он строится по номеру.
    """
    offer = {k: v for k, v in data["offer"].items() if k not in PAGE_NOISE}
    oid = offer.get("cianId") or offer.get("id")
    offer["fullUrl"] = OFFER_URL.format(oid)
    geo = dict(offer.get("geo") or {})
    stations = []
    for u in geo.get("undergrounds") or []:
        line_id, line_type = lines.get(str(u.get("id")), (None, None))
        stations.append({"id": u.get("id"), "name": u.get("name"), "time": u.get("travelTime"),
                         "transportType": u.get("travelType"), "lineId": line_id,
                         "lineType": line_type, "lineColor": u.get("lineColor"),
                         "underConstruction": u.get("underConstruction")})
    geo["undergrounds"] = stations
    offer["geo"] = geo
    agent = data.get("agent") or {}
    # Стаж на странице словами, «с 2004 года», в выдаче годом.
    since = re.search(r"\d{4}", str(agent.get("experience") or ""))
    offer["user"] = {"userType": agent.get("userType"), "accountType": agent.get("accountType"),
                     "cianUserId": agent.get("cianUserId"),
                     "experience": since.group(0) if since else None,
                     "isPassportVerified": agent.get("isPassportVerified")}
    return offer


def station_lines(conn, ids: list[str]) -> dict[str, tuple]:
    """Ветка и тип линии станций по последним снимкам выдачи."""
    if not ids:
        return {}
    with conn.cursor() as cur:
        cur.execute("""
            select distinct on (u->>'id') u->>'id', u->>'lineId', u->>'lineType'
              from raw.offer_snapshot s,
                   jsonb_array_elements(s.payload->'geo'->'undergrounds') u
             where s.source = 'cian' and s.payload is not null
               and s.snapshot_date >= current_date - 7
               and u->>'id' = any(%s)
             order by u->>'id', s.snapshot_date desc, s.snapshot_id desc""", (ids,))
        return {i: (int(line) if line and line.isdigit() else line, kind)
                for i, line, kind in cur.fetchall()}


# ---------------------------------------------------------------------
# Балл: модели dbt над объявлением, записанным в откатываемой транзакции
DBT_MODELS = ROOT / "dbt" / "models" / "marts"
CONFIG = re.compile(r"\{\{\s*config\(.*?\)\s*\}\}", re.S)
REF = re.compile(r"\{\{\s*ref\(\s*'(\w+)'\s*\)\s*\}\}")
SOURCE = re.compile(r"\{\{\s*source\(\s*'(\w+)'\s*,\s*'(\w+)'\s*\)\s*\}\}")


def render(model: str, inline: dict[str, str] | None = None) -> str:
    """SQL модели dbt без dbt: config убирается, ref это marts.имя или
    подставленный текст, source это схема.таблица. Других конструкций
    шаблона в моделях балла нет, а если появятся, запрос упадет, а не
    посчитает молча другое."""
    sql = CONFIG.sub("", (DBT_MODELS / f"{model}.sql").read_text())
    sql = REF.sub(lambda m: (inline or {}).get(m.group(1), f"marts.{m.group(1)}"), sql)
    sql = SOURCE.sub(lambda m: f"{m.group(1)}.{m.group(2)}", sql)
    if "{{" in sql or "{%" in sql:
        raise RuntimeError(f"В модели {model} шаблон, который здесь не разбирается")
    return sql


class _Held:
    """Соединение, у которого commit ничего не делает: все, что пишут
    функции обхода, остается в одной транзакции и откатывается."""

    def __init__(self, conn):
        self._conn = conn

    def commit(self) -> None:
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


def score_row(conn, payload: dict, snapshot_date: date) -> tuple | None:
    """Поля карточки бота для объявления, которого нет в базе."""
    from bot.cards import CARD_COLS
    parsed = parse_offer(payload, snapshot_date)
    if parsed is None:
        return None
    oid = parsed["offer"]["offer_id"]
    held = _Held(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("set local statement_timeout = '60s'")
            # Наблюдению нужен прогон, а какой именно, балл не читает.
            cur.execute("select max(run_id) from raw.collection_run where source = 'cian'")
            run_id = cur.fetchone()[0]
        db.save_snapshots(held, run_id, None, snapshot_date, [payload], 0)
        db.upsert_offers(held, [parsed], snapshot_date)
        db.upsert_observations(held, [parsed], run_id)
        db.upsert_metro(held, [parsed])
        db.upsert_photos(held, [parsed])
        enrich_one(conn, oid, parsed["offer"].get("lat"), parsed["offer"].get("lon"))
        scored = render("offer_scored", {"offer_condition": f"({render('offer_condition')})"})
        # Запрос идет с параметром, и знак процента в тексте моделей (в
        # комментариях их полно) удваивается, как в CARD_COLS.
        scored = scored.replace("%", "%%")
        with conn.cursor() as cur:
            cur.execute(f"select {CARD_COLS} from ({scored}) o "
                        "where o.source = 'cian' and o.offer_id = %s", (oid,))
            row = cur.fetchone()
    finally:
        conn.rollback()
    return row


def enrich_one(conn, offer_id: str, lat, lon) -> None:
    """Парки и МКАД, как etl.parks для всей панели."""
    if lat is None or lon is None:
        return
    found = parks.load_parks()
    tree, names = parks.build_index(found)
    large, large_names = parks.build_large_index(found)
    any_park, _ = parks.nearest_park(float(lat), float(lon), tree, names)
    big_park, _ = parks.nearest_park(float(lat), float(lon), large, large_names)
    inside = None
    if parks.MKAD_CACHE.exists():
        inside = parks.mkad_polygon().contains(parks.Point(*parks.to_xy(float(lat), float(lon))))
    with conn.cursor() as cur:
        cur.execute("""update core.offer set park_distance_m = %s, large_park_distance_m = %s,
                              inside_mkad = %s
                        where source = 'cian' and offer_id = %s""",
                    (any_park, big_park, inside, offer_id))


# ---------------------------------------------------------------------
# Браузер
def fetch_page(offer_id: str) -> tuple[int | None, str]:
    """Статус и HTML страницы объявления. Как обход: сначала главная,
    пауза, потом страница; картинки и шрифты не грузятся."""
    from playwright.sync_api import sync_playwright
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), channel="chrome", headless=False, locale="ru-RU",
            viewport={"width": 1440, "height": 900},
            args=["--no-sandbox"] if config.CHROME_NO_SANDBOX else [])
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.route("**/*", lambda route: (
                route.abort() if route.request.resource_type in ("image", "font", "media")
                else route.continue_()))
            resp = page.goto("https://www.cian.ru/", wait_until="domcontentloaded",
                             timeout=60_000)
            html = page.content()
            log.info("главная: %s, %d знаков", resp.status if resp else None, len(html))
            if BrowserClient._looks_blocked(html):
                return 403, html
            time.sleep(random.uniform(config.PAUSE_MIN, config.PAUSE_MAX))
            resp = page.goto(OFFER_URL.format(offer_id), wait_until="domcontentloaded",
                             timeout=90_000)
            html = page.content()
            log.info("объявление %s: %s, %d знаков", offer_id, resp.status if resp else None,
                     len(html))
            return (resp.status if resp else None), html
        finally:
            ctx.close()


def run(offer_id: str, snapshot_date: date) -> dict:
    status, html = fetch_page(offer_id)
    if status == 404:
        return {"status": "not_found"}
    # Заглушку узнаем только по странице без объявления: у настоящей
    # страницы объявления слово captcha стоит в настройках в самом начале,
    # и проверка обхода на ней срабатывает зря (проверено 17.11.2026).
    try:
        data = extract_offer_card(html)
    except SerpNotFound as err:
        if status == 403 or BrowserClient._looks_blocked(html):
            # Начало страницы в журнал: заглушка площадки и капча выглядят
            # по-разному, и без образца не понять, что именно пришло.
            log.warning("не пустили, начало страницы: %r", html[:600])
            return {"status": "blocked"}
        log.warning("страница %s без объявления: %s", offer_id, err)
        return {"status": "not_found"}
    offer = data["offer"]
    if offer.get("dealType") != "rent" or offer.get("offerType") != "flat":
        return {"status": "not_rent"}
    # Снятое площадка показывает той же страницей, с другим статусом, и
    # карточка без пометки выдала бы его за живое.
    if str(offer.get("status") or "").lower() != "published":
        return {"status": "not_found"}
    with psycopg.connect(**config.DB) as conn:
        ids = [str(u.get("id")) for u in (offer.get("geo") or {}).get("undergrounds") or []]
        payload = card_payload(data, station_lines(conn, ids))
        with conn.cursor() as cur:
            cur.execute((ROOT / "sql" / "schema" / "06_link.sql").read_text())
            cur.execute("insert into raw.link_snapshot (offer_id, payload) values (%s, %s)",
                        (str(offer.get("cianId") or offer_id), Jsonb(scrub_payload(payload))))
        conn.commit()
        row = score_row(conn, payload, snapshot_date)
    if row is None:
        return {"status": "error", "message": "объявление не разобралось"}
    return {"status": "ok", "row": list(row), "repair": REPAIR.get(offer.get("repairType"))}


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stderr)
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not args[0].isdigit():
        print(json.dumps({"status": "error", "message": "нужен номер объявления"}))
        return 2
    try:
        out = run(args[0], date.today())
    except Exception as err:  # noqa: BLE001 — боту нужен ответ, а не трассировка
        log.exception("ссылка %s не открылась", args[0])
        out = {"status": "error", "message": type(err).__name__}
    log.info("ссылка %s: %s", args[0], out["status"])
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
