"""Тесты схлопывания дублей.

Главное, что здесь проверяется, — консервативность. Пропущенный дубль
даёт небольшое смещение медианы; склеенные разные квартиры это удалённые
из выборки наблюдения, и такую ошибку уже ничем не исправить. Поэтому
тестов на «не склеил лишнего» больше, чем на «нашёл дубль».
"""
import pytest

from src.dedup import (
    AREA_TOLERANCE,
    TEXT_THRESHOLD,
    UnionFind,
    attributes_match,
    decide,
    find_duplicates,
    minhash_similarity,
)


def offer(oid, *, lat=55.75, lon=37.62, rooms=2, area=54.0, floor=7,
          minhash=None, price=90000, seller="Агентство"):
    return {"offer_id": oid, "lat": lat, "lon": lon, "rooms_count": rooms,
            "total_area": area, "floor": floor,
            "minhash": minhash if minhash is not None else list(range(32)),
            "price": price, "seller_name": seller, "district": "Тестовый",
            "first_seen_date": None}


# ---------------------------------------------------------------------
# Похожесть описаний
# ---------------------------------------------------------------------
def test_identical_signatures_are_fully_similar():
    assert minhash_similarity(list(range(32)), list(range(32))) == 1.0


def test_disjoint_signatures_are_not_similar():
    assert minhash_similarity(list(range(32)), list(range(100, 132))) == 0.0


def test_half_matching_signature():
    a = list(range(32))
    b = list(range(16)) + list(range(100, 116))
    assert minhash_similarity(a, b) == 0.5


def test_missing_signature_is_not_similarity_zero_by_accident():
    """Отсутствие подписи не должно выглядеть как «тексты разные»:
    оно означает «сравнить нечем». Ноль здесь безопасен только потому,
    что сам по себе текст ничего не решает — нужны ещё характеристики."""
    assert minhash_similarity(None, list(range(32))) == 0.0
    assert minhash_similarity([], []) == 0.0


def test_signatures_of_different_length_are_not_compared():
    assert minhash_similarity([1, 2, 3], list(range(32))) == 0.0


# ---------------------------------------------------------------------
# Характеристики
# ---------------------------------------------------------------------
def test_same_flat_attributes_match():
    assert attributes_match(offer("a"), offer("b"))


def test_area_tolerance_absorbs_rounding():
    """Агентства округляют площадь по-разному: 54.3 у одного, 54 у другого."""
    assert attributes_match(offer("a", area=54.3), offer("b", area=54.0))


def test_area_difference_beyond_tolerance_is_a_different_flat():
    assert not attributes_match(offer("a", area=54.0),
                                offer("b", area=54.0 + AREA_TOLERANCE + 0.1))


def test_different_floor_is_a_different_flat():
    assert not attributes_match(offer("a", floor=7), offer("b", floor=8))


def test_different_rooms_is_a_different_flat():
    assert not attributes_match(offer("a", rooms=1), offer("b", rooms=2))


def test_unknown_floor_never_matches():
    """Без этажа отличить квартиру от соседней сверху нечем. Считать
    такую пару совпавшей — прямой путь склеить разные квартиры."""
    assert not attributes_match(offer("a", floor=None), offer("b", floor=None))


def test_unknown_area_never_matches():
    assert not attributes_match(offer("a", area=None), offer("b", area=None))


# ---------------------------------------------------------------------
# Решение по паре
# ---------------------------------------------------------------------
def test_everything_matches_is_a_duplicate():
    verdict, reason, _ = decide(offer("a"), offer("b"))
    assert verdict == "duplicate" and reason == "характеристики+цена+текст"


def test_same_attributes_and_price_is_enough():
    a = offer("a", minhash=list(range(32)))
    b = offer("b", minhash=list(range(100, 132)))
    verdict, reason, _ = decide(a, b)
    assert verdict == "duplicate" and reason == "характеристики+цена"


def test_same_attributes_and_text_is_enough():
    """Агентства ставят разные цены на одну квартиру — своя наценка."""
    verdict, reason, _ = decide(offer("a", price=90000), offer("b", price=95000))
    assert verdict == "duplicate" and reason == "характеристики+текст"


def test_attributes_alone_is_only_a_suspect():
    """В типовых сериях две одинаковые квартиры на одном этаже — норма.
    Схлопывать их нельзя, но и молчать нельзя: это остаточная доля."""
    a = offer("a", price=90000, minhash=list(range(32)))
    b = offer("b", price=95000, minhash=list(range(100, 132)))
    verdict, reason, _ = decide(a, b)
    assert verdict == "suspect" and reason == "только характеристики"


def test_identical_text_without_matching_attributes_is_not_a_duplicate():
    """Самая опасная ловушка: застройщик разместил один и тот же текст
    на разные квартиры. На реальных данных таких пар 178 — больше, чем
    настоящих дублей с совпавшим текстом."""
    verdict, _, _ = decide(offer("a", area=54.0), offer("b", area=80.0))
    assert verdict == "different"


def test_identical_price_without_matching_attributes_is_not_a_duplicate():
    """Круглые цены совпадают сплошь и рядом: на реальных данных 2082
    пары с одинаковой ценой в одном доме — это просто 90 000 рублей."""
    verdict, _, _ = decide(offer("a", rooms=1), offer("b", rooms=3))
    assert verdict == "different"


def test_text_threshold_is_not_on_a_knife_edge():
    """Порог стоит в пустой зоне распределения: основная масса пар ниже
    0.3, совпадающие тексты выше 0.8."""
    assert 0.5 < TEXT_THRESHOLD < 0.95


# ---------------------------------------------------------------------
# Сборка групп
# ---------------------------------------------------------------------
def test_chain_of_pairs_becomes_one_group():
    """Квартира у трёх агентств даёт пары (A,B) и (B,C). Это одна группа
    из трёх, а не две группы по две."""
    uf = UnionFind()
    uf.union("a", "b")
    uf.union("b", "c")
    assert uf.groups() == {"a": ["a", "b", "c"]}


def test_canonical_does_not_depend_on_order():
    """Иначе повторный запуск менял бы представителя группы, и ссылки
    на «каноническое» объявление разъезжались бы между прогонами."""
    first, second = UnionFind(), UnionFind()
    first.union("b", "a")
    second.union("a", "b")
    assert first.groups() == second.groups()


def test_singletons_are_not_groups():
    uf = UnionFind()
    uf.find("lonely")
    assert uf.groups() == {}


# ---------------------------------------------------------------------
# Блоки
# ---------------------------------------------------------------------
def test_offers_in_different_buildings_are_never_compared():
    """Сравнение всех пар на 25 тысячах объявлений — это 316 миллионов
    сравнений. Разбиение по домам сводит их к тридцати тысячам."""
    far = offer("b", lat=55.90, lon=37.80)
    uf, suspects, stats, _ = find_duplicates([offer("a"), far])
    assert stats["пар рассмотрено"] == 0
    assert uf.groups() == {} and suspects == []


def test_same_building_pairs_are_compared():
    uf, _, stats, _ = find_duplicates([offer("a"), offer("b")])
    assert stats["пар рассмотрено"] == 1
    assert uf.groups() == {"a": ["a", "b"]}


def test_coordinate_rounding_keeps_one_building_together():
    """Разные агентства ставят чуть разные координаты одного дома.
    Округление до четвёртого знака — около одиннадцати метров."""
    uf, _, _, _ = find_duplicates([offer("a", lat=55.750001),
                                   offer("b", lat=55.750002)])
    assert uf.groups() == {"a": ["a", "b"]}
