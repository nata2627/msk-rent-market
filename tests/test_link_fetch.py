"""Страница объявления по ссылке: разбор, приведение к выдаче, модели dbt.

Образец страницы собран руками по устройству настоящей (17.11.2026) и
без чужих данных: имена и телефоны автора здесь выдуманные, и проверяется
как раз то, что до записи они не доходят.
"""
import json
from datetime import date

import pytest

from etl import link_fetch as lf
from etl.parse import parse_offer
from etl.privacy import scrub_payload
from etl.serp_html import SerpNotFound, extract_offer_card

OFFER = {
    "cianId": 312345678, "id": 312345678, "dealType": "rent", "offerType": "flat",
    "status": "published",
    "category": "flatRent", "roomsCount": 1, "totalArea": "35.0", "floorNumber": 3,
    "creationDate": "2026-11-09T23:16:58.303", "description": "Дизайнерский ремонт, звоните 89991234567",
    "bargainTerms": {"price": 90000, "currency": "rur", "paymentPeriod": "monthly",
                     "leaseTermType": "longTerm", "deposit": 90000, "prepayMonths": 1},
    "building": {"materialType": "brick", "floorsCount": 5, "buildYear": 1959},
    "geo": {"coordinates": {"lat": 55.73, "lng": 37.58},
            "address": [{"type": "location", "name": "Москва"}],
            "undergrounds": [{"id": 143, "name": "Фрунзенская", "travelType": "walk",
                              "travelTime": 8, "lineColor": "CF0000"},
                             {"id": 999, "name": "Лужники", "travelType": "walk",
                              "travelTime": 12}]},
    "petsAllowed": True, "repairType": "design", "phones": [{"number": "9991234567"}],
    "recoveryLink": "https://служебная", "trackingData": {"x": 1}, "userId": 1216029,
}
DATA = {"offer": OFFER,
        "agent": {"accountType": "specialist", "userType": "realtor_based", "cianUserId": 9235,
                  "experience": "с 2004 года", "isPassportVerified": True,
                  "firstName": "Иван", "lastName": "Выдуманный", "phones": ["79990000000"]}}
LINES = {"143": (10, None), "999": (None, "mck")}


def page(*entries_lists):
    parts = ["<html><script>"]
    for entries in entries_lists:
        parts.append("window._cianConfig['frontend-offer-card'] = "
                     "(window._cianConfig['frontend-offer-card'] || []).concat("
                     + json.dumps(entries, ensure_ascii=False) + ");")
    return "".join(parts) + "</script></html>"


def test_offer_card_state_is_found_in_second_assignment():
    html = page([{"key": "projectName", "value": "x"}],
                [{"key": "defaultState", "value": {"offerData": DATA}}])
    assert extract_offer_card(html)["offer"]["cianId"] == 312345678
    with pytest.raises(SerpNotFound):
        extract_offer_card(page([{"key": "defaultState", "value": {"offerData": {}}}]))
    with pytest.raises(SerpNotFound):
        extract_offer_card("<html>заглушка</html>")


def test_page_becomes_serp_offer():
    p = lf.card_payload(DATA, LINES)
    assert p["fullUrl"] == "https://www.cian.ru/rent/flat/312345678/"
    assert "recoveryLink" not in p and "trackingData" not in p
    assert p["geo"]["undergrounds"][0] == {
        "id": 143, "name": "Фрунзенская", "time": 8, "transportType": "walk", "lineId": 10,
        "lineType": None, "lineColor": "CF0000", "underConstruction": None}
    assert p["user"]["experience"] == "2004"
    assert "firstName" not in json.dumps(p, ensure_ascii=False)


def test_parsed_like_a_serp_offer():
    parsed = parse_offer(lf.card_payload(DATA, LINES), date(2026, 11, 17))
    offer, obs = parsed["offer"], parsed["observation"]
    # МЦК в 12 минутах не метро: ветку и тип линии страница не знает, они
    # из выдачи, и ближайшим метро остается Фрунзенская.
    assert offer["metro_real_walk_min"] == 8
    assert offer["url"] == "https://www.cian.ru/rent/flat/312345678/"
    assert (obs["pets_state"], obs["repair_type"]) == ("allowed", "design")
    assert (obs["seller_type"], obs["seller_experience_since"]) == ("realtor_based", 2004)
    assert "89991234567" not in obs["description"]


def test_what_is_stored_has_no_personal_data():
    stored = json.dumps(scrub_payload(lf.card_payload(DATA, LINES)), ensure_ascii=False)
    for secret in ("9991234567", "Выдуманный", "9235", "1216029"):
        assert secret not in stored


def test_dbt_models_render_without_dbt():
    sql = lf.render("offer_scored", {"offer_condition": "(select 1)"})
    assert "{{" not in sql and "{%" not in sql
    assert "from marts.offer_segment s" in sql
    assert "left join (select 1) r" in sql
    cond = lf.render("offer_condition")
    assert "from core.offer o" in cond and "from raw.offer_snapshot s" in cond


def test_render_refuses_unknown_template(tmp_path, monkeypatch):
    (tmp_path / "x.sql").write_text("select {{ var('y') }}")
    monkeypatch.setattr(lf, "DBT_MODELS", tmp_path)
    with pytest.raises(RuntimeError):
        lf.render("x")


class Conn:
    """Соединение-заглушка: commit под _Held ничего не делает."""

    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1

    def cursor(self):
        return "курсор"


def test_held_connection_never_commits():
    c = Conn()
    held = lf._Held(c)
    held.commit()
    assert c.commits == 0 and held.cursor() == "курсор"


class Reached(Exception):
    pass


def outcome(monkeypatch, status, html):
    monkeypatch.setattr(lf, "fetch_page", lambda oid: (status, html))

    def connect(**kw):
        raise Reached
    monkeypatch.setattr(lf.psycopg, "connect", connect)
    try:
        return lf.run("312345678", date(2026, 11, 17))["status"]
    except Reached:
        return "до базы"


def card_page(**changes):
    return page([{"key": "defaultState",
                  "value": {"captchaSiteKey": "x", "offerData": {**DATA,
                                                                 "offer": {**OFFER, **changes}}}}])


def test_run_outcomes_before_the_database(monkeypatch):
    # Слово captcha в настройках настоящей страницы не блокировка.
    assert outcome(monkeypatch, 200, card_page()) == "до базы"
    assert outcome(monkeypatch, 200, "<html><title>captcha</title></html>") == "blocked"
    assert outcome(monkeypatch, 403, "") == "blocked"
    assert outcome(monkeypatch, 404, "") == "not_found"
    assert outcome(monkeypatch, 200, "<html>нет такого</html>") == "not_found"
    assert outcome(monkeypatch, 200, card_page(status="deactivated")) == "not_found"
    assert outcome(monkeypatch, 200, card_page(dealType="sale")) == "not_rent"
