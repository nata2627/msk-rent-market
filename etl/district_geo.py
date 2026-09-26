"""Дополнить карту районов контурами поселений Новой Москвы.

    python -m etl.district_geo          # добрать недостающее
    python -m etl.district_geo --list   # только показать, чего не хватает

Зачем. Карта в дашборде красит районы, сопоставляя их ПО НАЗВАНИЮ с
контурами из `sql/geo/moscow_districts.geojson`. Чего в файле нет, то
остаётся серым — молча, ничем себя не выдавая. Так пропадали 17
поселений Новой Москвы и 2624 объявления в них: Сосенское, Московский,
Внуковское, Десёновское и другие.

Контуры берутся из OpenStreetMap той же машинерией, что тянет парки:
границы районов лежат там отношениями с admin_level=8.

ВАЖНО, ПРОВЕРЕНО 27.09.2026: поселений Новой Москвы в OpenStreetMap
больше НЕТ — ни на одном административном уровне с 5 по 10. После
реформы они заменены новыми районами: вместо «поселение Сосенское» там
«район Коммунарка», вместо «Филимонковское» — «Филимонковский район».
ЦИАН при этом продолжает отдавать старые названия поселений, и
сопоставить их с новыми границами по имени нельзя: деление другое, одно
поселение может лежать сразу в нескольких новых районах.

Поэтому семнадцать поселений так и остаются серыми на карте, и это не
недоделка, а отсутствие данных. Подбирать им чужие контуры «по смыслу»
нельзя — это будет выдумка, нарисованная поверх настоящей карты.
Модуль оставлен рабочим: он добирает любой район, который в OSM есть,
и честно перечисляет те, которых нет.

Собственных имён модуль не придумывает: он берёт список районов из
собранных данных и ищет для каждого контур, а не наоборот. Поэтому в
файл не попадёт ничего, чего нет в панели.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from etl import config, db
from etl.parks import _overpass

log = logging.getLogger("district_geo")

GEOJSON = config.ROOT / "sql" / "geo" / "moscow_districts.geojson"

# Квадрат с запасом: Новая Москва уходит далеко на юго-запад, до Роговского.
BBOX = "55.10,36.70,56.10,38.00"

QUERY = f"""
[out:json][timeout:300];
relation["boundary"="administrative"]["admin_level"="8"]({BBOX});
out geom;
"""

# Приставки, которыми OSM называет то, что у ЦИАНа зовётся одним словом:
# «поселение Сосенское» против «Сосенское».
PREFIXES = ("поселение ", "городское поселение ", "сельское поселение ",
            "городской округ ", "рабочий посёлок ", "рабочий поселок ",
            "посёлок ", "поселок ", "район ")


def normalise(name: str) -> str:
    """Название к виду, в котором его можно сравнивать.

    Написания расходятся по мелочам, и каждая мелочь стоит целого района:
    «ё» против «е», заглавная буква во втором слове, дефис вместо пробела.
    Однажды это уже спрятало с карты 12 районов и 3359 объявлений.
    """
    low = name.lower().strip()
    for prefix in PREFIXES:
        if low.startswith(prefix):
            low = low[len(prefix):]
            break
    return low.replace("ё", "е").replace("-", " ").strip()


def rings_from_relation(el: dict) -> list[list[list[float]]]:
    """Внешние контуры отношения, сшитые из кусков.

    Границы в OSM лежат не готовыми кольцами, а обрывками линий: у района
    общая граница с соседом — это одна линия, и принадлежит она обоим.
    Поэтому куски приходится сшивать по совпадающим концам.
    """
    pieces = [[(p["lon"], p["lat"]) for p in m["geometry"]]
              for m in el.get("members", [])
              if m.get("type") == "way" and m.get("geometry")
              and m.get("role") in ("outer", "")]
    rings, current = [], None
    while pieces or current:
        if current is None:
            current = pieces.pop(0)
        if current[0] == current[-1] and len(current) >= 4:
            rings.append([list(p) for p in current])
            current = None
            continue
        for i, piece in enumerate(pieces):
            if piece[0] == current[-1]:
                current = current + piece[1:]
                pieces.pop(i)
                break
            if piece[-1] == current[-1]:
                current = current + piece[::-1][1:]
                pieces.pop(i)
                break
        else:
            # Кусок ни к чему не прирос: граница в выгрузке рваная.
            # Замыкаем принудительно, если он похож на контур, иначе
            # выбрасываем — дырявый полигон карта всё равно не покажет.
            if len(current) >= 4:
                rings.append([list(p) for p in current] + [list(current[0])])
            current = None
    return rings


def wanted_districts() -> list[str]:
    """Районы из собранных данных — те же, что идут в фильтр дашборда."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("""select district from core.offer
                       where source = 'cian' and district is not null
                       group by 1 having count(*) >= 3 order by 1""")
        return [r[0] for r in cur.fetchall()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Контуры районов для карты")
    ap.add_argument("--list", action="store_true",
                    help="только показать, каких контуров не хватает")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    geo = json.loads(GEOJSON.read_text(encoding="utf-8"))
    have = {f["properties"]["name"] for f in geo["features"]}
    need = [d for d in wanted_districts() if d not in have]

    log.info("Контуров в файле: %d, районов в данных без контура: %d",
             len(have), len(need))
    if not need:
        log.info("Добирать нечего.")
        return 0
    log.info("Не хватает: %s", ", ".join(need))
    if args.list:
        return 0

    log.info("Запрашиваю границы у OpenStreetMap ...")
    elements = _overpass(QUERY)
    log.info("Отношений с admin_level=8 в ответе: %d", len(elements))

    by_norm: dict[str, dict] = {}
    for el in elements:
        name = (el.get("tags") or {}).get("name")
        if name:
            by_norm.setdefault(normalise(name), el)

    added, missed = [], []
    for district in need:
        el = by_norm.get(normalise(district))
        if not el:
            missed.append(district)
            continue
        rings = rings_from_relation(el)
        if not rings:
            missed.append(district)
            continue
        geo["features"].append({
            "type": "Feature",
            "properties": {"name": district,
                           "osm_name": (el.get("tags") or {}).get("name")},
            "geometry": ({"type": "Polygon", "coordinates": [rings[0]]}
                         if len(rings) == 1
                         else {"type": "MultiPolygon",
                               "coordinates": [[r] for r in rings]}),
        })
        added.append(district)

    GEOJSON.write_text(json.dumps(geo, ensure_ascii=False), encoding="utf-8")
    log.info("Добавлено контуров: %d — %s", len(added), ", ".join(added) or "нет")
    if missed:
        log.warning("Не нашлось в OpenStreetMap: %s", ", ".join(missed))
    log.info("Перезапустить раздачу: docker compose restart geojson")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
