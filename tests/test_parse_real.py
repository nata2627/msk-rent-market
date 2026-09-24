"""Разбор на НАСТОЯЩИХ карточках ЦИАНа.

Фикстура `cian_serp_offers.json` — три объявления ДЛИТЕЛЬНОЙ аренды,
вынутые из собственного сырого слоя и пропущенные через etl/privacy.py:
без телефонов, имён и с псевдонимизированными идентификаторами автора.
Фикстура уходит в публичный репозиторий, поэтому чистится тем же кодом,
что и база, а не вручную.

Эти тесты отвечают на вопрос, на который синтетическая фикстура ответить
не может: совпадают ли имена полей с тем, что площадка отдаёт на самом
деле. Несколько допущений здесь уже не подтвердились и были исправлены —
поэтому тесты и написаны.
"""
import json
from datetime import date
from pathlib import Path

import pytest

from etl.parse import parse_offer

OFFERS = json.loads(
    (Path(__file__).parent / "fixtures" / "cian_serp_offers.json").read_text(encoding="utf-8")
)
TODAY = date(2026, 9, 22)


@pytest.fixture
def parsed():
    return [parse_offer(o, TODAY) for o in OFFERS]


def test_fixture_is_real_platform_output():
    """Карточка выдачи содержит около 137 полей. Если их вдруг стало сильно
    меньше, фикстуру подменили обрезанной, и проверки теряют смысл."""
    assert len(OFFERS) == 3
    assert all(len(o) > 120 for o in OFFERS)


def test_every_offer_parses(parsed):
    assert all(p is not None for p in parsed)


def test_unmapped_fields_are_recorded_not_swallowed():
    """Площадка добавляет поля молча. Нераспознанное не выбрасывается,
    а складывается в unmapped_keys — это единственный способ заметить,
    что схема изменилась.

    Механизм уже окупился: через него нашлись goodPrice (собственная
    оценка цены площадкой), homeownerProofs и identicalOffers — чужая
    дедупликация, годная как эталон для нашей."""
    import copy

    payload = copy.deepcopy(OFFERS[0])
    payload["совершенноНовоеПоле"] = {"что-то": 1}
    keys = parse_offer(payload, TODAY)["observation"]["unmapped_keys"]
    assert keys == ["совершенноНовоеПоле"]


def test_known_fields_are_not_reported_as_unmapped(parsed):
    """После разбора goodPrice и соседей журнал должен опустеть —
    иначе он зашумлён и перестаёт работать как сигнал."""
    for p in parsed:
        assert p["observation"]["unmapped_keys"] is None


def test_platform_price_estimate_is_parsed(parsed):
    """Оценка приходит строкой «52 000—63 000 ₽/мес». Как строка она ни
    с чем не сравнивается, а сравнить есть с чем: это оценка чужой модели
    той же величины, которую моделируем мы."""
    with_estimate = [p for p in parsed if p["observation"]["price_estimate_low"]]
    assert with_estimate, "ни у одного объявления в фикстуре нет оценки площадки"
    obs = with_estimate[0]["observation"]
    assert obs["price_estimate_low"] < obs["price_estimate_high"]
    assert obs["price_label"]


def test_area_really_arrives_as_a_string():
    """Главная причина, по которой площадь достаётся через _num, а не
    приводится к float напрямую."""
    assert all(isinstance(o["totalArea"], str) for o in OFFERS)


def test_areas_are_parsed(parsed):
    assert [p["offer"]["total_area"] for p in parsed] == [136.0, 38.0, 70.0]


def test_prices_are_parsed(parsed):
    assert [p["observation"]["price"] for p in parsed] == [310000.0, 45000.0, 120000.0]


def test_coordinates_present_for_every_offer(parsed):
    """Координаты обязательны с первого дня — без них не посчитать
    расстояние до парка."""
    for p in parsed:
        assert p["offer"]["lat"] and p["offer"]["lon"]
        assert 55.0 < p["offer"]["lat"] < 56.5
        assert 36.5 < p["offer"]["lon"] < 38.5


def test_district_and_okrug_are_extracted(parsed):
    assert [p["offer"]["district"] for p in parsed] == [
        "Тверской", "Лосиноостровский", "Пресненский"]
    assert [p["offer"]["okrug"] for p in parsed] == ["ЦАО", "СВАО", "ЦАО"]


def test_district_comes_from_geo_districts_not_address_parts():
    """geo.districts заполнен у всех объявлений, а часть адреса с типом
    raion — не у всех. Район это ось всей аналитики, терять его нельзя."""
    assert all(o["geo"].get("districts") for o in OFFERS)


def test_address_part_for_district_is_called_raion():
    types = {a.get("type") for o in OFFERS for a in o["geo"]["address"]}
    assert "raion" in types and "district" not in types


def test_metro_walk_time_ignores_transport(parsed):
    """У второго объявления все станции только на транспорте — значит
    пешего времени нет, и поле остаётся пустым, а не заполняется
    временем на автобусе."""
    assert [p["offer"]["metro_walk_min"] for p in parsed] == [5, None, 3]


def test_long_term_offers_are_not_flagged_daily(parsed):
    """Все три — длительная аренда, то есть то, что изучает проект."""
    assert not any(p["observation"]["is_daily"] for p in parsed)
    assert all(o["category"] == "flatRent" for o in OFFERS)


def test_daily_rent_would_be_flagged_not_dropped():
    """Посуточные исключаются из аналитики, но не из сбора: пусть лежат
    с пометкой."""
    import copy
    payload = copy.deepcopy(OFFERS[0])
    payload["bargainTerms"]["leaseTermType"] = "daily"
    payload["bargainTerms"]["paymentPeriod"] = "day"
    assert parse_offer(payload, TODAY)["observation"]["is_daily"] is True


def test_creation_date_and_bump_timestamp_are_different_things(parsed):
    """Первая публикация разбросана по 2022-2025 годам, а addedTimestamp
    у всех приходится на одни и те же дни — это подъём объявления в выдаче.
    Перепутать их значит занизить срок жизни объявления в разы."""
    for p in parsed:
        published = p["offer"]["published_at"]
        bumped = p["observation"]["edited_at"]
        assert published.year == 2016
        assert bumped.year == 2026
        assert published.replace(tzinfo=None) < bumped.replace(tzinfo=None)


def test_deposit_and_utilities_are_read(parsed):
    obs = parsed[0]["observation"]
    assert obs["deposit_rub"] is not None and obs["deposit_rub"] > 0
    assert obs["utilities_included"] in (True, False)


def test_seller_type_survives_but_identity_does_not(parsed):
    """Тип продавца — объясняющая переменная регрессии, он нужен.
    Имя не сохраняется: у частных риелторов там их собственное имя,
    и отличить его от названия агентства нечем. Идентификатор хранится
    псевдонимом — по нему считается число объявлений у автора, но список
    людей из базы не собрать."""
    obs = parsed[0]["observation"]
    assert obs["seller_type"] == "realtor_based"
    assert obs["seller_name"] is None
    assert obs["seller_id"] is not None and len(obs["seller_id"]) == 16


def test_platform_minhash_is_captured(parsed):
    """Площадка сама считает минхеш описания — готовая подпись текста для
    схлопывания дублей, свои шинглы писать не придётся."""
    mh = parsed[0]["observation"]["description_minhash"]
    assert isinstance(mh, list) and len(mh) > 10


def test_pets_field_is_absent_from_search_results():
    """Причина, по которой в сборщике появился второй проход с фильтром
    площадки: поля petsAllowed в выдаче нет вовсе, и без этого прохода про
    животных можно судить только по тексту описания."""
    blob = json.dumps(OFFERS, ensure_ascii=False)
    assert "petsAllowed" not in blob
    assert all(p["observation"]["pets_source"] in ("text", "none") for p in [
        parse_offer(o, TODAY) for o in OFFERS])


def test_pets_platform_label_is_empty_until_the_filter_pass(parsed):
    """Жёсткая метка проставляется сборщиком после прохода с фильтром.
    None значит «проход не делался», а не «не разрешено»."""
    assert all(p["observation"]["pets_platform_allowed"] is None for p in parsed)


def test_photos_are_collected(parsed):
    assert [p["observation"]["photos_count"] for p in parsed] == [42, 7, 20]
    assert parsed[0]["photos"][0]["url"].startswith("https://")


def test_no_personal_data_in_the_committed_fixture():
    """Фикстура уходит в публичный репозиторий. Телефонов, имён и
    сырых идентификаторов автора в ней быть не должно ни в каком виде."""
    import re

    blob = json.dumps(OFFERS, ensure_ascii=False)
    assert '"phones"' not in blob and '"phoneNumbers"' not in blob
    assert not re.search(r'"(?:agencyName|companyName)"', blob)
    assert not re.search(r'"(?:userId|cianUserId|publishedUserId)": *\d{5,}', blob)
