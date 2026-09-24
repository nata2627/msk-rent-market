"""Тесты разбора официальной выгрузки ЦИАНа в Excel.

Площадка кладёт по нескольку значений в одну ячейку через запятую, и
разбирать их надо по смыслу, а не по позиции: состав и порядок кусков
у разных объявлений различаются.
"""
import pytest

from etl.xlsx_import import (
    parse_amenities_cell,
    parse_area_cell,
    parse_counted_cell,
    parse_house_cell,
    parse_metro_cell,
    parse_pets_cell,
    parse_price_cell,
    parse_rooms_cell,
    row_to_records,
)

PRICE = ("280000.0 руб./ За месяц, Залог - 280000 руб., "
         "Коммунальные услуги включены, Срок аренды - Длительный, Предоплата 1 мес")


# ---------------------------------------------------------------------
def test_price_cell_is_split_into_separate_conditions():
    out = parse_price_cell(PRICE)
    assert out["price"] == 280000.0
    assert out["deposit_rub"] == 280000.0
    assert out["prepay_months"] == 1
    assert out["utilities_included"] is True
    assert out["lease_term_type"] == "longTerm"
    assert out["payment_period"] == "month"


def test_deposit_can_differ_from_price():
    """Залог не обязан равняться месячной плате, и приравнивать их нельзя."""
    out = parse_price_cell("89000.0 руб./ За месяц, Залог - 99000 руб.")
    assert out["price"] == 89000.0 and out["deposit_rub"] == 99000.0


def test_missing_deposit_is_none_not_zero():
    """Ноль означал бы «залога нет», а на самом деле про него не сказано."""
    assert "deposit_rub" not in parse_price_cell("60000.0 руб./ За месяц")


def test_utilities_not_included_is_not_read_as_included():
    """Подстрока «включены» есть в обеих формулировках, поэтому отрицание
    проверяется первым. Ошибка здесь завысила бы реальную стоимость месяца."""
    out = parse_price_cell("60000 руб./ За месяц, Коммунальные услуги не включены")
    assert out["utilities_included"] is False


def test_few_months_term_is_distinguished_from_long_term():
    out = parse_price_cell("50000 руб./ За месяц, Срок аренды - На несколько месяцев")
    assert out["lease_term_type"] == "fewMonths"
    assert out["is_daily"] is False


def test_commission_percent_is_extracted():
    out = parse_price_cell("60000 руб./ За месяц, Комиссия 50%")
    assert out["client_fee_pct"] == 50.0


# ---------------------------------------------------------------------
def test_area_cell_holds_three_numbers_separated_by_slashes():
    """Площадь лежит одной строкой «общая/жилая/кухня». Первая версия
    разбирала ячейку как одно число и молча теряла площадь у 165 объявлений
    из 200 — а это главная объясняющая переменная всей регрессии."""
    assert parse_area_cell("320.0/230.0/30.0") == {
        "total_area": 320.0, "living_area": 230.0, "kitchen_area": 30.0}


def test_single_number_is_the_total_area():
    assert parse_area_cell("63.0") == {"total_area": 63.0}


def test_two_numbers_are_total_and_living():
    assert parse_area_cell("70.0/35.0") == {"total_area": 70.0, "living_area": 35.0}


def test_empty_area_cell_gives_nothing_not_zero():
    assert parse_area_cell(None) == {}


def test_zero_area_is_treated_as_missing():
    """Ноль квадратных метров — это не квартира-невидимка, это незаполненное
    поле. Оставить ноль значило бы делить на него при расчёте цены за метр."""
    assert "living_area" not in parse_area_cell("63.0/0/9.0")
    assert parse_area_cell("63.0/0/9.0")["kitchen_area"] == 9.0


def test_rooms_cell_keeps_number_and_layout():
    assert parse_rooms_cell("2, Изолированная")["rooms_count"] == 2
    assert parse_rooms_cell("2, Изолированная")["flat_type"] == "rooms"


def test_bare_room_count_still_parses():
    assert parse_rooms_cell("3") == {"rooms_count": 3}


def test_empty_rooms_cell_does_not_crash():
    assert parse_rooms_cell(None) == {}


def test_house_cell_gives_floor_floors_and_material():
    out = parse_house_cell("42/75, Монолитный")
    assert out == {"floor": 42, "floors_total": 75, "building_material": "Монолитный"}


def test_house_without_material():
    assert parse_house_cell("45/75") == {"floor": 45, "floors_total": 75}


def test_metro_cell_splits_name_time_and_mode():
    out = parse_metro_cell("м. Деловой центр (3 мин пешком)")
    assert out == [{"station_name": "Деловой центр", "time_min": 3,
                    "transport_type": "walk", "line_name": None}]


def test_metro_by_car_is_not_counted_as_walking():
    """Пятнадцать минут пешком и пятнадцать на машине — разные блага."""
    out = parse_metro_cell("м. Саларьево (15 мин на машине)")
    assert out[0]["transport_type"] == "transport"


def test_counted_cell_reads_quantities():
    assert parse_counted_cell("Совмещенный (1), Раздельный (2)") == {
        "совмещенный": 1, "раздельный": 2}


# ---------------------------------------------------------------------
def test_amenities_absent_from_a_filled_list_are_false():
    out = parse_amenities_cell("Мебель в комнатах, Холодильник")
    assert out["has_furniture"] is True
    assert out["has_fridge"] is True
    assert out["has_washer"] is False


def test_empty_amenities_cell_is_unknown_not_absent():
    """«Не указано» и «нет стиральной машины» — разные вещи, по той же
    причине, по которой у животных четыре состояния, а не два."""
    out = parse_amenities_cell(None)
    assert all(v is None for v in out.values())


# ---------------------------------------------------------------------
def test_platform_checkbox_wins_over_text():
    out = parse_pets_cell("Можно с детьми, Можно с животными", "Без животных.")
    assert out["pets_state"] == "allowed"
    assert out["pets_source"] == "platform_field"
    assert out["pets_platform_allowed"] is True


def test_missing_checkbox_is_not_a_prohibition():
    """У ЦИАНа в анкете есть только галочка «можно с животными».
    Состояния «запрещено» в ней нет вовсе, поэтому пустая ячейка означает
    «не отметили», а не «нельзя». Прочитать её как запрет значило бы
    выдумать данные."""
    out = parse_pets_cell("Можно с детьми", "Светлая квартира.")
    assert out["pets_state"] == "unknown"
    assert out["pets_platform_allowed"] is False
    assert out["children_allowed"] is True


def test_text_fills_in_where_the_checkbox_is_silent():
    out = parse_pets_cell(None, "С животными по договорённости.")
    assert out["pets_state"] == "negotiable" and out["pets_source"] == "text"


def test_children_flag_is_read_separately():
    out = parse_pets_cell("Можно с животными", None)
    assert out["children_allowed"] is False


# ---------------------------------------------------------------------
def test_full_row_maps_to_panel_records():
    row = {
        "ID  объявления": 291111695,
        "Количество комнат": "2, Изолированная",
        "Метро": "м. Деловой центр (3 мин пешком)",
        "Адрес": "Москва, 1-й Красногвардейский проезд, 15",
        "Площадь, м2": 63.0,
        "Дом": "42/75, Монолитный",
        "Цена": PRICE,
        "Описание": "Хорошая квартира.",
        "Ремонт": "Дизайнерский",
        "Санузел": "Совмещенный (1), Раздельный (1)",
        "Можно с детьми/животными": "Можно с детьми, Можно с животными",
        "Дополнительно": "Мебель в комнатах, Стиральная машина",
        "Лифт": "Пасс (4), Груз (2)",
        "Ссылка на объявление": "https://www.cian.ru/rent/flat/291111695",
    }
    import datetime
    rec = row_to_records(row, datetime.date(2026, 9, 22))
    offer, obs = rec["offer"], rec["observation"]
    assert offer["offer_id"] == "291111695"
    assert offer["rooms_count"] == 2 and offer["total_area"] == 63.0
    assert offer["living_area"] is None
    assert offer["floor"] == 42 and offer["floors_total"] == 75
    assert offer["metro_walk_min"] == 3
    assert offer["has_lift"] is True
    assert obs["price"] == 280000.0
    assert obs["pets_state"] == "allowed"
    assert obs["combined_wc_count"] == 1 and obs["separate_wc_count"] == 1


def test_export_has_no_coordinates_and_we_do_not_invent_them():
    """Координат в выгрузке нет. Вывести их из строки адреса без геокодера
    нельзя, и подставлять что-либо означало бы выдумать данные."""
    import datetime
    rec = row_to_records({"ID  объявления": 1, "Адрес": "Москва, Тверская, 1"},
                         datetime.date(2026, 9, 22))
    assert rec["offer"]["lat"] is None and rec["offer"]["lon"] is None
    assert rec["offer"]["district"] is None


def test_row_without_id_is_skipped():
    import datetime
    assert row_to_records({"Адрес": "Москва"}, datetime.date(2026, 9, 22)) is None
