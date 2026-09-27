"""Тесты клиента, которые можно прогнать без обращения к площадке.

Проверяется поведение, ради которого клиент вообще написан отдельным модулем:
распознавание заглушки вместо данных и разбор дерева районов.
"""
import pytest

from etl.cian_client import Blocked, CianClient, PageWithoutData, flatten_districts

BLOCK_PAGE = (
    '<!doctype html>\n<html lang="ru">\n<head><title>Captcha - база объявлений ЦИАН</title>'
    '</head><body>Код страницы: cian_waf_block</body></html>'
)


def test_block_page_is_detected_despite_http_200():
    """Заглушка приходит с кодом 200. Если смотреть только на статус, HTML
    молча примут за пустую выдачу и решат, что в районе нет объявлений —
    район выпадет из выборки, и никто этого не заметит."""
    assert CianClient._looks_blocked(BLOCK_PAGE)


def test_normal_json_is_not_mistaken_for_a_block():
    assert not CianClient._looks_blocked('{"data": {"offersSerialized": []}}')


def test_blocked_is_a_separate_exception_type():
    """Отдельный тип нужен, потому что реакция принципиально иная: сетевую
    ошибку повторяют, а блокировку — нет, обход останавливается."""
    assert issubclass(Blocked, RuntimeError)
    assert not issubclass(Blocked, ValueError)


def test_broken_page_is_not_a_block():
    """Разные причины — разная реакция. Блокировка означает «прекратить
    обход», сбойная страница — «повторить, а если не вышло, пометить
    корзину и идти дальше».

    Смешение этих случаев уже стоило 34% одного прогона: одна страница без
    данных остановила обход на 34-й корзине из 40, после пятидесяти минут
    работы."""
    assert not issubclass(PageWithoutData, Blocked)
    assert not issubclass(Blocked, PageWithoutData)
    assert issubclass(PageWithoutData, RuntimeError)


def test_empty_page_is_never_treated_as_an_empty_bucket():
    """Страница без данных не должна становиться «в районе нет квартир»:
    район молча выпал бы из выборки, и заметить это было бы нечем.
    Поэтому для неё заведён отдельный тип ошибки, а не пустой список."""
    import inspect

    from etl import browser_client

    src = inspect.getsource(browser_client.BrowserClient.search)
    assert "PageWithoutData" in src
    assert "return [], 0" not in src


def test_navigation_failure_is_a_bucket_failure_not_a_crash():
    """Страница, которая не открылась, не должна ронять весь обход.

    27.09.2026 одна страница фильтра «с животными» не открылась за
    полторы минуты, исключение Playwright прошло мимо всех обработчиков,
    и дневной сбор остановился на третьей корзине из почти трёхсот:
    111 объявлений вместо шестнадцати тысяч."""
    import inspect

    from etl import browser_client, collect

    src = inspect.getsource(browser_client.BrowserClient.search)
    assert "PlaywrightError" in src, "ошибка загрузки страницы не ловится"
    assert "continue" in src, "после сбоя загрузки нет повтора"

    # Проход по животным тоже обязан пережить сбойную корзину, иначе
    # ошибка снова улетит в обработчик «непредвиденной» и остановит день.
    run_src = inspect.getsource(collect.run)
    assert "except PageWithoutData" in run_src
    assert "pets_complete = False" in run_src


# ---------------------------------------------------------------------
# Справочник районов
# ---------------------------------------------------------------------
TREE = {
    "data": [
        {"id": 4, "name": "СВАО", "childs": [
            {"id": 21, "name": "Алексеевский"},
            {"id": 22, "name": "Свиблово"},
        ]},
        {"id": 5, "name": "ЗАО", "children": [
            {"id": 33, "name": "Филёвский парк"},
        ]},
    ]
}


def test_districts_are_flattened_with_their_okrug():
    districts = flatten_districts(TREE)
    assert {d["name"] for d in districts} == {"Алексеевский", "Свиблово", "Филёвский парк"}
    assert next(d for d in districts if d["name"] == "Свиблово")["okrug"] == "СВАО"


def test_okrug_itself_is_not_a_bucket():
    """Иначе объявления посчитались бы дважды — в округе и в его районе."""
    assert 4 not in {d["id"] for d in flatten_districts(TREE)}


def test_both_childs_and_children_spellings_are_handled():
    """Название ключа у площадки менялось. Промах здесь стоит целого округа,
    выпавшего из выборки."""
    assert any(d["name"] == "Филёвский парк" for d in flatten_districts(TREE))


def test_duplicate_districts_are_collapsed():
    doubled = {"data": TREE["data"] + TREE["data"]}
    assert len(flatten_districts(doubled)) == 3


def test_empty_tree_returns_empty_list_not_crash():
    """Сбор должен пережить недоступный справочник и уйти на нарезку по
    комнатности и цене, а не упасть."""
    assert flatten_districts({}) == []
