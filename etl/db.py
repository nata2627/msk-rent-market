"""Запись в PostgreSQL. Никаких расчётов — только перенос строк в таблицы."""
from __future__ import annotations

import json
from contextlib import contextmanager

import psycopg
from psycopg.types.json import Jsonb

from etl.config import DB
from etl.privacy import scrub_payload
from etl.snapshot_store import payload_hash


@contextmanager
def connect():
    with psycopg.connect(**DB) as conn:
        yield conn


# ---------------------------------------------------------------------
# Журнал запуска
# ---------------------------------------------------------------------
def collected_bucket_keys(conn, snapshot_date, source: str = "cian") -> set:
    """Корзины, уже собранные за эту дату снимка.

    Нужна для продолжения прерванного обхода. Площадка 12.10.2026 отдавала
    выдачу рывками, обход падал через несколько минут, и каждый следующий
    заход начинал список районов заново — за пять часов очередь не дошла
    дальше Раменок, хотя суммарно корзин пройдено было втрое больше.

    Берутся только листовые корзины: разделённые (`was_split`) сами ничего
    не собирали, за них отвечают дети. Родитель при продолжении запросится
    снова, потратит один запрос и снова разделится — это дешевле, чем
    держать в базе дерево и разбираться, целиком ли оно собрано.

    Ключ — то, что однозначно задаёт запрос к площадке: район, комнатность
    и границы цены.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select b.district_id, b.rooms, b.price_min, b.price_max
            from raw.bucket b
            join raw.collection_run r using (run_id)
            where r.source = %s
              and r.snapshot_date = %s
              and b.error is null
              and b.collected_count is not null
              and not b.was_split
              -- Заявленный ноль не считается собранной корзиной. В рывок
              -- площадка отдаёт разборную страницу с нулём результатов,
              -- и такая корзина заперла бы район вместе со всем его
              -- поддеревом до конца суток. Перепросить стоит один запрос.
              and coalesce(b.claimed_count, 0) > 0
            """,
            [source, snapshot_date],
        )
        return {tuple(row) for row in cur.fetchall()}


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
                 pages=None, was_split=False, is_truncated=False, error=None,
                 split_after_underfill=False) -> None:
    """Записать итог корзины.

    `split_after_underfill` отличает деление по недобору от обычного.
    Оба дают was_split, но означают разное: обычное — «заранее видно, что
    не влезет», это норма; по недобору — «площадка пообещала больше, чем
    отдала», это поломка, которую мы обошли. Без отдельной пометки
    посчитать, как часто выдача врёт, задним числом уже нельзя.
    """
    with conn.cursor() as cur:
        cur.execute(
            """update raw.bucket
               set claimed_count = %s, collected_count = %s, pages_fetched = %s,
                   was_split = %s, is_truncated = %s, error = %s,
                   split_after_underfill = %s
               where bucket_id = %s""",
            (claimed, collected, pages, was_split, is_truncated, error,
             split_after_underfill, bucket_id),
        )
    conn.commit()


# ---------------------------------------------------------------------
# Сырые снимки
# ---------------------------------------------------------------------
def save_snapshots(conn, run_id, bucket_id, snapshot_date, offers, page, source="cian") -> int:
    """Записать снимки страницы. Строка на каждое объявление пишется
    всегда, а полная карточка только если она изменилась с последней
    полной карточки этого объявления (etl/snapshot_store.py). Иначе в
    строке ссылка same_as и время подъема added_ts."""
    rows = []
    for payload in offers:
        offer_id = payload.get("id") or payload.get("cianId")
        if offer_id is None:
            continue
        # Персональные данные не попадают в хранилище вовсе, а не
        # прячутся при показе: проект идёт в публичный репозиторий, и
        # любой скриншот или выгрузка иначе становятся распространением
        # чужих телефонов. Ни для одного вопроса исследования они не нужны.
        clean = scrub_payload(payload)
        rows.append((str(offer_id), clean, payload_hash(clean)))
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """select distinct on (offer_id) offer_id, snapshot_id, payload_hash
                 from raw.offer_snapshot
                where source = %s and offer_id = any(%s) and payload is not null
                order by offer_id, snapshot_id desc""",
            (source, [r[0] for r in rows]))
        last = {oid: (sid, h) for oid, sid, h in cur.fetchall()}
        for offer_id, clean, h in rows:
            prev = last.get(offer_id)
            if prev is not None and prev[1] == h:
                cur.execute(
                    """insert into raw.offer_snapshot
                           (run_id, bucket_id, source, offer_id, snapshot_date, page,
                            payload, payload_hash, same_as, added_ts)
                       values (%s, %s, %s, %s, %s, %s, null, %s, %s, %s)""",
                    (run_id, bucket_id, source, offer_id, snapshot_date, page, h, prev[0],
                     Jsonb(clean["addedTimestamp"]) if "addedTimestamp" in clean else None))
            else:
                cur.execute(
                    """insert into raw.offer_snapshot
                           (run_id, bucket_id, source, offer_id, snapshot_date, page,
                            payload, payload_hash)
                       values (%s, %s, %s, %s, %s, %s, %s, %s)
                       returning snapshot_id""",
                    (run_id, bucket_id, source, offer_id, snapshot_date, page, Jsonb(clean), h))
                last[offer_id] = (cur.fetchone()[0], h)
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------
# Панель
# ---------------------------------------------------------------------
OFFER_COLS = [
    "source", "offer_id", "url", "published_at", "rooms_count", "flat_type",
    "is_apartments", "total_area", "living_area", "kitchen_area", "floor",
    "floors_total", "building_material", "build_year", "has_lift",
    "metro_walk_min", "metro_real_walk_min", "address", "lat", "lon", "okrug", "district",
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
         s["time_min"], s["transport_type"], s["line_name"], s.get("line_type"))
        for p in parsed for s in p["metro"]
    ]
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """insert into core.offer_metro
                   (source, offer_id, station_name, time_min, transport_type, line_name, line_type)
               values (%s, %s, %s, %s, %s, %s, %s)
               on conflict (source, offer_id, station_name) do update set
                   time_min = excluded.time_min,
                   transport_type = excluded.transport_type,
                   line_name = excluded.line_name,
                   line_type = excluded.line_type""",
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


def record_pets_hits(conn, run_id: int, bucket_id, snapshot_date,
                     offer_ids, source: str = "cian") -> int:
    """Запомнить в raw, какие объявления площадка отдала под фильтром.

    Без этой записи слой raw неполон: пересборка core из него теряет
    разметку животных, и теряет молча. Строк немного — несколько тысяч в
    день против семнадцати тысяч снимков с полными карточками.
    """
    if not offer_ids:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """insert into raw.pets_hit
                   (run_id, bucket_id, source, offer_id, snapshot_date)
               values (%s, %s, %s, %s, %s)
               on conflict do nothing""",
            [(run_id, bucket_id, source, oid, snapshot_date) for oid in offer_ids],
        )
        written = cur.rowcount
    conn.commit()
    return written


def set_pets_pass_complete(conn, run_id: int, complete: bool) -> None:
    with conn.cursor() as cur:
        cur.execute("update raw.collection_run set pets_pass_complete = %s "
                    "where run_id = %s", (complete, run_id))
    conn.commit()


def pets_hits_for_date(conn, snapshot_date, source: str = "cian") -> list[str]:
    with conn.cursor() as cur:
        cur.execute("select distinct offer_id from raw.pets_hit "
                    "where source = %s and snapshot_date = %s",
                    (source, snapshot_date))
        return [r[0] for r in cur.fetchall()]


def pets_pass_was_complete(conn, snapshot_date, source: str = "cian") -> bool:
    """Был ли хоть один прогон за этот день с полным проходом по животным."""
    with conn.cursor() as cur:
        cur.execute("select bool_or(coalesce(pets_pass_complete, false)) "
                    "from raw.collection_run where snapshot_date = %s",
                    (snapshot_date,))
        row = cur.fetchone()
        return bool(row and row[0])


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


# Сколько подряд не встретить объявление, чтобы счесть его снятым.
#
# Не единица, и это принципиально. На панели из шести дней 5.5% объявлений
# пропадали из выдачи и возвращались: 976 на один день, 113 на два, 25 на
# три. Это не рынок, а недобор обхода — в сегодняшнем прогоне 134 корзины
# из 294 недобрали объявлений, покрытие около 90%.
#
# При пороге в один день мы записали бы тысячу снятий, которых не было, и
# кривая выживаемости провалилась бы на ровном месте. При трёх днях таких
# случаев остаётся 25 из 20 109 — одна восьмая процента.
#
# Цена порога: снятие фиксируется на три дня позже, чем произошло, и
# последние три дня панели цензурированы справа. Для анализа это
# нормально, лишь бы про это помнили.
DISAPPEARED_AFTER_DAYS = 3


def rooms_predicate(room_codes) -> tuple[str, list]:
    """Условие «объявление принадлежит собранным комнатностям».

    Коды площадки и колонки панели не совпадают: 1..6 — это число комнат,
    7 — студия, 9 — свободная планировка, и у последних двух rooms_count
    пуст, а вид хранится в flat_type.
    """
    numeric = [int(c) for c in room_codes if c.isdigit() and int(c) <= 6]
    parts, params = [], []
    if numeric:
        parts.append("rooms_count = any(%s)")
        params.append(numeric)
    if "7" in room_codes:
        parts.append("flat_type = 'studio'")
    if "9" in room_codes:
        parts.append("flat_type = 'openPlan'")
    return "(" + " or ".join(parts) + ")", params


def mark_disappeared(conn, snapshot_date, room_codes, source: str = "cian") -> int:
    """Отметить объявления, которых больше нет в выдаче.

    Отмечаются ТОЛЬКО те комнатности, которые этот обход действительно
    собирал. Раньше проверка была грубее — отметка ставилась лишь после
    обхода всех комнатностей сразу, — и из-за этого не ставилась никогда:
    такого обхода в проекте не случилось ни одного. Узкий ежедневный срез
    покрывает одно- и двухкомнатные полностью, и для них вывод об
    исчезновении столь же законен, как после полного обхода. А вот
    записать в снятые трёхкомнатные, на которые мы не смотрели, нельзя.

    Считаются только дни, когда обход этих комнатностей ЗАВЕРШИЛСЯ УСПЕШНО.
    Без этого пропущенный или упавший день выглядел бы как исчезновение
    всего рынка разом — а падения случаются: 27.09.2026 сбор остановился
    на третьей корзине из почти трёхсот.

    Покрытие считается ОТДЕЛЬНО ПО КАЖДОЙ комнатности, а не по всем
    собранным сразу. Прогон 26.09.2026 закрылся успешно, собрав одни
    двухкомнатные: для однокомнатных этот день пустой, и засчитывать его
    им нельзя. Прежняя версия складывала дни по «любой из комнатностей» —
    достаточно было двухкомнатного обхода, чтобы приблизить снятие
    однокомнатного.

    Дата снятия — первый СОБРАННЫЙ день, в который объявления не нашлось.
    Не `последний раз видели + 1`: этого дня могло не быть в обходе вовсе,
    и тогда дата утверждала бы то, чего никто не проверял. Однокомнатные
    не собирались 23 и 26 сентября 2026 — а 774 объявления из 2417 были
    помечены снятыми именно этими числами, то есть срок жизни каждого
    оказался занижен. Для ежедневного среза оба правила совпадают, разница
    вылезает ровно там, где день пропущен.

    Ещё раз, потому что это главная ловушка проекта: disappeared_date — это
    дата, когда объявление перестало появляться в выдаче. Это НЕ дата сдачи
    квартиры. Объявление снимают по множеству причин, и в анализе
    выживаемости это событие так и называется — снятие с публикации.
    """
    where_rooms, params = rooms_predicate(room_codes)
    if not params and "flat_type" not in where_rooms:
        return 0

    with conn.cursor() as cur:
        cur.execute(
            f"""
            with covered as (
                -- Пара «комнатность — день», а не просто день: обход
                -- бывает частичным, и каждая комнатность отвечает за себя.
                select distinct b.rooms, r.snapshot_date
                from raw.collection_run r
                join raw.bucket b using (run_id)
                where r.source = %s and r.status = 'ok'
                  and b.rooms = any(%s)
                  -- Корзина с ошибкой не покрывает день. Прогон 149 от
                  -- 12.10.2026 закрылся как 'ok' с двумя корзинами, обе
                  -- не прочитались, и день уже числился собранным по
                  -- одно- и двухкомнатным при нуле собранного. Ещё два
                  -- таких дня — и 14 тысяч объявлений получили бы снятие
                  -- задним числом.
                  and b.error is null
            ),
            candidate as (
                -- Код комнатности площадки: rooms_count не знает про
                -- студии (7) и свободную планировку (9), у них вид лежит
                -- в flat_type. Комнатность вперёд вида: у одной студии
                -- площадка указала и flat_type='studio', и одну комнату —
                -- и отдавала её в корзине «1», а не «7». Кто в какой
                -- корзине лежит, решает площадка, и спорить с ней нельзя.
                select o.source, o.offer_id, o.last_seen_date,
                       case when o.rooms_count between 1 and 6
                                 then o.rooms_count::text
                            when o.flat_type = 'studio'   then '7'
                            when o.flat_type = 'openPlan' then '9'
                            end as rooms
                from core.offer o
                where o.source = %s
                  and o.is_active
                  and {where_rooms}
                  and o.last_seen_date < %s
            ),
            gone as (
                select c.source, c.offer_id,
                       min(v.snapshot_date) as first_missing,
                       count(*) as looked
                from candidate c
                join covered v on v.rooms = c.rooms
                              and v.snapshot_date > c.last_seen_date
                group by 1, 2
            )
            update core.offer o
               set is_active = false,
                   disappeared_date = coalesce(o.disappeared_date, g.first_missing),
                   updated_at = now()
              from gone g
             where o.source = g.source and o.offer_id = g.offer_id
               and g.looked >= %s
            """,
            [source, list(room_codes), source] + params
            + [snapshot_date, DISAPPEARED_AFTER_DAYS],
        )
        affected = cur.rowcount
    conn.commit()
    return affected


def remark_disappeared(conn, until_date, source: str = "cian") -> int:
    """Пересчитать снятия по всей панели с нуля.

    Нужно в двух случаях. Первый — пересборка core: upsert_offers
    возвращает объявлению is_active, потому что снимок для него означает
    встречу в выдаче, и отличить пересборку от настоящей встречи он не
    может. Снятия при этом стираются все разом, а в raw их нет: они не
    данные, а вывод из того, в какие дни какая комнатность собиралась.
    Второй — правка самого правила, как 29.09.2026.

    Считается целиком из raw, поэтому идемпотентно: повторный запуск даёт
    то же самое. Комнатности берутся тоже из raw — какие обходились, по
    тем и судим.
    """
    with conn.cursor() as cur:
        cur.execute(
            """select distinct b.rooms
               from raw.collection_run r join raw.bucket b using (run_id)
               where r.source = %s and r.status = 'ok' and b.rooms is not null
               order by 1""",
            (source,),
        )
        codes = [r[0] for r in cur.fetchall()]
    if not codes:
        return 0

    with conn.cursor() as cur:
        cur.execute(
            """update core.offer set is_active = true, disappeared_date = null
               where source = %s and not is_active""",
            (source,),
        )
    # Без commit: сброс и новая отметка обязаны попасть в базу одной
    # транзакцией, иначе оборванный пересчёт оставит панель без снятий.
    return mark_disappeared(conn, until_date, codes, source=source)


