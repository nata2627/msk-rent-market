"""Тесты нарезки поиска на корзины.

Главное, что здесь проверяется, — не работоспособность кода, а инвариант
выборки: диапазоны цены идут встык и покрывают всю шкалу. Если он нарушится,
часть объявлений не попадёт в сбор, выборка окажется обрезанной по цене, и
все оценки гедонистической регрессии сместятся — причём незаметно, потому
что данные будут выглядеть нормальными.
"""
import pytest

from etl import config
from etl.partition import (
    Bucket,
    build_query,
    covers_whole_scale,
    root_buckets,
    split,
    split_price,
)

DISTRICTS = [
    {"id": 21, "name": "Алексеевский", "okrug": "СВАО"},
    {"id": 33, "name": "Филёвский парк", "okrug": "ЗАО"},
]


# ---------------------------------------------------------------------
# Диапазоны цены
# ---------------------------------------------------------------------
def test_unbounded_range_splits_into_two_contiguous_parts():
    parts = split_price(None, None)
    assert parts == [(0, config.PRICE_FIRST_SPLIT - 1), (config.PRICE_FIRST_SPLIT, None)]
    assert covers_whole_scale(parts)


def test_open_top_range_doubles_the_lower_bound():
    """Хвост дорогих квартир проходится крупными шагами, иначе дробление
    тратит запросы на диапазоны, где почти ничего нет."""
    assert split_price(60_000, None) == [(60_000, 119_999), (120_000, None)]


def test_finite_range_splits_at_geometric_mean():
    """Геометрическое среднее, а не арифметическое: ставки распределены
    примерно логнормально, и по линейной шкале половины вышли бы неравными."""
    low, high = split_price(30_000, 300_000)
    assert low == (30_000, 94_867)
    assert high == (94_868, 300_000)
    assert abs(low[1] + 1 - int((30_000 * 300_000) ** 0.5)) == 0


def test_split_from_zero_does_not_collapse_to_the_bottom():
    """Геометрическое среднее от нуля схлопывается к корню: середина между
    0 и 60 000 выходила 244 рубля, и на живом обходе четыре запроса подряд
    вернули по нулю объявлений. Точка разреза считается от опоры снизу."""
    low, high = split_price(0, 59_999)
    assert 25_000 < low[1] < 45_000
    assert low[1] + 1 == high[0]          # встык, покрытие не пострадало
    assert low[0] == 0 and high[1] == 59_999


def test_narrow_range_is_not_split_further():
    """Ниже минимальной ширины дробление упёрлось бы в объявления с
    одинаковой ценой и не сходилось бы."""
    assert split_price(50_000, 50_500) == []


def test_parts_are_contiguous_no_gaps_no_overlaps():
    lo, hi = split_price(20_000, 200_000)
    assert lo[1] + 1 == hi[0]
    assert lo[0] == 20_000 and hi[1] == 200_000


def _fake_market(lo: int, hi: int | None) -> int:
    """Сколько объявлений «лежит» в диапазоне цены у придуманного рынка.

    Логнормальное по форме: масса сосредоточена между 40 и 150 тысячами,
    дорогой хвост тонкий. Нужно, чтобы тест воспроизводил то, чем дробление
    управляется на самом деле, — заявленное площадкой число результатов.
    """
    import math

    def cdf(x: int | None) -> float:
        if x is None:
            return 1.0
        if x <= 0:
            return 0.0
        # Приблизительно: медиана 70 тыс., сигма 0.55 по логарифму.
        z = (math.log(x) - math.log(70_000)) / 0.55
        return 0.5 * (1 + math.erf(z / math.sqrt(2)))

    return round(40_000 * (cdf(hi) - cdf(lo)))


def test_recursive_price_split_covers_whole_scale():
    """Дробление до тех пор, пока корзина не влезет в предел выдачи, покрывает
    всю шкалу цен без дыр и пересечений.

    Это главный инвариант проекта: дыра в покрытии означала бы, что часть
    объявлений не собрана, выборка обрезана по цене — то есть по объясняемой
    переменной, — и оценки регрессии смещены незаметно для глаза."""
    leaves: list[tuple[int, int | None]] = []
    stack: list[tuple[int | None, int | None]] = [(None, None)]

    while stack:
        lo, hi = stack.pop()
        if _fake_market(lo or 0, hi) <= config.SPLIT_THRESHOLD:
            leaves.append((lo or 0, hi))
            continue
        children = split_price(lo, hi)
        if not children:
            leaves.append((lo or 0, hi))   # делить нечем, корзина усечена
            continue
        stack.extend(children)

    assert covers_whole_scale(leaves)
    assert all(_fake_market(lo, hi) <= config.SPLIT_THRESHOLD
               or split_price(lo, hi) == []
               for lo, hi in leaves)


def test_covers_whole_scale_rejects_a_gap():
    assert not covers_whole_scale([(0, 50_000), (50_002, None)])


def test_covers_whole_scale_rejects_bounded_tail():
    """Если верхний диапазон закрыт, самые дорогие квартиры выпадут из
    выборки — а это отсечение по объясняемой переменной."""
    assert not covers_whole_scale([(0, 59_999), (60_000, 500_000)])


# ---------------------------------------------------------------------
# Порядок осей
# ---------------------------------------------------------------------
def test_first_axis_is_district():
    children = split(Bucket(), DISTRICTS)
    assert [c.district for c in children] == ["Алексеевский", "Филёвский парк"]
    assert all(c.depth == 1 for c in children)


def test_second_axis_is_rooms():
    children = split(Bucket(district_id=21, district="Алексеевский"), DISTRICTS)
    assert [c.rooms for c in children] == config.ROOM_CODES


def test_all_room_categories_are_covered_exactly_once():
    """Студии (7) и свободная планировка (9) должны попасть в сбор: это
    часть рынка, и молча потерять её нельзя."""
    codes = [c.rooms for c in split(Bucket(district_id=21), DISTRICTS)]
    assert len(codes) == len(set(codes))
    assert {"7", "9"} <= set(codes)


def test_third_axis_is_price():
    children = split(Bucket(district_id=21, rooms="2"), DISTRICTS)
    assert [(c.price_min, c.price_max) for c in children] == [
        (0, config.PRICE_FIRST_SPLIT - 1), (config.PRICE_FIRST_SPLIT, None)
    ]


def test_split_returns_empty_when_axes_are_exhausted():
    """Пустой ответ — это сигнал пометить корзину усечённой, а не молча
    потерять её содержимое."""
    exhausted = Bucket(district_id=21, rooms="2", price_min=50_000, price_max=50_400)
    assert split(exhausted, DISTRICTS) == []


def test_split_stops_at_the_depth_limit():
    """Открытый сверху диапазон делится всегда: 60 -> 120 -> 240 тысяч и так
    до бесконечности. В рабочем обходе это безопасно, потому что делением
    управляет заявленное число результатов, и на дорогом хвосте оно быстро
    падает. Но если площадка начнёт всегда заявлять число выше порога,
    остановить рекурсию сможет только предел глубины."""
    deep = Bucket(district_id=21, rooms="2", price_min=60_000, depth=config.MAX_BUCKET_DEPTH)
    assert split(deep, DISTRICTS) == []


def test_recursion_terminates_even_if_the_platform_always_claims_too_much():
    """Худший случай: площадка по каждой корзине заявляет число выше порога.
    Обход обязан завершиться, пусть и пометив корзины усечёнными."""
    stack, leaves, steps = [Bucket()], 0, 0
    while stack and steps < 100_000:
        children = split(stack.pop(), DISTRICTS)
        if children:
            stack.extend(children)
        else:
            leaves += 1
        steps += 1
    assert not stack, "дробление не сошлось"
    assert leaves > 0


def test_root_buckets_start_from_districts_when_available():
    assert len(root_buckets(DISTRICTS)) == 2


def test_root_buckets_degrade_gracefully_without_districts():
    """Без справочника районов покрытие остаётся полным, просто запросов
    понадобится больше."""
    roots = root_buckets([])
    assert roots == [Bucket()]
    assert split(roots[0], []) != []


# ---------------------------------------------------------------------
# Тело запроса
# ---------------------------------------------------------------------
def test_query_never_filters_by_pets():
    """Фильтр по животным на сборе запрещён: без объявлений, где животные
    не разрешены, не будет контрольной группы и надбавку не посчитать."""
    query = build_query(Bucket(district_id=21, rooms="2"))["jsonQuery"]
    assert not any("pet" in key.lower() for key in query)


def test_query_has_no_price_filter_for_an_unbounded_bucket():
    """Цена появляется в запросе только как граница нарезки. У корневой
    корзины границ нет — значит и фильтра быть не должно."""
    assert "price" not in build_query(Bucket(district_id=21, rooms="2"))["jsonQuery"]


def test_query_carries_price_bounds_of_the_bucket():
    query = build_query(Bucket(district_id=21, rooms="2",
                               price_min=60_000, price_max=119_999))["jsonQuery"]
    assert query["price"] == {"type": "range", "value": {"gte": 60_000, "lte": 119_999}}


def test_open_top_bucket_sends_only_lower_bound():
    query = build_query(Bucket(rooms="2", price_min=120_000))["jsonQuery"]
    assert query["price"]["value"] == {"gte": 120_000}


def test_query_targets_moscow_city_and_long_term_rent():
    query = build_query(Bucket())["jsonQuery"]
    assert query["region"]["value"] == [config.REGION_MOSCOW]
    assert query["_type"] == "flatrent"
    assert query["for_day"]["value"] == "!1"


def test_page_number_reaches_the_query():
    assert build_query(Bucket(), page=7)["jsonQuery"]["page"]["value"] == 7


def test_bucket_capacity_matches_platform_limit():
    """Порог дробления обязан быть ниже потолка выдачи, иначе корзина,
    равная потолку, не разделится и молча потеряет хвост."""
    assert config.SPLIT_THRESHOLD < config.BUCKET_CAPACITY
    assert config.BUCKET_CAPACITY == config.PAGE_SIZE * config.MAX_PAGE
