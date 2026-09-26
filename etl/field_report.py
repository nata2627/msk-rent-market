"""Опись собираемых полей: что есть, насколько заполнено, где используется.

    python -m etl.field_report > docs/fields.md

Зачем отдельной командой, а не разовым запросом. Список полей меняется
вместе с разбором и со схемой, и написанная руками опись устаревает молча
— а именно на неё смотрят, решая, что взять в оценку варианта. Пусть
лучше пересобирается одной строкой.

Главное, что видно в отчёте: часть колонок пуста у ВСЕХ объявлений.
Это не сбой разбора, а граница источника — выдача поиска таких полей не
отдаёт вовсе, они есть только на странице самого объявления.
"""
from __future__ import annotations

from datetime import date

from etl import db

LATEST = ("(select max(snapshot_date) from raw.collection_run "
          "where source='cian' and status='ok')")

# Служебное, в опись не идёт: хеши, минхеши, сырые ключи, геометрия.
SKIP = {"source", "offer_id", "snapshot_date", "run_id", "observed_at", "updated_at",
        "description_sha1", "description_minhash", "unmapped_keys",
        "platform_duplicate_ids", "seller_name", "geom"}

RU = {
    "price": "цена в месяц", "currency": "валюта", "payment_period": "период платежа",
    "utilities_included": "коммуналка включена в цену", "utilities_price": "коммуналка, руб",
    "meters_not_included": "счётчики сверх цены", "deposit_rub": "залог, руб",
    "deposit_months": "залог, месяцев (из текста)", "client_fee_pct": "комиссия с нанимателя, %",
    "agent_fee_pct": "комиссия агента, %", "commission_rub": "комиссия, руб",
    "prepay_months": "предоплата, месяцев (из текста)", "lease_term_type": "срок аренды",
    "is_daily": "посуточная", "pets_state": "животные: 4 состояния",
    "pets_raw": "анкетный флаг животных", "pets_source": "откуда вывод про животных",
    "pets_platform_allowed": "попало под фильтр площадки",
    "children_allowed": "можно с детьми", "repair_type": "ТИП РЕМОНТА",
    "has_furniture": "мебель в комнатах", "has_kitchen_furniture": "кухонная мебель",
    "has_washer": "стиральная машина", "has_fridge": "холодильник",
    "has_dishwasher": "посудомойка", "has_conditioner": "кондиционер",
    "has_tv": "телевизор", "has_internet": "интернет", "has_bathtub": "ванна",
    "has_shower": "душевая", "balconies_count": "балконов", "loggias_count": "лоджий",
    "separate_wc_count": "санузлов раздельных", "combined_wc_count": "санузлов совмещённых",
    "description": "текст описания", "seller_type": "тип продавца",
    "seller_id": "продавец (хеш)", "seller_rating": "рейтинг продавца",
    "seller_experience_since": "стаж продавца с", "seller_offers_count": "объявлений у продавца",
    "is_by_homeowner": "от собственника", "photos_count": "фотографий",
    "edited_at": "когда правили", "title": "заголовок",
    "price_estimate_low": "оценка площадки, нижняя", "price_estimate_high": "оценка площадки, верхняя",
    "price_label": "метка площадки о цене", "owner_verified": "собственник подтверждён",
    "is_duplicated_description": "описание не уникально", "url": "ссылка",
    "published_at": "дата публикации", "first_seen_date": "впервые увидели",
    "last_seen_date": "последний раз видели", "is_active": "ещё висит",
    "disappeared_date": "дата исчезновения", "rooms_count": "комнат",
    "flat_type": "тип планировки", "is_apartments": "АПАРТАМЕНТЫ",
    "total_area": "общая площадь", "living_area": "жилая площадь",
    "kitchen_area": "площадь кухни", "floor": "этаж", "floors_total": "этажей в доме",
    "building_material": "тип дома (материал)", "build_year": "год постройки",
    "has_lift": "лифт", "address": "адрес", "lat": "широта", "lon": "долгота",
    "okrug": "округ", "district": "район", "street": "улица", "house": "дом",
    "metro_walk_min": "до метро пешком, мин", "park_distance_m": "до любого парка, м",
    "large_park_distance_m": "до парка от 10 га, м", "inside_mkad": "внутри МКАД",
}

# Веса держатся в marts.offer_scored; здесь только подпись для отчёта.
SCORE = {
    "price": "вес 25 (относительно похожих)",
    "total_area": "вес 20 (относительно похожих)",
    "build_year": "вес 15 (вместо ремонта)",
    "metro_walk_min": "вес 15",
    "large_park_distance_m": "вес 15 (через park_band)",
    "has_furniture": "вес 10",
}

FILTER = {
    "price": "фильтр «цена до»", "total_area": "фильтр «площадь от»",
    "rooms_count": "фильтр, мультивыбор", "metro_walk_min": "фильтр «до метро»",
    "large_park_distance_m": "фильтр «парк», мультивыбор",
    "pets_state": "фильтр, мультивыбор", "client_fee_pct": "фильтр «комиссия»",
    "published_at": "фильтр «опубликовано»", "inside_mkad": "фильтр «внутри МКАД»",
    "district": "фильтр «район»", "is_apartments": "ИСКЛЮЧАЮТСЯ из подбора",
    "is_daily": "исключаются", "lease_term_type": "исключается не длительная",
    "deposit_rub": "в сумме «нужно сразу»", "deposit_months": "в сумме «нужно сразу»",
}


def collect() -> tuple[list[tuple[str, str, float]], int, int]:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from core.offer_observation "
                    f"where source='cian' and snapshot_date={LATEST}")
        n_obs = cur.fetchone()[0]
        cur.execute("select count(*) from core.offer where source='cian'")
        n_off = cur.fetchone()[0]

        rows = []
        for table, total in (("offer", n_off), ("offer_observation", n_obs)):
            cur.execute("""select column_name from information_schema.columns
                           where table_schema='core' and table_name=%s
                           order by ordinal_position""", (table,))
            columns = [c for (c,) in cur.fetchall() if c not in SKIP]
            for column in columns:
                where = "where source='cian'"
                if table == "offer_observation":
                    where += f" and snapshot_date={LATEST}"
                cur.execute(f'select count("{column}") from core.{table} {where}')
                filled = cur.fetchone()[0]
                rows.append((column, RU.get(column, column),
                             100.0 * filled / total if total else 0.0))
    return rows, n_obs, n_off


def _table(items) -> str:
    out = ["| поле | что это | заполнено | где используется | ваш вес |",
           "|---|---|---:|---|---|"]
    for column, label, pct in items:
        use = "; ".join(x for x in (SCORE.get(column), FILTER.get(column)) if x) or "—"
        out.append(f"| `{column}` | {label} | {pct:.0f}% | {use} |  |")
    return "\n".join(out)


def main() -> int:
    rows, n_obs, n_off = collect()
    full = sorted((r for r in rows if r[2] >= 50), key=lambda r: -r[2])
    part = sorted((r for r in rows if 0 < r[2] < 50), key=lambda r: -r[2])
    zero = sorted((r for r in rows if r[2] == 0), key=lambda r: r[0])

    print(f"""# Что собирается о квартире

Опись снята автоматически {date.today():%d.%m.%Y} по последнему завершённому
обходу: {n_obs} наблюдений, {n_off} объявлений.

Колонка «ваш вес» пустая намеренно — размечайте прямо здесь, а веса потом
переедут в `marts.offer_scored`. Сейчас они в сумме дают 100.

## Заполнено, можно пользоваться ({len(full)} полей)

{_table(full)}

## Заполнено частично ({len(part)} полей)

Пользоваться можно, но надо решить, что делать с пропусками.

{_table(part)}

## Пусто у всех объявлений ({len(zero)} полей)

Колонки в схеме есть, данных нет: **выдача поиска этих полей не отдаёт**.
Всё это есть на странице самого объявления и появится только после
обогащения по карточкам короткого списка.

{_table(zero)}

---

Пересобрать опись: `python -m etl.field_report > docs/fields.md`
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
