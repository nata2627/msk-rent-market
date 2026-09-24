"""Ряд средних цен аренды из ЕМИСС (Росстат).

    python -m etl.rosstat

Зачем. Собственная панель начинается с сентября 2026 и за несколько недель
наберёт не больше полутора месяцев. Этого достаточно, чтобы увидеть
снижения цен у конкретных объявлений, но недостаточно для сезонности и
тренда: полтора месяца не отличают «рынок падает» от «в сентябре всегда
так». Росстат публикует среднюю плату за аренду однокомнатной и
двухкомнатной квартиры у частных лиц помесячно с 2015 года.

Чем этот ряд НЕ является. Это средняя по обследованию Росстата, а не
медиана объявлений. У двух источников разные выборки (у них — реально
заключённые договоры у частных лиц, у нас — запрашиваемые цены в
объявлениях) и разный способ измерения. Сравнивать уровни напрямую нельзя.
Сопоставимы изменения: если наш ряд и росстатовский расходятся в
направлении, это повод разбираться со сбором.

Про устройство запроса. ЕМИСС отдаёт данные «широко»: строка — регион и
товар, столбец — конкретный месяц конкретного года, а имя столбца
выглядит как dim2015_1540283_d46405879_i3494. Значащие части — год и
идентификатор месяца, остальное служебное. Поэтому столбцы разбираются
регулярным выражением, а не по позиции.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date

import requests

from etl import db

log = logging.getLogger("rosstat")

URL = "https://www.fedstat.ru/indicator/dataGrid.do"
INDICATOR = "31448"          # Средние потребительские цены на товары и услуги
CPI_INDICATOR = "31074"      # Индексы потребительских цен на товары и услуги

# Внутри ИПЦ выбираем «Все товары и услуги» и разрез «к предыдущему месяцу»:
# нам нужен помесячный дефлятор, а не накопленный к декабрю.
CPI_ITEM_ALL = "1707675"
CPI_KIND_MOM = "1704142"

REGIONS = {
    "Москва": "1688506",
    "Московская область": "1688498",
    "Российская Федерация": "1688487",
}

# Позиции справочника «Виды товаров и услуг».
ITEMS = {
    1: "1755157",            # Аренда однокомнатной квартиры у частных лиц, месяц
    2: "1755035",            # Аренда двухкомнатной квартиры у частных лиц, месяц
}

# Идентификаторы месяцев в измерении «Период».
#
# Порядок в справочнике ЕМИСС не календарный, а произвольный, и вывести
# эти числа нельзя — только прочитать из справочника на странице
# показателя. Первая версия три последних кода угадала, и данные за
# октябрь, ноябрь и декабрь молча не загрузились: колонка просто не
# распозналась, а ошибки не возникло. Ноябрь при этом — самый дорогой
# месяц года для входа в аренду, то есть ровно тот, который терять нельзя.
MONTHS = {
    "1540283": 1,   # январь
    "1540282": 2,   # февраль
    "1540236": 3,   # март
    "1540229": 4,   # апрель
    "1540235": 5,   # май
    "1540234": 6,   # июнь
    "1540233": 7,   # июль
    "1540228": 8,   # август
    "1540276": 9,   # сентябрь
    "1540273": 10,  # октябрь
    "1540272": 11,  # ноябрь
    "1540230": 12,  # декабрь
}

COLUMN = re.compile(r"^dim(\d{4})_(\d+)_")


def fetch(region: str, year_from: int, year_to: int) -> list[dict]:
    data = [("id", INDICATOR),
            ("lineObjectIds", "57831"), ("lineObjectIds", "58273"),
            ("columnObjectIds", "3"), ("columnObjectIds", "33560"),
            ("selectedFilterIds", f"57831_{REGIONS[region]}")]
    for item in ITEMS.values():
        data.append(("selectedFilterIds", f"58273_{item}"))
    for year in range(year_from, year_to + 1):
        data.append(("selectedFilterIds", f"3_{year}"))

    resp = requests.post(URL, data=data, timeout=180, headers={
        "User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    resp.raise_for_status()
    return resp.json().get("results", [])


def fetch_cpi(region: str, year_from: int, year_to: int) -> list[dict]:
    data = [("id", CPI_INDICATOR),
            ("lineObjectIds", "57831"), ("lineObjectIds", "58273"),
            ("lineObjectIds", "57937"),
            ("columnObjectIds", "3"), ("columnObjectIds", "33560"),
            ("selectedFilterIds", f"57831_{REGIONS[region]}"),
            ("selectedFilterIds", f"58273_{CPI_ITEM_ALL}"),
            ("selectedFilterIds", f"57937_{CPI_KIND_MOM}")]
    for year in range(year_from, year_to + 1):
        data.append(("selectedFilterIds", f"3_{year}"))
    resp = requests.post(URL, data=data, timeout=180, headers={
        "User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    resp.raise_for_status()
    return resp.json().get("results", [])


def cpi_to_long(rows: list[dict], region: str) -> list[tuple]:
    out = []
    for row in rows:
        for key, value in row.items():
            m = COLUMN.match(key)
            if not m or not value:
                continue
            month = MONTHS.get(m.group(2))
            if month is None:
                continue          # кварталы и годы в помесячный ряд не идут
            cleaned = str(value).replace("\xa0", "").replace(" ", "").replace(",", ".")
            try:
                index = float(cleaned)
            except ValueError:
                continue
            # Росстат публикует индекс как 100.4, а не как 0.4%.
            out.append((region, date(int(m.group(1)), month, 1), index - 100.0))
    return out


def save_cpi(records: list[tuple]) -> int:
    if not records:
        return 0
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """insert into core.rosstat_cpi (region, period, mom_pct)
                   values (%s, %s, %s)
                   on conflict (region, period) do update
                   set mom_pct = excluded.mom_pct, loaded_at = now()""",
                records)
        conn.commit()
    return len(records)


def to_long(rows: list[dict], region: str) -> list[tuple]:
    """Широкая таблица ЕМИСС -> строки (регион, комнат, месяц, цена)."""
    out: list[tuple] = []
    unknown_months: set[str] = set()
    for row in rows:
        title = row.get("dim58273") or ""
        if "однокомнатной" in title:
            rooms = 1
        elif "двухкомнатной" in title:
            rooms = 2
        else:
            continue

        for key, value in row.items():
            m = COLUMN.match(key)
            if not m or not value:
                continue
            year, month_id = int(m.group(1)), m.group(2)
            month = MONTHS.get(month_id)
            if month is None:
                # Неизвестный код месяца — это не «пустая клетка», а дыра
                # в справочнике. Молча пропустить значит потерять целые
                # месяцы ряда, как уже случилось с осенью.
                unknown_months.add(month_id)
                continue
            # Росстат пишет числа с запятой и неразрывными пробелами.
            cleaned = str(value).replace("\xa0", "").replace(" ", "").replace(",", ".")
            try:
                price = float(cleaned)
            except ValueError:
                continue
            if price <= 0:
                continue
            out.append((region, rooms, date(year, month, 1), price))

    if unknown_months:
        log.warning("Нераспознанные коды месяцев: %s. Эти месяцы в ряд НЕ "
                    "попали — справочник ЕМИСС изменился, обновить MONTHS.",
                    sorted(unknown_months))
    return out


def save(records: list[tuple]) -> int:
    if not records:
        return 0
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """insert into core.rosstat_rent (region, rooms, period, price)
                   values (%s, %s, %s, %s)
                   on conflict (region, rooms, period) do update
                   set price = excluded.price, loaded_at = now()""",
                records)
        conn.commit()
    return len(records)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Ряд аренды из ЕМИСС")
    parser.add_argument("--from-year", type=int, default=2015)
    parser.add_argument("--to-year", type=int, default=date.today().year)
    parser.add_argument("--regions", default="Москва,Российская Федерация")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    total = 0
    for region in [r.strip() for r in args.regions.split(",")]:
        if region not in REGIONS:
            log.warning("Неизвестный регион: %s", region)
            continue
        rows = fetch(region, args.from_year, args.to_year)
        records = to_long(rows, region)
        total += save(records)
        log.info("%s, аренда: строк %d, месяцев %d",
                 region, len(records), len({r[2] for r in records}))

        cpi = cpi_to_long(fetch_cpi(region, args.from_year, args.to_year), region)
        save_cpi(cpi)
        log.info("%s, ИПЦ: месяцев %d", region, len(cpi))
    log.info("Всего загружено значений: %d", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
