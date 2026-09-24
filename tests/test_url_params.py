"""Тесты сборки адреса публичной страницы выдачи.

Имена параметров взяты из самой площадки: в состоянии архивной страницы
лежит paginationUrls, который ЦИАН строит себе сам, и в нём видно и набор
ключей, и то, что номер страницы передаётся как p.
"""
from etl import config
from etl.partition import Bucket, build_url_params, check_room_codes


def test_base_parameters_match_what_the_platform_builds_itself():
    p = build_url_params(Bucket())
    assert p["deal_type"] == "rent"
    assert p["offer_type"] == "flat"
    assert p["engine_version"] == 2
    assert p["region"] == config.REGION_MOSCOW


def test_neighbouring_regions_are_excluded():
    """Подмосковье — другой рынок. Если оно подмешается в выдачу, московские
    медианы окажутся занижены, и заметить это будет нечем."""
    assert build_url_params(Bucket())["with_neighbors"] == 0


def test_long_term_rent_type():
    """type=4 — длительная аренда, type=2 — посуточная. На архивном снимке
    с type=2 все 28 объявлений оказались посуточными."""
    assert build_url_params(Bucket())["type"] == 4


def test_first_page_has_no_page_parameter():
    """Площадка сама строит первую ссылку без p — повторяем за ней, чтобы
    адрес совпадал с настоящим и не плодил лишних вариантов страницы."""
    assert "p" not in build_url_params(Bucket(), page=1)


def test_pagination_parameter_is_p():
    assert build_url_params(Bucket(), page=7)["p"] == 7


def test_rooms_become_separate_flags():
    """Комнатность задаётся отдельным параметром на каждое значение:
    room1=1, room2=1 — так видно в сохранённых адресах."""
    assert build_url_params(Bucket(rooms="2"))["room2"] == 1
    assert "room1" not in build_url_params(Bucket(rooms="2"))


def test_price_bounds_are_passed_as_cut_points():
    p = build_url_params(Bucket(price_min=60_000, price_max=119_999))
    assert p["minprice"] == 60_000 and p["maxprice"] == 119_999


def test_unbounded_bucket_has_no_price_parameters():
    """У корневой корзины границ нет — значит и фильтра быть не должно."""
    p = build_url_params(Bucket(rooms="1"))
    assert "minprice" not in p and "maxprice" not in p


def test_pets_filter_is_opt_in_only():
    assert "pets" not in build_url_params(Bucket(rooms="1"))
    assert build_url_params(Bucket(rooms="1"), pets_only=True)["pets"] == 1


def test_district_is_indexed():
    assert build_url_params(Bucket(district_id=21))["district[0]"] == 21


# ---------------------------------------------------------------------
def test_room_codes_are_checked_against_known_counts():
    """Контрольные числа сняты со страницы поиска без фильтров вручную.
    Если запрос с room1 вернёт не около семи тысяч, значит параметр
    означает не то, что мы думаем, — и вся комнатность собрана неверно."""
    assert check_room_codes({"1": 7_100, "2": 9_400}) == []


def test_room_code_mismatch_is_caught():
    assert "1" in check_room_codes({"1": 200})


def test_tolerance_allows_a_living_market_to_drift():
    """Допуск широкий намеренно: рынок меняется день ото дня, и ловить надо
    ошибку в разы, а не расхождение в проценты."""
    assert check_room_codes({"1": 8_000}) == []
