"""Одна проба: отдаёт ли площадка выдачу прямо сейчас.

Нужна сторожу (`scripts/box_watch.sh`). Площадка после аварии 11.10.2026
отвечает рывками: из десяти запросов данные даются один-два раза, а
остальные возвращают код 553 и страницу в 178 байт. Обход в такие часы
тратит попытки впустую, поэтому его запускают не по часам, а по факту —
когда проба прошла.

Проверяется не код ответа, а наличие состояния выдачи `_cianConfig`:
заглушка приходит с кодом 200 ничуть не реже.

Код возврата: 0 — выдача далась, 1 — нет. Печатает одну строку для журнала.
"""
from __future__ import annotations

import random
import re
import sys
import time

from playwright.sync_api import sync_playwright

from etl import config
from etl.partition import Bucket, build_url_params
from etl.serp_html import SerpNotFound, extract_offers

PROFILE = str(config.DATA / "browser_profile")


def main() -> int:
    # Корзина самая дешёвая из возможных: одна комната по всей Москве.
    # Нам нужен факт «страница отдалась», а не её содержимое.
    params = build_url_params(Bucket(rooms="1"), page=1)
    url = config.SERP_URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            PROFILE,
            channel="chrome",
            headless=False,
            locale="ru-RU",
            viewport={"width": 1440, "height": 900},
            args=["--no-sandbox"] if config.CHROME_NO_SANDBOX else [],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.route("**/*", lambda route: (
            route.abort()
            if route.request.resource_type in ("image", "font", "media")
            else route.continue_()))

        try:
            # Как человек: сначала главная, потом выдача. Без этого
            # сессионных кук нет и выдача не отдаётся даже в хорошие часы.
            page.goto("https://www.cian.ru/", wait_until="domcontentloaded",
                      timeout=60_000)
            time.sleep(random.uniform(3, 6))
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            html = page.content()
        except Exception as err:                      # noqa: BLE001
            print(f"проба: не дошли до страницы — {type(err).__name__}")
            context.close()
            return 1

        status = resp.status if resp else 0
        try:
            offers, claimed = extract_offers(html)
        except SerpNotFound:
            print(f"проба: выдача НЕ далась (код {status}, длина {len(html)})")
            context.close()
            return 1

        print(f"проба: выдача далась (код {status}, заявлено {claimed}, "
              f"объявлений на странице {len(offers)})")
        context.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
