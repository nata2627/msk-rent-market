"""Тесты извлечения объявлений из HTML страницы выдачи.

Публичная страница — основной транспорт сбора: данные в ней те же, что в
служебном API, а защита относится к ней мягче. Разбор проверяется на
фикстуре с настоящей обёрткой _cianConfig и настоящими карточками.

Отдельно эта же функция прогонялась на полном архивном снимке страницы
(3.7 МБ, 28 объявлений, заявлено 1577) — в репозиторий он не кладётся
из-за размера.
"""
from pathlib import Path

import pytest

from etl.serp_html import (
    SerpNotFound,
    extract_offers,
    extract_state,
    looks_like_offer_page,
)

PAGE = (Path(__file__).parent / "fixtures" / "cian_serp_page.html").read_text(encoding="utf-8")


def test_offers_are_extracted_from_the_page():
    offers, total = extract_offers(PAGE)
    assert len(offers) == 3
    assert total == 1577


def test_extracted_offers_are_full_cards():
    """Страница несёт тот же объект, что и API: 137 полей, а не урезанную
    карточку для показа. Поэтому etl/parse.py общий для обоих транспортов."""
    offers, _ = extract_offers(PAGE)
    assert len(offers[0]) > 120
    assert "bargainTerms" in offers[0] and "geo" in offers[0]


def test_page_is_recognised():
    assert looks_like_offer_page(PAGE)


def test_brace_scanner_survives_braces_inside_text():
    """Описания квартир содержат и скобки, и кавычки. Регулярное выражение
    на таком спотыкается — отсюда сканер со счётчиком скобок."""
    tricky = PAGE.replace(
        "window._cianConfig = window._cianConfig || {};",
        'window._cianConfig = window._cianConfig || {}; /* [{"}": "]"} */',
    )
    offers, _ = extract_offers(tricky)
    assert len(offers) == 3


def test_missing_state_raises_instead_of_returning_empty():
    """Пустой список молча означал бы «в районе нет объявлений», и район
    выпал бы из выборки незаметно. Поэтому отсутствие состояния — ошибка."""
    with pytest.raises(SerpNotFound):
        extract_offers("<html><body>Ничего интересного</body></html>")


def test_truncated_page_raises():
    with pytest.raises(SerpNotFound):
        extract_offers(PAGE[: len(PAGE) // 2])


def test_microfrontend_name_is_not_hardcoded():
    """Имя микрофронтенда у площадки уже менялось. Промах здесь стоил бы
    всего сбора, поэтому ищется любой ключ, оканчивающийся на serp."""
    renamed = PAGE.replace("'frontend-serp'", "'desktop-serp'")
    offers, _ = extract_offers(renamed)
    assert len(offers) == 3


def test_state_contains_results():
    assert "results" in extract_state(PAGE)
