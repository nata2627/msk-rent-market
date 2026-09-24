"""Тесты удаления персональных данных.

Эти тесты охраняют не корректность расчёта, а то, что чужие телефоны и
имена не попадут в публичный репозиторий. Поэтому они сформулированы как
запреты: «в результате НЕ должно быть», а не «должно быть».

Первая версия вычистки хешировала один идентификатор автора из четырёх,
и выглядела рабочей: телефоны исчезли, имена исчезли. Автор при этом
по-прежнему опознавался по трём оставшимся полям. Отсюда тест, который
проверяет ВСЕ идентификаторы разом, а не по одному.
"""
import json

import pytest

from etl.privacy import (
    SELLER_ID_FIELDS,
    hash_seller_id,
    scrub_payload,
    scrub_text,
)

CARD = {
    "id": 312845671,
    "cianId": 312845671,
    "userId": 10000001,
    "cianUserId": 10000001,
    "publishedUserId": 10000001,
    "phones": [{"countryCode": "7", "number": "0000000000"}],
    "description": "Сдаётся квартира. Звонить 8 900 000-00-00 после 10.",
    "user": {
        "userId": 10000001,
        "cianUserId": 10000001,
        "agencyName": "Тестовое Имя Автора",
        "companyName": "Тестовое Имя Автора",
        "phoneNumbers": [{"number": "0000000000"}],
        "profileUri": "/agents/12345/",
        "userType": "realtor_not_commercial",
        "isAgent": True,
        "personalRating": 4.8,
        "experience": "2019",
    },
}


@pytest.fixture
def clean():
    return scrub_payload(CARD)


# ---------------------------------------------------------------------
def test_no_phone_survives_anywhere(clean):
    """Самая важная проверка: телефона не должно остаться ни в одном
    поле, на любой глубине. Поэтому ищем по всему сериализованному
    объекту, а не по известным местам."""
    assert "0000000000" not in json.dumps(clean, ensure_ascii=False)
    assert "phones" not in clean


def test_no_personal_name_survives(clean):
    """У частных риелторов в поле агентства стоит их собственное имя,
    и отличить его от названия компании нечем."""
    assert "Тестовое Имя Автора" not in json.dumps(clean, ensure_ascii=False)


def test_phone_inside_free_text_is_masked(clean):
    assert "900 000" not in clean["description"]
    assert "[телефон удалён]" in clean["description"]


def test_all_four_seller_identifiers_are_hashed(clean):
    """Идентификаторов автора четыре, и лежат они в разных местах.
    Захешировать один — значит не сделать ничего: по остальным трём
    автор по-прежнему опознаётся."""
    for field in SELLER_ID_FIELDS:
        assert clean[field] != CARD[field], f"верхний уровень: {field}"
        assert len(clean[field]) == 16
    assert clean["user"]["userId"] != CARD["user"]["userId"]
    assert "10000001" not in json.dumps(clean)


def test_offer_id_is_not_hashed(clean):
    """Идентификатор объявления публичный, по нему открывается страница.
    Хешировать его значило бы потерять связь с площадкой."""
    assert clean["id"] == CARD["id"]
    assert clean["cianId"] == CARD["cianId"]


def test_non_personal_profile_fields_survive(clean):
    """Тип продавца — объясняющая переменная регрессии, рейтинг и стаж
    публичны и никого не идентифицируют. Их вычищать незачем."""
    assert clean["user"]["userType"] == "realtor_not_commercial"
    assert clean["user"]["isAgent"] is True
    assert clean["user"]["personalRating"] == 4.8
    assert clean["user"]["experience"] == "2019"


def test_original_card_is_not_modified(clean):
    """Из исходной карточки разбираются поля для панели, и порядок
    вызовов не должен ни на что влиять."""
    assert CARD["phones"][0]["number"] == "0000000000"
    assert CARD["user"]["agencyName"] == "Тестовое Имя Автора"


# ---------------------------------------------------------------------
def test_same_seller_gets_same_pseudonym():
    """Иначе нельзя посчитать, сколько объявлений у одного автора."""
    assert hash_seller_id(12345) == hash_seller_id(12345)
    assert hash_seller_id(12345) == hash_seller_id("12345")


def test_different_sellers_get_different_pseudonyms():
    assert hash_seller_id(12345) != hash_seller_id(12346)


def test_pseudonym_does_not_contain_the_original():
    assert "12345" not in hash_seller_id(12345)


def test_missing_seller_stays_missing():
    assert hash_seller_id(None) is None
    assert hash_seller_id("") is None


def test_text_without_phone_is_untouched():
    text = "Светлая квартира, 10 минут до метро, 2024 год постройки."
    assert scrub_text(text) == text


def test_empty_text_does_not_crash():
    assert scrub_text(None) is None
    assert scrub_text("") == ""


def test_card_without_user_block_does_not_crash():
    assert scrub_payload({"id": 1})["id"] == 1
