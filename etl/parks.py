"""Расстояние от квартиры до ближайшего парка по данным OpenStreetMap.

    python -m etl.parks --fetch     # выгрузить парки Москвы (один раз)
    python -m etl.parks             # проставить расстояния объявлениям

Зачем. Парк рядом — один из критериев поиска и одновременно переменная
гедонистической регрессии: без неё надбавка за животных вберёт в себя
часть эффекта зелёной зоны (с собакой у парка живут охотнее), и
коэффициент окажется завышен.

Источник — Overpass API поверх OpenStreetMap: открытый, бесплатный,
без ключа. Берутся объекты leisure=park, а также garden и nature_reserve:
Измайловский и Лосиный Остров размечены не как park, и потерять их значило
бы объявить восток Москвы безлесным.

Две вещи, которые здесь считаются не «правильно», а осознанно упрощённо.

1. **Расстояние до границы парка, а не до его центра.** Для Сокольников
   разница в километры: дом у ворот и дом у центра парка — разные вещи.
   Точка внутри парка получает ноль.

2. **Плоская проекция вместо геодезической.** Москва занимает около
   полуградуса по широте, и равнопромежуточная проекция с масштабом по
   косинусу широты даёт ошибку меньше половины процента — сантиметры на
   километр. Тянуть ради этого pyproj незачем, а расстояние до парка всё
   равно интересует нас с точностью до сотни метров.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from pathlib import Path

import requests
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

from etl import config, db

log = logging.getLogger("parks")

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
PARKS_CACHE = config.REFERENCE / "moscow_parks.json"

# Порог «настоящего» парка, гектары.
#
# OpenStreetMap метит тегом leisure=park и Измайловский лес, и сквер между
# двумя домами: медианный контур по Москве — 0.21 га. По расстоянию до
# любого парка «зелень рядом» оказывается у 88% объявлений, и переменная
# перестаёт что-либо различать. Десять гектаров отделяют районный парк,
# куда ходят гулять с собакой, от озеленения во дворе: таких контуров в
# Москве 139 против 3685 всего.
LARGE_PARK_HA = 10.0

# Центр проекции — примерно центр Москвы. От него считаются метры.
LAT0, LON0 = 55.75, 37.62
EARTH_R = 6_371_000.0

# Парком считается не только leisure=park: крупнейшие зелёные массивы
# Москвы размечены иначе, и без них картина востока и северо-востока
# города была бы неверной.
# Рамка вместо поиска административной области: запрос по area у Overpass
# тяжёлый и на Москве отваливается по таймауту (504). Рамка покрывает город
# в пределах МКАД с запасом. Новая Москва за её пределами остаётся — это
# осознанное упрощение: аренды там мало, а объявлению без парка в рамке
# расстояние всё равно посчитается, просто до ближайшего попавшего в неё.
BBOX = "55.54,37.31,55.93,37.90"

QUERY = f"""
[out:json][timeout:180];
(
  way["leisure"="park"]({BBOX});
  relation["leisure"="park"]({BBOX});
  way["leisure"="garden"]({BBOX});
  way["leisure"="nature_reserve"]({BBOX});
  relation["leisure"="nature_reserve"]({BBOX});
);
out geom;
"""

# Зеркала Overpass, в порядке убывания надёжности.
#
# gall.openstreetmap.de стоит первым не случайно: балансировщик
# overpass-api.de в сентябре 2026 отвечал 504 даже на запрос в квадрат
# 300 метров, при том что служебный /api/status показывал свободные слоты
# и пустую очередь. Тот же запрос к бэкенду напрямую отрабатывал за
# секунду. То есть дело не в нашем темпе и не в сложности запроса.
OVERPASS_MIRRORS = [
    "https://gall.openstreetmap.de/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
]

MKAD_CACHE = config.REFERENCE / "mkad.json"

# МКАД размечен в OpenStreetMap по имени, а не по ref: тег ref у него
# пустой, а name свой на каждый километр — «МКАД, 84-й километр».
# Запрос по ref возвращает ноль, и это легко принять за пустой результат.
MKAD_QUERY = """
[out:json][timeout:180];
way["highway"~"^(motorway|trunk)$"]["name"~"^МКАД"](55.50,37.30,55.95,37.95);
out geom;
"""


# ---------------------------------------------------------------------
def to_xy(lat: float, lon: float) -> tuple[float, float]:
    """Широта и долгота -> метры в локальной плоской системе."""
    x = math.radians(lon - LON0) * EARTH_R * math.cos(math.radians(LAT0))
    y = math.radians(lat - LAT0) * EARTH_R
    return x, y


def fetch_parks() -> list[dict]:
    """Выгрузить парки Москвы из OpenStreetMap и положить в кеш."""
    elements = _overpass(QUERY)

    parks = []
    for el in elements:
        name = (el.get("tags") or {}).get("name")
        if el["type"] == "way" and el.get("geometry"):
            rings = [el["geometry"]]
        elif el["type"] == "relation":
            # У мультиполигона берём внешние контуры. Каждый обрабатывается
            # как отдельный полигон: для расстояния до ближайшей границы
            # этого достаточно, дырки внутри парка нам не важны.
            rings = [m["geometry"] for m in el.get("members", [])
                     if m.get("role") == "outer" and m.get("geometry")]
        else:
            continue

        for ring in rings:
            if len(ring) >= 4:
                parks.append({
                    "name": name,
                    "coords": [[p["lat"], p["lon"]] for p in ring],
                })

    PARKS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    PARKS_CACHE.write_text(json.dumps(parks, ensure_ascii=False), encoding="utf-8")
    log.info("Контуров парков сохранено: %d", len(parks))
    return parks


def _overpass(query: str) -> list[dict]:
    headers = {"User-Agent": "msk-rent-market/1.0 (research project)",
               "Accept": "application/json"}
    for mirror in OVERPASS_MIRRORS:
        log.info("Запрос к %s ...", mirror)
        try:
            resp = requests.post(mirror, data={"data": query},
                                 headers=headers, timeout=600)
        except requests.RequestException as err:
            log.warning("  не ответил: %s", type(err).__name__)
            continue
        if resp.status_code != 200:
            log.warning("  HTTP %s", resp.status_code)
            continue
        try:
            return resp.json().get("elements", [])
        except json.JSONDecodeError:
            log.warning("  ответ не разбирается как JSON")
    raise RuntimeError("Ни одно зеркало Overpass не ответило. Повторить позже.")


def fetch_mkad() -> list[list[float]]:
    """Точки кольца МКАД из OpenStreetMap."""
    els = _overpass(MKAD_QUERY)
    pts = [[p["lat"], p["lon"]] for e in els for p in (e.get("geometry") or [])]
    if len(pts) < 500:
        raise RuntimeError(f"МКАД вернулся подозрительно коротким: {len(pts)} точек")
    MKAD_CACHE.parent.mkdir(parents=True, exist_ok=True)
    MKAD_CACHE.write_text(json.dumps(pts), encoding="utf-8")
    log.info("Точек кольца МКАД: %d", len(pts))
    return pts


def mkad_polygon():
    """Многоугольник, ограниченный МКАД.

    Строится вогнутой оболочкой по точкам обеих проезжих частей. Сшивать
    624 участка в замкнутое кольцо не нужно: для вопроса «внутри или
    снаружи» достаточно контура, а оболочка даёт его устойчиво.

    Проверка на правдоподобие — по периметру: у настоящего МКАД 108.9 км.
    Площадь внутри получается около 880 км². Это НЕ 1091 км² «Москвы в
    старых границах»: та включает Солнцево, Бутово, Митино и Куркино,
    которые лежат за кольцом, — собственно, ради них фильтр и нужен.
    """
    from shapely import concave_hull
    from shapely.geometry import MultiPoint

    if not MKAD_CACHE.exists():
        raise FileNotFoundError("Кольцо не выгружено: python -m etl.parks --fetch-mkad")
    pts = json.loads(MKAD_CACHE.read_text(encoding="utf-8"))
    hull = concave_hull(MultiPoint([to_xy(lat, lon) for lat, lon in pts]), ratio=0.1)
    km = hull.length / 1000
    if not 95 <= km <= 125:
        raise RuntimeError(f"Периметр кольца {km:.0f} км не похож на МКАД (109 км)")
    return hull


def load_parks() -> list[dict]:
    if not PARKS_CACHE.exists():
        raise FileNotFoundError(
            "Парки не выгружены. Сначала: python -m etl.parks --fetch")
    return json.loads(PARKS_CACHE.read_text(encoding="utf-8"))


def _polygons(parks: list[dict]):
    """Контуры -> валидные многоугольники в метрах, с названиями."""
    for park in parks:
        ring = [to_xy(lat, lon) for lat, lon in park["coords"]]
        try:
            poly = Polygon(ring)
        except Exception:
            continue
        if not poly.is_valid:
            poly = poly.buffer(0)      # чинит самопересечения контура
        if poly.is_empty or poly.area <= 0:
            continue
        yield poly, park.get("name")


def build_index(parks: list[dict]) -> tuple[STRtree, list[str | None]]:
    """Пространственный индекс всех контуров.

    Без индекса каждое объявление сравнивалось бы со всеми парками: на
    26 тысячах объявлений и тысячах контуров это десятки миллионов
    проверок геометрии.
    """
    polygons, names = [], []
    for poly, name in _polygons(parks):
        polygons.append(poly)
        names.append(name)
    return STRtree(polygons), names


def build_large_index(parks: list[dict],
                      min_ha: float = LARGE_PARK_HA) -> tuple[STRtree, list[str | None]]:
    """Индекс крупных парков.

    Контуры сначала склеиваются по названию. Это не косметика: крупные
    парки приходят из OSM мультиполигонами, и каждый внешний контур
    попадает к нам отдельной фигурой. Измайловский парк без склейки
    выглядит как несколько кусков по полсотни гектаров и не проходит
    порог, хотя целиком он в разы больше.
    """
    by_name: dict[str, list] = {}
    anonymous = []
    for poly, name in _polygons(parks):
        if name:
            by_name.setdefault(name, []).append(poly)
        else:
            anonymous.append(poly)

    merged: list[tuple] = []
    for name, parts in by_name.items():
        geom = unary_union(parts) if len(parts) > 1 else parts[0]
        merged.append((geom, name))
    merged.extend((poly, None) for poly in anonymous)

    threshold = min_ha * 10_000        # гектары -> квадратные метры
    polygons, names = [], []
    for geom, name in merged:
        if geom.area >= threshold:
            polygons.append(geom)
            names.append(name)
    return STRtree(polygons), names


def nearest_park(lat: float, lon: float, tree: STRtree,
                 names: list[str | None]) -> tuple[int, str | None]:
    """Расстояние в метрах до ближайшего парка и его название.

    Ноль означает, что точка внутри парка. Это не ошибка: дом действительно
    может стоять на территории парка, и таких в Москве немало.
    """
    point = Point(*to_xy(lat, lon))
    idx = tree.nearest(point)
    geom = tree.geometries.take(idx)
    return int(round(point.distance(geom))), names[idx]


# ---------------------------------------------------------------------
def enrich(source: str | None = None) -> dict:
    parks = load_parks()
    tree, names = build_index(parks)
    large_tree, large_names = build_large_index(parks)
    log.info("Контуров всего: %d, крупнее %.0f га: %d",
             len(names), LARGE_PARK_HA, len(large_names))
    stats = {"updated": 0, "skipped": 0}

    with db.connect() as conn:
        with conn.cursor() as cur:
            if source:
                cur.execute("""select source, offer_id, lat, lon from core.offer
                               where lat is not null and source = %s""", (source,))
            else:
                cur.execute("""select source, offer_id, lat, lon from core.offer
                               where lat is not null""")
            rows = cur.fetchall()

        log.info("Объявлений с координатами: %d", len(rows))
        ring = None
        if MKAD_CACHE.exists():
            ring = mkad_polygon()
            log.info("Кольцо МКАД загружено, площадь внутри %.0f км²",
                     ring.area / 1e6)
        else:
            log.warning("Кольцо МКАД не выгружено, флаг inside_mkad не заполнится: "
                        "python -m etl.parks --fetch-mkad")

        updates = []
        for src, offer_id, lat, lon in rows:
            lat, lon = float(lat), float(lon)
            any_park, _ = nearest_park(lat, lon, tree, names)
            big_park, _ = nearest_park(lat, lon, large_tree, large_names)
            inside = ring.contains(Point(*to_xy(lat, lon))) if ring else None
            updates.append((any_park, big_park, inside, src, offer_id))

        with conn.cursor() as cur:
            cur.executemany(
                """update core.offer
                   set park_distance_m = %s, large_park_distance_m = %s,
                       inside_mkad = %s
                   where source = %s and offer_id = %s""",
                updates,
            )
        conn.commit()
        stats["updated"] = len(updates)
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Расстояние до ближайшего парка")
    parser.add_argument("--fetch", action="store_true",
                        help="выгрузить парки из OpenStreetMap")
    parser.add_argument("--fetch-mkad", action="store_true",
                        help="выгрузить кольцо МКАД из OpenStreetMap")
    parser.add_argument("--source", default=None, help="только этот источник")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.fetch:
        fetch_parks()
        return 0

    if args.fetch_mkad:
        fetch_mkad()
        poly = mkad_polygon()
        log.info("Периметр кольца %.0f км, площадь внутри %.0f км²",
                 poly.length / 1000, poly.area / 1e6)
        return 0

    stats = enrich(args.source)
    log.info("Проставлено расстояний: %d", stats["updated"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
