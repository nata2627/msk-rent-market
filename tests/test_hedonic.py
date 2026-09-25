"""Построение матрицы плана для гедонической регрессии.

Проверяется не арифметика (она в test_linreg), а то, из чего модель
собрана: где база у категориальных переменных, что происходит с
пропусками и в каком порядке идут столбцы. Ошибка здесь не уронит
программу — она тихо изменит смысл коэффициента.
"""
import math

import numpy as np

from src.hedonic import _dummies, _numeric_with_missing, build, cell_key, controls


def row(**kw):
    base = dict(price=80000, total_area=40.0, rooms_count=1, floor=3,
                floors_total=9, build_year=2005, building_material="panel",
                metro_walk_min=10, large_park_distance_m=800, inside_mkad=True,
                is_apartments=False, has_furniture=True, has_washer=True,
                has_dishwasher=False, has_conditioner=False, has_lift=True,
                photos_count=12, seller_type="homeowner", flat_type="flat",
                district="Марьино", pets_state="unknown", pets_source="none",
                pets_platform_allowed=False)
    base.update(kw)
    return base


def test_dummies_omit_the_named_base():
    # База задаётся явно: иначе при новом сборе она сменится сама собой
    # и все коэффициенты начнут значить другое.
    cols, names = _dummies(["a", "b", "c", "b"], base="b", prefix="x")
    assert names == ["x:a", "x:c"]
    assert list(cols[0]) == [1.0, 0.0, 0.0, 0.0]


def test_dummies_add_explicit_missing_level():
    _, names = _dummies(["a", None, "c"], base="a", prefix="x")
    assert "x:нет данных" in names


def test_missing_numeric_is_filled_by_median_with_a_flag():
    # Объявление с неизвестным расстоянием до метро остаётся в выборке,
    # но модель знает, что значение подставлено.
    cols, names = _numeric_with_missing([10, 20, None, 30], "метро")
    assert names == ["метро", "метро:нет данных"]
    assert cols[0][2] == 20.0
    assert list(cols[1]) == [0.0, 0.0, 1.0, 0.0]


def test_no_missing_means_no_extra_flag():
    _, names = _numeric_with_missing([1, 2, 3], "метро")
    assert names == ["метро"]


def test_treatment_stands_left_of_controls():
    # Порядок столбцов решает, что выбросят при линейной зависимости.
    # Изучаемый признак должен уцелеть в любом случае.
    rows = [row(), row(rooms_count=2, price=110000)]
    treat = np.array([1.0, 0.0])
    _, _, names = build(rows, treat, "животные", use_controls=True)
    assert names[0] == "константа"
    assert names[1] == "животные"


def test_dependent_variable_is_log_price():
    rows = [row(price=100000)]
    y, _, _ = build(rows, np.array([0.0]), "животные", use_controls=False)
    assert abs(y[0] - math.log(100000)) < 1e-12


def test_rooms_are_capped_so_rare_sizes_do_not_make_lonely_dummies():
    rows = [row(rooms_count=1), row(rooms_count=4), row(rooms_count=9)]
    _, names = controls(rows)
    assert "комнат:4" in names
    assert "комнат:9" not in names


def test_matching_cell_groups_close_flats_and_separates_districts():
    a = row(total_area=41.0, metro_walk_min=7)
    b = row(total_area=48.0, metro_walk_min=9)     # та же ячейка
    c = row(total_area=41.0, metro_walk_min=7, district="Раменки")
    d = row(total_area=55.0, metro_walk_min=7)     # другая корзина площади
    assert cell_key(a) == cell_key(b)
    assert cell_key(a) != cell_key(c)
    assert cell_key(a) != cell_key(d)


def test_missing_metro_gets_its_own_cell_not_a_silent_merge():
    # Пропуск не должен склеиваться с «до пяти минут» — иначе объявления
    # без метро молча попадут к самым удобным.
    assert cell_key(row(metro_walk_min=None)) != cell_key(row(metro_walk_min=2))
