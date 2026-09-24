"""Извлечение объявлений из HTML страницы выдачи.

Почему через страницу, а не через API. У ЦИАНа есть внутренний эндпоинт
api.cian.ru, которым пользуется его собственный фронтенд. Он удобнее —
отдаёт готовый JSON, — но это служебный интерфейс SPA, и защита относится
к нему строже: первый же запрос к нему вернул заглушку, тогда как обычная
страница до этого открывалась нормально.

Публичная страница cat.php — тот самый адрес, который открывает человек.
И данные в ней ровно те же: страница серверного рендеринга несёт с собой
целиком состояние приложения, включая массив объявлений по 137 полей.
Лежит оно в

    window._cianConfig['frontend-serp'] = (... || []).concat([ ... ])

среди записей вида {"key": ..., "value": ...}, в записи с ключом
initialState, по пути results.offers. Там же results.totalOffers —
заявленное площадкой число результатов, по которому сборщик решает,
делить корзину или листать.

Разбор идёт сканером со счётчиком скобок, а не регулярным выражением:
в JSON встречаются и скобки внутри строк (в описаниях квартир), и
экранированные кавычки, и регулярное выражение на таком спотыкается.
"""
from __future__ import annotations

import json
import re

# Начало присваивания. Имя микрофронтенда может смениться, поэтому оно
# не зашито намертво: ищем любой ключ, оканчивающийся на 'serp'.
_ASSIGNMENT = re.compile(
    r"_cianConfig\[['\"]([\w-]*serp)['\"]\]\s*=\s*\([^)]*\)\s*\.concat\(",
)


class SerpNotFound(RuntimeError):
    """В странице нет состояния приложения.

    Отдельный тип, потому что причина обычно не в разборе, а в том, что
    вместо выдачи пришло что-то другое: заглушка, капча или страница
    с нулём результатов.
    """


def _balanced(text: str, start: int) -> str | None:
    """Вырезать сбалансированный [...] или {...}, начиная с позиции start.

    Уважает строковые литералы и экранирование: скобка внутри описания
    квартиры не должна сбивать счётчик.
    """
    depth = 0
    in_string = False
    escaped = False
    i = start

    while i < len(text):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        i += 1
    return None


def extract_state(html: str) -> dict:
    """HTML страницы -> состояние приложения (initialState)."""
    match = _ASSIGNMENT.search(html)
    if not match:
        raise SerpNotFound("В странице нет _cianConfig с состоянием выдачи")

    bracket = html.find("[", match.end() - 1)
    if bracket == -1:
        raise SerpNotFound("Не найдено начало массива настроек")

    chunk = _balanced(html, bracket)
    if chunk is None:
        raise SerpNotFound("Массив настроек не закрывается — страница обрезана")

    try:
        entries = json.loads(chunk)
    except json.JSONDecodeError as err:
        raise SerpNotFound(f"Настройки не разбираются как JSON: {err}") from err

    for entry in entries:
        if isinstance(entry, dict) and entry.get("key") == "initialState":
            return entry.get("value") or {}
    raise SerpNotFound("Среди настроек нет initialState")


def extract_offers(html: str) -> tuple[list[dict], int]:
    """HTML страницы -> (объявления, заявленное число результатов).

    Пустой список с ненулевым счётчиком — нормальная ситуация для страницы
    за пределом глубины выдачи. Пустой список с нулём — корзина пуста.
    """
    results = (extract_state(html) or {}).get("results") or {}
    offers = results.get("offers") or []
    total = results.get("totalOffers")
    if total is None:
        total = results.get("aggregatedOffersCount") or 0
    return offers, int(total)


def looks_like_offer_page(html: str) -> bool:
    """Быстрая проверка без полного разбора — для диагностики."""
    return bool(_ASSIGNMENT.search(html))
