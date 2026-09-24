"""Справочник районов, собираемый из скачанных страниц.

Цена ошибки здесь несимметрична. Лишняя корзина стоит запросов; пропущенный
район означает, что его объявления не попадут в выборку вообще и этого никто
не заметит. Поэтому проверяем обе стороны отдельно.
"""
from etl.districts import LEAF_TYPES, build, okrug_of


def node(node_id, name, node_type, parent_id=None):
    return {"id": node_id, "name": name, "type": node_type,
            "parent_id": parent_id, "offers": 1}


MOSCOW = {
    8: node(8, "ЮВАО", "okrug"),
    77: node(77, "Марьино", "raion", 8),
    325: node(325, "НАО (Новомосковский)", "okrug"),
    332: node(332, "Московский", "poselenie", 325),
    # Микрорайон лежит внутри района: как корзина он повторил бы уже
    # собранное.
    900: node(900, "Марьинский парк", "mikroraion", 77),
    # Зонтик над целым округом: тип района, родителя нет.
    4334: node(4334, "Новая Москва", "raion"),
}


def test_leaf_types_do_not_include_mikroraion():
    # Не косметика: микрорайон вложен в район, и обход по обоим уровням
    # запросил бы одни и те же объявления дважды.
    assert "mikroraion" not in LEAF_TYPES


def test_build_keeps_raion_and_poselenie():
    districts, _ = build(MOSCOW)
    assert {d["name"] for d in districts} == {"Марьино", "Московский"}


def test_poselenie_is_not_dropped():
    # Новая Москва — это 21 поселение и больше семи тысяч объявлений.
    # Потерять их из-за того, что тип узла не «raion», нельзя.
    districts, _ = build(MOSCOW)
    assert any(d["type"] == "poselenie" for d in districts)


def test_umbrella_node_is_excluded_and_reported():
    districts, umbrellas = build(MOSCOW)
    assert "Новая Москва" not in {d["name"] for d in districts}
    assert umbrellas == ["Новая Москва"]


def test_okrug_resolved_through_the_chain():
    assert okrug_of(MOSCOW[77], MOSCOW) == "ЮВАО"
    assert okrug_of(MOSCOW[332], MOSCOW) == "НАО (Новомосковский)"
    # Микрорайон -> район -> округ: родитель не округ, идём выше.
    assert okrug_of(MOSCOW[900], MOSCOW) == "ЮВАО"


def test_cycle_in_parents_does_not_hang():
    # Битые данные не должны подвешивать сбор: справочник важен, но не
    # настолько, чтобы ради него зависнуть.
    looped = {
        1: node(1, "А", "raion", 2),
        2: node(2, "Б", "raion", 1),
    }
    assert okrug_of(looped[1], looped) is None


def test_missing_parent_is_not_an_error():
    orphan = {5: node(5, "Где-то", "raion", 999)}
    assert okrug_of(orphan[5], orphan) is None
