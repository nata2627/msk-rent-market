"""Тесты разбора карточки объявления.

Основной предмет проверки — четыре состояния «животных». Это главная
объясняющая переменная проекта, и ошибка в ней прямо портит ответ на вопрос
«сколько стоит разрешение с животными».
"""
import json
from datetime import date
from pathlib import Path

import pytest

from etl.parse import (
    _num,
    deposit_months_from_text,
    prepay_months_from_text,
    metro_walk_min,
    parse_geo,
    parse_metro,
    parse_offer,
    pets_from_text,
    pets_state,
    seller_type,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "offer_cian.json").read_text(encoding="utf-8")
)
TODAY = date(2026, 9, 22)


@pytest.fixture
def parsed():
    return parse_offer(FIXTURE, TODAY)


# ---------------------------------------------------------------------
# Животные: четыре состояния
# ---------------------------------------------------------------------
def test_platform_flag_true_means_allowed():
    assert pets_state(True, None) == ("allowed", True, "platform_field")


def test_platform_flag_false_means_forbidden():
    assert pets_state(False, None) == ("forbidden", False, "platform_field")


def test_source_of_the_verdict_is_recorded():
    """У трёх способов узнать про животных разная надёжность, и в регрессии
    их нельзя смешивать: метка от фильтра площадки — факт, вычитанное из
    описания — догадка."""
    assert pets_state(None, "С животными по договорённости.")[2] == "text"
    assert pets_state(None, "Светлая квартира.")[2] == "none"
    assert pets_state(True, None)[2] == "platform_field"


def test_missing_flag_and_silent_text_means_unknown():
    """«Не указано» и «запрещено» — разные вещи. Схлопывание их в False
    смешало бы надбавку за животных с надбавкой за заполненную анкету."""
    state, raw, src = pets_state(None, "Светлая квартира с ремонтом.")
    assert state == "unknown" and raw is None and src == "none"


def test_negotiable_comes_only_from_text():
    """Такого состояния в структурированных полях площадки нет вовсе."""
    state, raw, src = pets_state(None, "С животными по договорённости.")
    assert state == "negotiable" and raw is None and src == "text"


def test_text_forbidden_is_recognised():
    assert pets_from_text("Сдаётся без животных.") == "forbidden"


def test_text_allowed_is_recognised():
    assert pets_from_text("Можно с животными, хозяйка не против.") == "allowed"


def test_negotiable_wording_variants():
    for text in ("животные по согласованию",
                 "кошка обсуждается",
                 "с питомцами по договоренности"):
        assert pets_from_text(text) == "negotiable", text


def test_marker_far_from_animals_is_ignored():
    """«Нельзя» рядом с курением не должно превращаться в запрет животных —
    иначе запрет припишется квартирам, где про животных не сказано ничего."""
    assert pets_from_text("Курить в квартире нельзя. Рядом парк.") is None


def test_unrelated_prohibition_in_the_next_sentence_does_not_leak():
    """Ровно на этом правило и ошибалось, когда контекстом было окно из
    90 символов: «нельзя» из соседнего предложения про курение дотягивалось
    до животных и превращало разрешение в запрет."""
    assert pets_from_text(
        "С животными по договорённости. Курить в квартире нельзя."
    ) == "negotiable"


def test_conflicting_text_resolves_conservatively():
    """Лучше недосчитать разрешений, чем приписать их квартире, где животных
    на самом деле не ждут: завышенная доля разрешений раздула бы оценку
    надбавки."""
    assert pets_from_text(
        "Можно с кошкой. Собаки не допускаются."
    ) == "forbidden"


def test_platform_flag_wins_over_text():
    """Анкету заполняет сам автор, текст — источник второго сорта."""
    state, _, src = pets_state(True, "Без животных.")
    assert state == "allowed" and src == "platform_field"


def test_fixture_pets_state(parsed):
    assert parsed["observation"]["pets_state"] == "negotiable"
    assert parsed["observation"]["pets_raw"] is None


# ---------------------------------------------------------------------
# Деньги
# ---------------------------------------------------------------------
def test_deposit_in_months_is_extracted_from_text():
    assert deposit_months_from_text("Залог 1 месяц, комиссия 50%.") == 1.0


def test_deposit_months_reversed_wording():
    assert deposit_months_from_text("2 месяца залог") == 2.0


def test_absurd_deposit_in_months_is_rejected():
    """«Залог 36 месяцев» — почти наверняка не про депозит, а вырванный из
    контекста срок договора."""
    assert deposit_months_from_text("залог 36 месяцев") is None


def test_lease_term_is_not_a_deposit():
    """Самая частая формулировка срока аренды — «договор 11 месяцев», и
    стоящее рядом слово «залог» превращало её в залог в одиннадцать
    месячных плат. На панели таких наблюдений было 202, и у каждого сумма
    «нужно сразу» завышалась многократно."""
    assert deposit_months_from_text("Договор 11 месяцев, залог 50 000 руб.") is None
    assert deposit_months_from_text("Сдаётся на срок 6 месяцев, залог обсуждается") is None
    assert deposit_months_from_text("Залог. Договор 11 месяцев") is None


def test_deposit_in_months_still_read_when_stated_plainly():
    """Ужесточение правила не должно съесть настоящие формулировки."""
    assert deposit_months_from_text("2 месяца залог") == 2.0
    assert deposit_months_from_text("депозит 2 месяца") == 2.0
    assert deposit_months_from_text("Залог 1,5 месяца") == 1.5


def test_deposit_kept_in_both_forms(parsed):
    """Рубли и месяцы хранятся отдельно: депозит в месяцах растёт вместе
    с ценой, а в рублях нет, и приведение одного к другому потеряло бы
    формулировку условия."""
    obs = parsed["observation"]
    assert obs["deposit_rub"] == 95_000
    assert obs["deposit_months"] == 1.0


def test_prepay_first_and_last_month_is_two():
    """«Первый и последний» — два месяца на входе, а не один. Пока это не
    разбиралось, сумма «нужно сразу» занижалась ровно на месячную плату."""
    assert prepay_months_from_text("Оплата первого и последнего месяцев проживания.") == 2.0
    assert prepay_months_from_text("первый месяц + последний месяц + залог=270к") == 2.0
    assert prepay_months_from_text("Оплата за первый и последний месяц") == 2.0


def test_prepay_explicit_number():
    assert prepay_months_from_text("Залоговая сумма 107 тысяч, предоплата за 1 месяц") == 1.0
    assert prepay_months_from_text("1 мес. предоплата + депозит") == 1.0
    assert prepay_months_from_text("предоплата за 2 месяца") == 2.0


def test_prepay_number_plus_last_month_is_two():
    """«Предоплата за 1 месяц + последний месяц» — это два месяца, а не
    один: число относится к первому платежу, последний идёт сверх него."""
    assert prepay_months_from_text(
        "Предоплата за 1 месяц+последний месяц, вода и электричество по счётчикам") == 2.0


def test_deposit_for_last_month_is_not_prepay():
    """«Депозит за последний месяц» — это залог, он уже посчитан отдельно.
    Без оговорки один и тот же месяц попал бы в сумму дважды."""
    assert prepay_months_from_text("Депозит за последний месяц, можем разбить.") is None


def test_prepay_does_not_leak_across_lines():
    """Срок договора — не предоплата. На живых описаниях «Договор 11
    месяцев» из соседней строки списка превращалось в одиннадцать месяцев
    предоплаты: пять наблюдений с суммой на входе в миллион рублей."""
    assert prepay_months_from_text(
        "Договор 11 месяцев\nПредоплата за 1-й месяц, страховой депозит") is None


def test_discount_for_long_prepayment_is_not_a_condition():
    """«Скидка при оплате за 3 месяца» — предложение, а не требование при
    заселении. Широкая связка между словом и числом цепляла его."""
    assert prepay_months_from_text(
        "600 000 ₽/месяц, с гибкой системой скидок при долгосрочной "
        "предоплате — при оплате за 3 месяца — 575 000") is None


def test_prepay_silent_when_nothing_said():
    """Молчание — не ноль и не единица. Подставлять значение должен тот,
    кто считает сумму, а не тот, кто разбирает текст."""
    assert prepay_months_from_text("Собственник. Без комиссий и предоплаты.") is None
    assert prepay_months_from_text("Уютная квартира с мебелью.") is None
    assert prepay_months_from_text(None) is None


def test_money_fields(parsed):
    obs = parsed["observation"]
    assert obs["price"] == 95_000
    assert obs["client_fee_pct"] == 50
    assert obs["prepay_months"] == 1
    assert obs["utilities_included"] is False
    assert obs["utilities_price"] == 5_500
    assert obs["meters_not_included"] is True


# ---------------------------------------------------------------------
# Числа и типы
# ---------------------------------------------------------------------
def test_area_with_comma_is_parsed():
    """ЦИАН отдаёт площадь то числом, то строкой с запятой. Молча вернуть
    None означало бы потерять площадь у части объявлений и перекосить
    регрессию именно по главному регрессору."""
    assert _num("54,3") == 54.3


def test_area_with_dot_and_spaces():
    assert _num(" 31.0 ") == 31.0


def test_garbage_number_returns_none_not_raises():
    assert _num("по запросу") is None
    assert _num(None) is None


def test_boolean_is_not_mistaken_for_a_number():
    assert _num(True) is None


def test_fixture_areas(parsed):
    offer = parsed["offer"]
    assert offer["total_area"] == 54.3
    assert offer["living_area"] == 31.0
    assert offer["kitchen_area"] == 9.5


# ---------------------------------------------------------------------
# География
# ---------------------------------------------------------------------
def test_address_parts_are_read_by_type_not_position():
    """Порядок частей адреса у разных объявлений различается — в фикстуре
    улица идёт раньше округа. Разбор по позиции перепутал бы район с улицей."""
    geo = parse_geo(FIXTURE)
    assert geo["okrug"] == "СВАО"
    assert geo["district"] == "Алексеевский"
    assert geo["street"] == "Новоалексеевская улица"
    assert geo["house"] == "16к1"


def test_coordinates_are_extracted():
    """Координаты обязательны с первого дня: без них не посчитать расстояние
    до парка, а добирать их задним числом по адресу — потери."""
    geo = parse_geo(FIXTURE)
    assert geo["lat"] == pytest.approx(55.8123)
    assert geo["lon"] == pytest.approx(37.6402)


def test_missing_geo_does_not_crash():
    assert parse_geo({})["lat"] is None


def test_all_metro_stations_are_kept():
    assert len(parse_metro(FIXTURE)) == 3


def test_metro_walk_ignores_transport_time():
    """Шесть минут на транспорте до ВДНХ и восемь пешком до Алексеевской —
    разные блага. Минимум берётся только среди пеших."""
    assert metro_walk_min(parse_metro(FIXTURE)) == 8


def test_metro_walk_is_none_when_all_stations_are_by_transport():
    assert metro_walk_min([{"transport_type": "transport", "time_min": 5}]) is None


# ---------------------------------------------------------------------
# Квартира, автор, служебное
# ---------------------------------------------------------------------
def test_apartments_flag_is_separate_from_flat_type(parsed):
    """Апартаменты юридически не жильё — отдельный флаг, а не разновидность
    квартиры."""
    assert parsed["offer"]["is_apartments"] is False
    assert parsed["offer"]["flat_type"] == "rooms"


def test_building_attributes(parsed):
    offer = parsed["offer"]
    assert offer["floor"] == 7 and offer["floors_total"] == 17
    assert offer["build_year"] == 2006
    assert offer["has_lift"] is True


def test_long_term_offer_is_not_marked_daily(parsed):
    assert parsed["observation"]["is_daily"] is False


def test_daily_offer_is_flagged_not_dropped():
    """Посуточные исключаются из аналитики, но не из сбора: пусть лежат
    с пометкой."""
    payload = dict(FIXTURE)
    payload["bargainTerms"] = dict(FIXTURE["bargainTerms"],
                                   leaseTermType="daily", paymentPeriod="day")
    assert parse_offer(payload, TODAY)["observation"]["is_daily"] is True


def test_agency_offer_is_not_marked_as_homeowner(parsed):
    assert parsed["observation"]["seller_type"] == "realtor_not_commercial"
    assert parsed["observation"]["is_by_homeowner"] is False


def test_homeowner_flag_wins():
    assert seller_type({"isByHomeowner": True}, {"userType": "agency"}) == "homeowner"


def test_description_is_stored_whole_with_a_hash(parsed):
    """Текст нужен целиком: и для дедупликации, и для условий, которых нет
    в структурированных полях."""
    obs = parsed["observation"]
    assert obs["description"] == FIXTURE["description"]
    assert len(obs["description_sha1"]) == 40


def test_unknown_platform_fields_are_recorded(parsed):
    """Площадка меняет схему молча. Список нераспознанных ключей —
    единственный способ это заметить."""
    assert "newFieldFromPlatform" in parsed["observation"]["unmapped_keys"]


def test_photos_are_numbered_in_order(parsed):
    assert [p["photo_pos"] for p in parsed["photos"]] == [0, 1, 2]
    assert parsed["observation"]["photos_count"] == 3


def test_offer_without_id_is_skipped():
    """Без идентификатора объявление не встраивается в панель: нечем связать
    наблюдения между датами."""
    assert parse_offer({"description": "нет id"}, TODAY) is None


def test_snapshot_date_lands_in_observation(parsed):
    assert parsed["observation"]["snapshot_date"] == TODAY


def test_empty_payload_does_not_crash():
    assert parse_offer({"id": 1}, TODAY)["observation"]["price"] is None


def test_district_falls_back_to_settlement():
    """Новая Москва: административная единица называется поселением.

    Без этого 1566 объявлений из 16 519 остались бы без территории — и не
    случайных, а систематически более дешёвых.
    """
    from etl.parse import parse_geo
    geo = {
        "districts": [
            {"id": 332, "name": "Московский", "type": "poselenie"},
            {"id": 325, "name": "НАО (Новомосковский)", "type": "okrug"},
        ],
        "address": [{"type": "okrug", "name": "НАО (Новомосковский)"}],
        "coordinates": {"lat": 55.6, "lng": 37.35},
    }
    assert parse_geo({"geo": geo})["district"] == "Московский"


def test_district_prefers_raion_over_mikroraion():
    """Микрорайон вложен в район: подменять им район нельзя."""
    from etl.parse import parse_geo
    geo = {
        "districts": [
            {"id": 900, "name": "Марьинский парк", "type": "mikroraion"},
            {"id": 77, "name": "Марьино", "type": "raion"},
        ],
        "coordinates": {"lat": 55.65, "lng": 37.75},
    }
    assert parse_geo({"geo": geo})["district"] == "Марьино"
