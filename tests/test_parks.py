"""Тесты расчёта расстояния до парка.

Проверяется то, что легко сделать неправильно и трудно заметить:
проекция, расстояние до границы вместо центра и порог «настоящего» парка.
"""
import math

import pytest

from etl.parks import LARGE_PARK_HA, build_index, build_large_index, nearest_park, to_xy


def square(lat, lon, side_m):
    """Квадратный контур со стороной side_m вокруг точки."""
    d_lat = side_m / 2 / 111_320
    d_lon = side_m / 2 / (111_320 * math.cos(math.radians(lat)))
    return [[lat - d_lat, lon - d_lon], [lat - d_lat, lon + d_lon],
            [lat + d_lat, lon + d_lon], [lat + d_lat, lon - d_lon],
            [lat - d_lat, lon - d_lon]]


# ---------------------------------------------------------------------
def test_projection_origin_is_zero():
    x, y = to_xy(55.75, 37.62)
    assert abs(x) < 1 and abs(y) < 1


def test_one_degree_of_latitude_is_about_111_km():
    _, y = to_xy(56.75, 37.62)
    assert 110_000 < y < 112_000


def test_longitude_is_compressed_by_latitude():
    """На широте Москвы градус долготы примерно вдвое короче градуса
    широты. Забыть косинус — значит растянуть город по горизонтали
    и получить неверные расстояния."""
    x, _ = to_xy(55.75, 38.62)
    assert 61_000 < x < 64_000


# ---------------------------------------------------------------------
def test_distance_is_measured_to_the_border_not_the_centre():
    """Для большого парка разница в километры: дом у ворот и дом
    у середины парка — разные вещи."""
    park = [{"name": "Большой", "coords": square(55.75, 37.62, 2000)}]
    tree, names = build_index(park)
    # Точка в 200 м за восточной границей (граница в 1000 м от центра).
    east = 37.62 + 1200 / (111_320 * math.cos(math.radians(55.75)))
    distance, name = nearest_park(55.75, east, tree, names)
    assert 150 < distance < 260
    assert name == "Большой"


def test_point_inside_a_park_is_zero():
    """Дом действительно может стоять на территории парка. Ноль — это
    не ошибка расчёта."""
    park = [{"name": "Вокруг", "coords": square(55.75, 37.62, 2000)}]
    tree, names = build_index(park)
    assert nearest_park(55.75, 37.62, tree, names)[0] == 0


def test_nearest_of_several_parks_is_chosen():
    parks = [{"name": "Далёкий", "coords": square(55.85, 37.62, 500)},
             {"name": "Близкий", "coords": square(55.7505, 37.62, 200)}]
    tree, names = build_index(parks)
    assert nearest_park(55.75, 37.62, tree, names)[1] == "Близкий"


# ---------------------------------------------------------------------
def test_small_squares_are_excluded_from_large_parks():
    """Медианный контур leisure=park в Москве — 0.21 га. Если считать
    парком и его, «зелень рядом» окажется у 88% объявлений, и переменная
    перестанет что-либо различать."""
    tiny = [{"name": "Сквер", "coords": square(55.75, 37.62, 100)}]   # 1 га
    _, names = build_large_index(tiny)
    assert names == []


def test_big_park_passes_the_threshold():
    big = [{"name": "Парк", "coords": square(55.75, 37.62, 600)}]     # 36 га
    _, names = build_large_index(big)
    assert names == ["Парк"]


def test_parts_of_one_park_are_merged_by_name():
    """Крупные парки приходят из OSM мультиполигонами, и каждый внешний
    контур попадает отдельной фигурой. Без склейки Измайловский выглядит
    как несколько кусков и не проходит порог, хотя целиком он огромен."""
    side = 240   # 5.76 га — поодиночке ниже порога в 10 га
    halves = [
        {"name": "Разрезанный", "coords": square(55.7500, 37.6200, side)},
        {"name": "Разрезанный", "coords": square(55.7500, 37.6238, side)},
    ]
    assert build_large_index(halves)[1] == ["Разрезанный"]
    # Те же два куска под разными именами не склеиваются и порог не проходят.
    separate = [dict(halves[0]), dict(halves[1], name="Другой")]
    assert build_large_index(separate)[1] == []


def test_threshold_is_explicit_and_documented():
    assert LARGE_PARK_HA == 10.0


def test_invalid_ring_does_not_crash_the_index():
    """Контуры в OSM бывают самопересекающимися. Падение здесь остановило
    бы обогащение всей выборки из-за одного кривого парка."""
    bowtie = [{"name": "Бантик", "coords": [[55.75, 37.62], [55.76, 37.63],
                                            [55.75, 37.63], [55.76, 37.62],
                                            [55.75, 37.62]]}]
    tree, names = build_index(bowtie)
    assert len(names) <= 1
