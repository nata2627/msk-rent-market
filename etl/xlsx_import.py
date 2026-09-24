"""Импорт официальной выгрузки ЦИАНа в Excel.

На странице выдачи есть кнопка «Сохранить файл Excel». Это штатная
возможность площадки, а не обход чего-либо, и она даёт то, чего нет в JSON
выдачи: колонку «Можно с детьми/животными».

    python -m etl.xlsx_import data/raw/offers.xlsx

Чем выгрузка ХУЖЕ обычного сбора, и почему она его не заменяет:

* потолок 200 объявлений на файл против 1512 у постраничного обхода;
* нет координат — не посчитать расстояние до парка;
* нет района и округа, только адрес строкой;
* нет даты публикации — значит нет ни срока жизни объявления, ни анализа
  выживаемости;
* нет типа продавца;
* файл скачивается руками, поэтому ежедневную панель на нём не построить;
* и главное: в файл попадают ПЕРВЫЕ 200 объявлений в том порядке, в каком
  их показала выдача. Порядок «По умолчанию» у ЦИАНа учитывает платное
  продвижение, поэтому выгрузка — это верхушка промо-выдачи, а не случайная
  выборка. На проверенном файле это видно прямо: поиск без фильтров нашёл
  26 168 объявлений, в файл попало 200 (0.8%), медиана в них 178 тыс. руб.
  при рыночной вдвое-втрое ниже, а «дизайнерский ремонт» у 98 строк из 200.
  Считать по такому файлу медианы по рынку нельзя.

Чем она ЛУЧШЕ: в ней есть флаг животных прямо от площадки. Это эталон,
по которому проверяется разбор описаний (см. `python -m etl.pets_calibrate`).

Поэтому источник пишется как 'cian_xlsx' и в аналитике по умолчанию не
смешивается с собранными данными: у него другая выборка и другой состав
полей.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
from datetime import date
from pathlib import Path

from etl import db
from etl.parse import pets_from_text
from etl.privacy import scrub_text

log = logging.getLogger("xlsx")

SOURCE = "cian_xlsx"

# ---------------------------------------------------------------------
# Разбор склеенных полей
#
# Площадка кладёт в одну ячейку по нескольку значений через запятую:
# «280000.0 руб./ За месяц, Залог - 280000 руб., Коммунальные услуги
# включены, Срок аренды - Длительный, Предоплата 1 мес». Разбираем по
# смыслу каждого куска, а не по его позиции: порядок и состав кусков
# у разных объявлений различаются.
# ---------------------------------------------------------------------
_PRICE = re.compile(r"([\d\s.,]+)\s*руб")
_DEPOSIT = re.compile(r"Залог\s*-\s*([\d\s]+)\s*руб", re.I)
_PREPAY = re.compile(r"Предоплата\s*(\d+)\s*мес", re.I)
_TERM = re.compile(r"Срок аренды\s*-\s*([^,]+)", re.I)
_COMMISSION_PCT = re.compile(r"Комиссия\D{0,12}(\d{1,3})\s*%", re.I)

LEASE_TERMS = {
    "длительный": "longTerm",
    "на несколько месяцев": "fewMonths",
    "посуточно": "daily",
}


def parse_price_cell(cell: str | None) -> dict:
    if not cell:
        return {}
    text = str(cell)
    out: dict = {}

    m = _PRICE.search(text)
    if m:
        out["price"] = float(m.group(1).replace(" ", "").replace("\xa0", "").replace(",", "."))

    out["payment_period"] = "day" if "за сутки" in text.lower() else "month"

    m = _DEPOSIT.search(text)
    if m:
        out["deposit_rub"] = float(m.group(1).replace(" ", "").replace("\xa0", ""))

    m = _PREPAY.search(text)
    if m:
        out["prepay_months"] = int(m.group(1))

    m = _COMMISSION_PCT.search(text)
    if m:
        out["client_fee_pct"] = float(m.group(1))

    # «Коммунальные услуги включены» против «не включены». Проверяем
    # отрицание первым: подстрока «включены» есть в обеих формулировках.
    low = text.lower()
    if "коммунальные услуги не включены" in low:
        out["utilities_included"] = False
    elif "коммунальные услуги включены" in low:
        out["utilities_included"] = True

    m = _TERM.search(text)
    if m:
        term = m.group(1).strip().lower()
        out["lease_term_type"] = LEASE_TERMS.get(term, term)
        out["is_daily"] = out["lease_term_type"] == "daily"
    return out


_ROOMS = re.compile(r"^\s*(\d+)")


def parse_rooms_cell(cell) -> dict:
    """«2, Изолированная» -> число комнат и тип планировки."""
    if cell is None:
        return {}
    text = str(cell)
    out: dict = {}
    m = _ROOMS.match(text)
    if m:
        out["rooms_count"] = int(m.group(1))
    if "студи" in text.lower():
        out["flat_type"] = "studio"
    elif "изолирован" in text.lower():
        out["flat_type"] = "rooms"
    return out


_HOUSE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)")


def parse_house_cell(cell) -> dict:
    """«42/75, Монолитный» -> этаж, этажность, материал стен."""
    if cell is None:
        return {}
    text = str(cell)
    out: dict = {}
    m = _HOUSE.match(text)
    if m:
        out["floor"], out["floors_total"] = int(m.group(1)), int(m.group(2))
    tail = text.split(",", 1)
    if len(tail) == 2 and tail[1].strip():
        out["building_material"] = tail[1].strip()
    return out


_METRO = re.compile(r"м\.\s*([^(]+?)\s*\((\d+)\s*мин\s+([^)]+)\)")


def parse_metro_cell(cell) -> list[dict]:
    """«м. Деловой центр (3 мин пешком)» -> станция, минуты, способ.

    Способ добирания различается намеренно: пятнадцать минут пешком и
    пятнадцать на машине — разные блага, и в метро_walk попадают только пешие.
    """
    if not cell:
        return []
    out = []
    for name, minutes, how in _METRO.findall(str(cell)):
        out.append({
            "station_name": name.strip(),
            "time_min": int(minutes),
            "transport_type": "walk" if "пешк" in how else "transport",
            "line_name": None,
        })
    return out


_COUNTED = re.compile(r"([А-Яа-яЁё\s]+?)\s*\((\d+)\)")


def parse_counted_cell(cell) -> dict[str, int]:
    """«Совмещенный (1), Раздельный (1)» -> {'совмещенный': 1, 'раздельный': 1}."""
    if not cell:
        return {}
    return {name.strip().lower(): int(n) for name, n in _COUNTED.findall(str(cell))}


AMENITIES = {
    "has_furniture": "мебель в комнатах",
    "has_kitchen_furniture": "мебель на кухне",
    "has_washer": "стиральная машина",
    "has_fridge": "холодильник",
    "has_dishwasher": "посудомоечная машина",
    "has_conditioner": "кондиционер",
    "has_tv": "телевизор",
    "has_internet": "интернет",
    "has_bathtub": "ванна",
    "has_shower": "душевая кабина",
}


def parse_amenities_cell(cell) -> dict:
    """Список удобств -> отдельные флаги.

    Пустая ячейка даёт None, а не False: «не указано» и «нет стиральной
    машины» — разные вещи, и сливать их нельзя ровно по той же причине,
    по которой у животных четыре состояния, а не два.
    """
    if not cell:
        return {k: None for k in AMENITIES}
    low = str(cell).lower()
    return {field: (token in low) for field, token in AMENITIES.items()}


def parse_area_cell(cell) -> dict:
    """«320.0/230.0/30.0» -> общая, жилая и кухня.

    Площадь в выгрузке лежит одной строкой через слэш в порядке
    общая/жилая/кухня. На проверенном файле из 200 строк так устроены 146,
    ещё 19 содержат два значения, и только 35 — одно.

    Это стоит отдельной функции, потому что первая версия разбирала ячейку
    как одно число и молча теряла площадь у 165 объявлений из 200. Площадь —
    главная объясняющая переменная гедонистической регрессии, и такая потеря
    испортила бы всё, причём незаметно: колонка выглядела бы просто
    наполовину пустой, а не неверной.

    Два значения читаются как общая/жилая — таков порядок у площадки.
    Полностью исключить, что вторым числом иногда идёт кухня, нельзя;
    проверка на правдоподобие соотношений есть в sql/checks.
    """
    if cell is None:
        return {}
    parts = [p.strip() for p in str(cell).split("/")]
    values = []
    for part in parts:
        v = _f(part)
        values.append(v if v and v > 0 else None)

    out: dict = {}
    names = ("total_area", "living_area", "kitchen_area")
    for name, value in zip(names, values):
        if value is not None:
            out[name] = value
    return out


def parse_pets_cell(cell, description: str | None) -> dict:
    """«Можно с детьми, Можно с животными» -> состояния животных и детей.

    Важное ограничение площадки: в анкете есть только галочка «можно
    с животными». Состояния «запрещено» в ней не существует — пустая
    ячейка означает, что галочку не поставили, а не что животных не пускают.
    Поэтому из анкеты получается ровно одно твёрдое значение, 'allowed',
    а 'forbidden' и 'negotiable' по-прежнему вытаскиваются только из текста.
    """
    low = (str(cell) if cell else "").lower()
    pets_flag = "можно с животными" in low
    children = "можно с детьми" in low if cell else None

    if pets_flag:
        return {"pets_state": "allowed", "pets_raw": True,
                "pets_source": "platform_field", "pets_platform_allowed": True,
                "children_allowed": children}

    from_text = pets_from_text(description)
    return {
        "pets_state": from_text or "unknown",
        "pets_raw": None,
        "pets_source": "text" if from_text else "none",
        # Галочки нет. Это НЕ запрет: анкету могли просто не заполнить.
        "pets_platform_allowed": False,
        "children_allowed": children,
    }


# ---------------------------------------------------------------------
def read_rows(path: Path) -> list[dict]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = list(wb.active.iter_rows(values_only=True))
    if not rows:
        return []
    header = [str(h).strip() if h is not None else "" for h in rows[0]]
    return [dict(zip(header, r)) for r in rows[1:] if any(v is not None for v in r)]


def column(row: dict, *names):
    """Достать колонку по одному из возможных названий.

    В заголовке встречается двойной пробел («ID  объявления»), и состав
    колонок у выгрузки для аренды и для продажи различается.
    """
    for name in names:
        for key in row:
            if key.replace("  ", " ").strip().lower() == name.lower():
                return row[key]
    return None


def row_to_records(row: dict, snapshot_date: date) -> dict | None:
    offer_id = column(row, "ID объявления", "ID")
    if offer_id is None:
        return None
    offer_id = str(int(offer_id)) if isinstance(offer_id, float) else str(offer_id).strip()

    description = scrub_text(column(row, "Описание"))
    stations = parse_metro_cell(column(row, "Метро"))
    walk = [s["time_min"] for s in stations if s["transport_type"] == "walk"]

    house = parse_house_cell(column(row, "Дом"))
    wc = parse_counted_cell(column(row, "Санузел"))
    balcony = parse_counted_cell(column(row, "Балкон"))
    lifts = parse_counted_cell(column(row, "Лифт"))

    offer = {
        "source": SOURCE,
        "offer_id": offer_id,
        "url": column(row, "Ссылка на объявление"),
        "published_at": None,          # в выгрузке даты публикации нет
        "flat_type": None,
        "is_apartments": None,
        "total_area": None, "living_area": None, "kitchen_area": None,
        **parse_area_cell(column(row, "Площадь, м2")),
        "building_material": house.get("building_material"),
        "build_year": None,
        "has_lift": bool(sum(lifts.values())) if lifts else None,
        "metro_walk_min": min(walk) if walk else None,
        "address": column(row, "Адрес"),
        # Координат, района и округа в выгрузке нет. Оставляем пустыми
        # честно, а не выдумываем из строки адреса.
        "lat": None, "lon": None, "okrug": None, "district": None,
        "street": None, "house": None,
        "floor": house.get("floor"),
        "floors_total": house.get("floors_total"),
        **parse_rooms_cell(column(row, "Количество комнат")),
    }

    observation = {
        "source": SOURCE,
        "offer_id": offer_id,
        "snapshot_date": snapshot_date,
        "currency": "rur",
        "is_daily": False,
        "repair_type": column(row, "Ремонт"),
        "description": description,
        "description_sha1": (hashlib.sha1(description.encode()).hexdigest()
                             if description else None),
        "title": None,
        "separate_wc_count": wc.get("раздельный"),
        "combined_wc_count": wc.get("совмещенный"),
        "balconies_count": balcony.get("балкон"),
        "loggias_count": balcony.get("лоджия"),
        "photos_count": None,
        "seller_type": None, "seller_name": None, "seller_id": None,
        "seller_offers_count": None, "seller_rating": None,
        "seller_experience_since": None, "is_by_homeowner": None,
        "edited_at": None, "unmapped_keys": None,
        "description_minhash": None, "is_duplicated_description": None,
        "utilities_price": None, "meters_not_included": None,
        "deposit_months": None, "agent_fee_pct": None, "commission_rub": None,
        **parse_price_cell(column(row, "Цена")),
        **parse_amenities_cell(column(row, "Дополнительно")),
        **parse_pets_cell(column(row, "Можно с детьми/животными"), description),
    }

    return {"offer": offer, "observation": observation,
            "metro": stations, "photos": []}


def _f(value):
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ".").replace(" ", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Импорт выгрузки ЦИАНа в Excel")
    parser.add_argument("path", type=Path)
    parser.add_argument("--snapshot-date", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    snapshot = (date.fromisoformat(args.snapshot_date)
                if args.snapshot_date else date.today())
    rows = read_rows(args.path)
    parsed = [r for r in (row_to_records(r, snapshot) for r in rows) if r]
    log.info("Строк в файле: %d, разобрано: %d", len(rows), len(parsed))

    if len(rows) >= 200:
        log.warning("В файле %d строк — выгрузка упирается в потолок в 200 "
                    "объявлений. Значит поиск отдал больше, чем сюда попало.",
                    len(rows))

    with db.connect() as conn:
        run_id = db.start_run(conn, "narrow", snapshot, source=SOURCE)
        db.upsert_offers(conn, parsed, snapshot)
        db.upsert_observations(conn, parsed, run_id)
        db.upsert_metro(conn, parsed)
        db.finish_run(conn, run_id, "ok", offers_seen=len(parsed),
                      buckets_total=0, buckets_underfilled=0, requests_made=0,
                      note=f"ручная выгрузка {args.path.name}")
    log.info("Загружено в панель как source='%s'", SOURCE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
