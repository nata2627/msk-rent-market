"""Один запрос к площадке: отдаёт ли она данные прямо сейчас.

    python -m etl.check_access

Нужно, чтобы проверять доступ, не запуская полуторачасовой обход и не
устраивая серию запросов. Ровно один запрос — и внятный вывод о том,
что происходит.
"""
from __future__ import annotations

import sys

from etl.cian_client import Blocked, CianClient
from etl.partition import Bucket


def main() -> int:
    use_browser = "--browser" in sys.argv
    if use_browser:
        from etl.browser_client import BrowserClient
        client = BrowserClient()
    else:
        client = CianClient()
    names = {"browser": "настоящий Chrome", "html": "публичная страница выдачи",
             "api": "внутренний эндпоинт"}
    print(f"Транспорт: {client.transport} ({names.get(client.transport, '?')})")
    try:
        offers, claimed = client.search(Bucket(rooms="2"))
    except Blocked as err:
        print(f"ЗАКРЫТО. {err}")
        print()
        print("Площадка отдаёт страницу-заглушку вместо данных. Это не чинится")
        print("ускорением, сменой User-Agent или прокси, и чинить мы это не будем.")
        print("Что делать:")
        print("  1. Открыть www.cian.ru в обычном браузере и посмотреть, работает ли")
        print("     сайт руками. Если работает — блокировка адресная к клиенту,")
        print("     а не к адресу, и обычно снимается сама за несколько часов.")
        print("  2. Подождать и повторить эту команду. Не чаще раза в час.")
        return 2
    except Exception as err:                      # noqa: BLE001
        print(f"ОШИБКА СЕТИ: {type(err).__name__}: {err}")
        return 3

    if hasattr(client, "close"):
        client.close()
    print(f"ОТКРЫТО. Заявлено результатов: {claimed}, на странице: {len(offers)}.")
    from etl.partition import KNOWN_ROOM_COUNTS
    expected = KNOWN_ROOM_COUNTS.get("2")
    if expected:
        print(f"Для двухкомнатных ожидалось около {expected} "
              f"(снято вручную 2026-09-22). "
              f"{'Сходится.' if 0.75 * expected <= claimed <= 1.25 * expected else 'НЕ СХОДИТСЯ — проверить параметры адреса.'}")
    if offers:
        keys = sorted(offers[0].keys())
        print(f"Полей в карточке: {len(keys)}")
        print("Первые двадцать:", ", ".join(keys[:20]))
        print()
        print("Дальше: сохранить этот ответ в tests/fixtures/offer_cian.json")
        print("вместо синтетической карточки и прогнать make test — до этого")
        print("совпадение имён полей остаётся непроверенным допущением.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
