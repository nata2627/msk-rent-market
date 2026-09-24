"""Запись в PostgreSQL. Никаких расчётов — только перенос строк в таблицы."""
from __future__ import annotations

import json
from contextlib import contextmanager

import psycopg
from psycopg.types.json import Jsonb

from etl.config import DB
from etl.privacy import scrub_payload


@contextmanager
def connect():
    with psycopg.connect(**DB) as conn:
        yield conn


# ---------------------------------------------------------------------
# Журнал запуска
# ---------------------------------------------------------------------
def start_run(conn, mode: str, snapshot_date, source: str = "cian") -> int:
    with conn.cursor() as cur:
        cur.execute(
            """insert into raw.collection_run (source, mode, snapshot_date)
               values (%s, %s, %s) returning run_id""",
            (source, mode, snapshot_date),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def finish_run(conn, run_id: int, status: str, **stats) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """update raw.collection_run
               set status = %s, finished_at = now(),
                   buckets_total = %s, buckets_underfilled = %s,
                   offers_seen = %s, requests_made = %s, note = %s
               where run_id = %s""",
            (status, stats.get("buckets_total"), stats.get("buckets_underfilled"),
             stats.get("offers_seen"), stats.get("requests_made"),
             stats.get("note"), run_id),
        )
    conn.commit()


# ---------------------------------------------------------------------
# Корзины
# ---------------------------------------------------------------------
def insert_bucket(conn, run_id: int, bucket, query: dict, parent_id: int | None,
                  pets_filter: bool = False) -> int:
    cols = bucket.as_columns()
    with conn.cursor() as cur:
        cur.execute(
            """insert into raw.bucket
                   (run_id, okrug, district, district_id, rooms,
                    price_min, price_max, depth, parent_bucket_id, query_json,
                    pets_filter)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               returning bucket_id""",
            (run_id, cols["okrug"], cols["district"], cols["district_id"],
             cols["rooms"], cols["price_min"], cols["price_max"], cols["depth"],
             parent_id, Jsonb(query), pets_filter),
        )
        bucket_id = cur.fetchone()[0]
    conn.commit()
    return bucket_id


def close_bucket(conn, bucket_id: int, *, claimed=None, collected=None,
                 pages=None, was_split=False, is_truncated=False, error=None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """update raw.bucket
               set claimed_count = %s, collected_count = %s, pages_fetched = %s,
                   was_split = %s, is_truncated = %s, error = %s
               where bucket_id = %s""",
            (claimed, collected, pages, was_split, is_truncated, error, bucket_id),
        )
    conn.commit()


# ---------------------------------------------------------------------
# Сырые снимки
# ---------------------------------------------------------------------
def save_snapshots(conn, run_id, bucket_id, snapshot_date, offers, page, source="cian") -> int:
    rows = []
    for payload in offers:
        offer_id = payload.get("id") or payload.get("cianId")
        if offer_id is None:
            continue
        # Персональные данные не попадают в хранилище вовсе, а не
        # прячутся при показе: проект идёт в публичный репозиторий, и
        # любой скриншот или выгрузка иначе становятся распространением
        # чужих телефонов. Ни для одного вопроса исследования они не нужны.
        rows.append((run_id, bucket_id, source, str(offer_id),
                     snapshot_date, page, Jsonb(scrub_payload(payload))))
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """insert into raw.offer_snapshot
                   (run_id, bucket_id, source, offer_id, snapshot_date, page, payload)
               values (%s, %s, %s, %s, %s, %s, %s)""",
            rows,
        )
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------
# Панель
# ---------------------------------------------------------------------
OFFER_COLS = [
    "source", "offer_id", "url", "published_at", "rooms_count", "flat_type",
    "is_apartments", "total_area", "living_area", "kitchen_area", "floor",
    "floors_total", "building_material", "build_year", "has_lift",
    "metro_walk_min", "address", "lat", "lon", "okrug", "district",
    "street", "house",
]

OBSERVATION_COLS = [
    "source", "offer_id", "snapshot_date", "run_id", "price", "currency",
    "payment_period", "utilities_included", "utilities_price",
    "meters_not_included", "deposit_rub", "deposit_months", "client_fee_pct",
    "agent_fee_pct", "commission_rub", "prepay_months", "lease_term_type",
    "is_daily", "pets_state", "pets_raw", "children_allowed", "repair_type",
    "has_furniture", "has_kitchen_furniture", "has_washer", "has_fridge",
    "has_dishwasher", "has_conditioner", "has_tv", "has_internet",
    "has_bathtub", "has_shower", "balconies_count", "loggias_count",
    "separate_wc_count", "combined_wc_count", "description",
    "description_sha1", "seller_type", "seller_name", "seller_id",
    "seller_offers_count", "is_by_homeowner", "photos_count", "edited_at",
    "unmapped_keys", "title", "pets_source", "pets_platform_allowed",
    "description_minhash", "is_duplicated_description", "seller_rating",
    "seller_experience_since", "price_estimate_low", "price_estimate_high",
    "price_label", "owner_verified", "platform_duplicate_ids",
]


def _placeholders(cols):
    return ", ".join(["%s"] * len(cols))


def upsert_offers(conn, parsed: list[dict], snapshot_date) -> None:
    """Идентичность объявления.

    Устойчивые поля пишутся при первой встрече и дальше обновляются только
    через coalesce: если площадка вдруг перестала отдавать площадь, мы не
    затираем ту, что уже знаем. Пустое значение — это почти всегда пропуск
    в выдаче, а не исправление данных.

    first_seen_date фиксируется через least: повторный запуск задним числом
    не должен «омолодить» объявление, иначе срок жизни в анализе выживаемости
    посчитается короче реального.
    """
    if not parsed:
        return
    updates = ", ".join(
        f"{c} = coalesce(excluded.{c}, core.offer.{c})"
        for c in OFFER_COLS if c not in ("source", "offer_id")
    )
    sql = f"""
        insert into core.offer ({", ".join(OFFER_COLS)}, first_seen_date, last_seen_date)
        values ({_placeholders(OFFER_COLS)}, %s, %s)
        on conflict (source, offer_id) do update set
            {updates},
            first_seen_date = least(core.offer.first_seen_date, excluded.first_seen_date),
            last_seen_date  = greatest(core.offer.last_seen_date, excluded.last_seen_date),
            is_active = true,
            disappeared_date = null,
            updated_at = now()
    """
    rows = [
        tuple(p["offer"].get(c) for c in OFFER_COLS) + (snapshot_date, snapshot_date)
        for p in parsed
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()


def upsert_observations(conn, parsed: list[dict], run_id: int) -> None:
    """Наблюдение панели.

    Ключ (source, offer_id, snapshot_date) и даёт идемпотентность: повторный
    запуск в тот же день переписывает наблюдение того же дня, а не добавляет
    второе. Без этого история цен из core.price_change показала бы
    несуществующие «изменения» между двумя чтениями одних суток.
    """
    if not parsed:
        return
    updates = ", ".join(
        f"{c} = excluded.{c}"
        for c in OBSERVATION_COLS if c not in ("source", "offer_id", "snapshot_date")
    )
    sql = f"""
        insert into core.offer_observation ({", ".join(OBSERVATION_COLS)})
        values ({_placeholders(OBSERVATION_COLS)})
        on conflict (source, offer_id, snapshot_date) do update set
            {updates}, observed_at = now()
    """
    rows = []
    for p in parsed:
        obs = dict(p["observation"])
        obs["run_id"] = run_id
        for jsonb_col in ("unmapped_keys", "description_minhash",
                          "platform_duplicate_ids"):
            if obs.get(jsonb_col) is not None:
                obs[jsonb_col] = Jsonb(obs[jsonb_col])
        rows.append(tuple(obs.get(c) for c in OBSERVATION_COLS))
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()


def upsert_metro(conn, parsed: list[dict]) -> None:
    rows = [
        (p["offer"]["source"], p["offer"]["offer_id"], s["station_name"],
         s["time_min"], s["transport_type"], s["line_name"])
        for p in parsed for s in p["metro"]
    ]
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """insert into core.offer_metro
                   (source, offer_id, station_name, time_min, transport_type, line_name)
               values (%s, %s, %s, %s, %s, %s)
               on conflict (source, offer_id, station_name) do update set
                   time_min = excluded.time_min,
                   transport_type = excluded.transport_type,
                   line_name = excluded.line_name""",
            rows,
        )
    conn.commit()


def upsert_photos(conn, parsed: list[dict]) -> None:
    rows = [
        (p["offer"]["source"], p["offer"]["offer_id"], ph["photo_pos"], ph["url"])
        for p in parsed for ph in p["photos"]
    ]
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """insert into core.offer_photo (source, offer_id, photo_pos, url)
               values (%s, %s, %s, %s)
               on conflict (source, offer_id, photo_pos) do update set
                   url = excluded.url""",
            rows,
        )
    conn.commit()


def mark_pets_allowed(conn, offer_ids: list[str], snapshot_date,
                      source: str = "cian", mark_rest_false: bool = True) -> int:
    """Проставить твёрдую метку «можно с животными» по результату прохода
    с фильтром площадки.

    Метка кладётся в отдельную колонку pets_platform_allowed и НЕ затирает
    pets_state, выведенное из текста: расхождение между ними — самостоятельный
    результат (насколько описание врёт относительно анкеты), и стирать его
    незачем. В pets_state значение обновляется только на 'allowed', потому
    что попадание в фильтр это факт, а не догадка.
    """
    if not offer_ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """update core.offer_observation
               set pets_platform_allowed = true,
                   pets_state  = 'allowed',
                   pets_source = 'platform_filter'
               where source = %s and snapshot_date = %s and offer_id = any(%s)""",
            (source, snapshot_date, list(offer_ids)),
        )
        marked = cur.rowcount
        # Всё остальное, что мы видели в этот день, площадка под фильтр не
        # отдала. Это «не помечено как можно», а не «запрещено»: анкета
        # могла быть просто не заполнена. Поэтому false, а pets_state
        # остаётся тем, что вычитано из текста.
        # ...но только если проход с фильтром покрыл ВСЕ корзины. Если по
        # какой-то корзине фильтр не сработал, пометить её объявления как
        # «не разрешено» значило бы выдать пробел в сборе за факт о рынке.
        if mark_rest_false:
            cur.execute(
                """update core.offer_observation
                   set pets_platform_allowed = false
                   where source = %s and snapshot_date = %s
                     and pets_platform_allowed is null
                     and not (offer_id = any(%s))""",
                (source, snapshot_date, list(offer_ids)),
            )
    conn.commit()
    return marked


def inherit_pets(conn, snapshot_date, source: str = "cian") -> int:
    """Перенести известную метку про животных на сегодняшнее наблюдение.

    Разрешение держать животных — свойство квартиры, а не дня. Один раз
    выясненное, оно не меняется, и выяснять его заново каждые сутки значит
    тратить пятую часть обхода впустую.

    Переносится только твёрдая метка площадки (pets_source =
    'platform_filter'): она получена сравнением двух выдач и не является
    догадкой. Вычитанное из текста не переносим — текст объявления могли
    переписать, и вместе с ним могло измениться условие.
    """
    with conn.cursor() as cur:
        cur.execute(
            """with known as (
                   select distinct on (offer_id) offer_id, pets_platform_allowed, pets_state
                   from core.offer_observation
                   where source = %s and snapshot_date < %s
                     and pets_source = 'platform_filter'
                   order by offer_id, snapshot_date desc
               )
               update core.offer_observation ob
               set pets_platform_allowed = k.pets_platform_allowed,
                   pets_state  = case when k.pets_platform_allowed
                                      then 'allowed' else ob.pets_state end,
                   pets_source = 'platform_filter'
               from known k
               where ob.source = %s and ob.snapshot_date = %s
                 and ob.offer_id = k.offer_id
                 and ob.pets_source is distinct from 'platform_filter'""",
            (source, snapshot_date, source, snapshot_date),
        )
        n = cur.rowcount
    conn.commit()
    return n


def offers_without_pets_verdict(conn, offer_ids: list[str],
                                source: str = "cian") -> int:
    """Сколько из этих объявлений мы ещё ни разу не классифицировали.

    Нужно, чтобы решить, стоит ли вообще делать по корзине второй запрос:
    если все объявления в ней уже с меткой, проход с фильтром не даст
    ничего нового.
    """
    if not offer_ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """select count(*) from unnest(%s::text[]) as t(offer_id)
               where not exists (
                   select 1 from core.offer_observation ob
                   where ob.source = %s and ob.offer_id = t.offer_id
                     and ob.pets_source = 'platform_filter')""",
            (list(offer_ids), source),
        )
        return cur.fetchone()[0]


def mark_disappeared(conn, snapshot_date, source: str = "cian") -> int:
    """Отметить объявления, которых больше нет в выдаче.

    Вызывается ТОЛЬКО после полного обхода. После узкого среза нельзя: он
    покрывает один сегмент, и всё остальное объявление «исчезло» бы просто
    потому, что мы туда не смотрели.

    Ещё раз, потому что это главная ловушка проекта: disappeared_date — это
    дата, когда объявление перестало появляться в выдаче. Это НЕ дата сдачи
    квартиры. Объявление снимают по множеству причин, и в анализе
    выживаемости это событие так и называется — снятие с публикации.
    """
    with conn.cursor() as cur:
        cur.execute(
            """update core.offer
               set is_active = false,
                   disappeared_date = coalesce(disappeared_date, %s),
                   updated_at = now()
               where source = %s and is_active and last_seen_date < %s""",
            (snapshot_date, source, snapshot_date),
        )
        affected = cur.rowcount
    conn.commit()
    return affected


def refresh_marts(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("select marts.refresh_all()")
    conn.commit()
