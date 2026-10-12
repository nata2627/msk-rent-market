"""Транспорт через настоящий браузер.

    python -m etl.collect --mode narrow --transport browser

Зачем он понадобился. ЦИАН отвечает заглушкой на запросы из Python, но
нормально отдаёт страницы браузеру с того же адреса. Проверено по шагам:

* `requests` с браузерными заголовками — заглушка;
* установка сессии (сначала главная, потом выдача) — заглушка;
* Playwright поверх настоящего Chrome в headless-режиме — HTTP 403;
* **Playwright поверх настоящего Chrome в обычном режиме — работает.**

Здесь ничего не подделывается. Это тот самый Chrome, установленный в
системе, с обычным профилем, который сам ходит на главную страницу и
получает куки — ровно как если бы страницу открыл человек. Отличается
только то, что на «дальше» нажимает скрипт, а не рука.

Чего здесь намеренно НЕТ и не будет: подмены отпечатка TLS, патчей,
скрывающих автоматизацию, ротации прокси и решения капчи. Разница
принципиальная: браузер не выдаёт себя за другого.

Темп тот же, что у обычного клиента: один поток, пауза 3–6 секунд
с джиттером. Браузер открывается один раз на весь обход.
"""
from __future__ import annotations

import logging
import random
import time
import urllib.parse

from playwright.sync_api import Error as PlaywrightError

from etl import config
from etl.cian_client import Blocked, PageWithoutData
from etl.partition import Bucket, build_url_params
from etl.serp_html import SerpNotFound, extract_offers

log = logging.getLogger(__name__)

# Профиль браузера живёт рядом с данными и в репозиторий не попадает:
# в нём куки сессии.
PROFILE_DIR = config.DATA / "browser_profile"

# Сколько раз перезагружать страницу, прежде чем признать её сбойной.
PAGE_RETRIES = 3


class BrowserClient:
    """Тот же интерфейс, что у CianClient, но страницы берёт браузер."""

    def __init__(self, pause_min: float | None = None, pause_max: float | None = None,
                 headless: bool = False):
        self.pause_min = config.PAUSE_MIN if pause_min is None else pause_min
        self.pause_max = config.PAUSE_MAX if pause_max is None else pause_max
        # headless по умолчанию выключен не из осторожности, а по факту:
        # в headless площадка отвечает 403, в обычном режиме — отдаёт
        # страницу. Окно браузера будет видно всё время обхода.
        self.headless = headless
        self.transport = "browser"
        self.requests_made = 0
        self._playwright = None
        self._context = None
        self._page = None

    # ------------------------------------------------------------------
    def _ensure_browser(self):
        if self._page is not None:
            return

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as err:
            raise RuntimeError(
                "Нужен playwright: pip install playwright"
            ) from err

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        # channel="chrome" — настоящий установленный Chrome, а не Chromium
        # из комплекта Playwright: у них разные отпечатки, и проходит
        # именно первый.
        self._context = self._playwright.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            channel="chrome",
            headless=self.headless,
            locale="ru-RU",
            viewport={"width": 1440, "height": 900},
            # Пусто на макбуке, --no-sandbox при сборе в контейнере: см.
            # CHROME_NO_SANDBOX в etl/config.py.
            args=["--no-sandbox"] if config.CHROME_NO_SANDBOX else [],
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

        # Не грузим то, что нам не нужно. Данные лежат в тексте страницы,
        # а картинки, шрифты и видео мы не читаем — только ссылки на фото
        # из разметки. Это экономит и наш канал, и нагрузку на площадку.
        # Скрипты и стили НЕ блокируются: они дёшевы, а их отсутствие —
        # нетипичное поведение клиента.
        self._page.route(
            "**/*",
            lambda route: (route.abort()
                           if route.request.resource_type in ("image", "font", "media")
                           else route.continue_()),
        )

        # Сначала главная — как человек. Сайт выставляет сессионные куки,
        # и без них выдача не отдаётся.
        resp = self._page.goto("https://www.cian.ru/",
                               wait_until="domcontentloaded", timeout=60_000)
        html = self._page.content()
        if self._looks_blocked(html):
            raise Blocked("Главная страница отдала заглушку даже в браузере.")
        self.requests_made += 1
        log.info("Браузер запущен, сессия установлена (кук: %d)",
                 len(self._context.cookies()))
        self._sleep()

    @staticmethod
    def _looks_blocked(text: str) -> bool:
        markers = ("cian_waf_block", "captcha", "antibot", "vpnblock",
                   "подозрительный трафик")
        head = text[:4000].lower()
        return any(m in head for m in markers)

    def _sleep(self) -> None:
        time.sleep(random.uniform(self.pause_min, self.pause_max))

    # ------------------------------------------------------------------
    def describe_query(self, bucket: Bucket, page: int = 1,
                       pets_only: bool = False) -> dict:
        return {"transport": "browser", "url": config.SERP_URL,
                "params": build_url_params(bucket, page, pets_only)}

    def search(self, bucket: Bucket, page: int = 1,
               pets_only: bool = False) -> tuple[list[dict], int]:
        self._ensure_browser()
        params = build_url_params(bucket, page, pets_only)
        url = f"{config.SERP_URL}?{urllib.parse.urlencode(params)}"

        last_err: Exception | None = None
        for attempt in range(PAGE_RETRIES):
            try:
                resp = self._page.goto(url, wait_until="domcontentloaded",
                                       timeout=90_000)
                self.requests_made += 1
                status = resp.status if resp else None
                html = self._page.content()
            except PlaywrightError as err:
                # Страница не открылась за полторы минуты или браузер
                # ответил ошибкой. Это НЕ повод ронять весь обход.
                #
                # 27.09.2026 ровно так и вышло: одна страница фильтра
                # «с животными» по Вешнякам не открылась, исключение
                # прошло мимо всех обработчиков, и дневной сбор
                # остановился на третьей корзине из почти трёхсот.
                # Собранными оказались 111 объявлений вместо 16 тысяч.
                last_err = err
                wait = 5.0 * (2 ** attempt)
                log.warning("Страница не открылась (%s из %s): %s. Пауза %.0f с",
                            attempt + 1, PAGE_RETRIES, type(err).__name__, wait)
                time.sleep(wait)
                continue

            if status == 403 or self._looks_blocked(html):
                raise Blocked(f"Площадка закрыла доступ (HTTP {status}). Сбор остановлен.")

            try:
                offers, total = extract_offers(html)
            except SerpNotFound as err:
                # Разовый сбой загрузки: не догрузился скрипт, оборвался
                # ответ. Повторяем с нарастающей паузой — ронять из-за
                # одной страницы часовой обход незачем.
                last_err = err
                wait = 5.0 * (2 ** attempt)
                log.warning("Страница без данных выдачи (%s из %s), пауза %.0f с",
                            attempt + 1, PAGE_RETRIES, wait)
                time.sleep(wait)
                continue

            self._sleep()
            return offers, total

        # Повторы не помогли. Пустой корзиной это считать нельзя — район
        # молча выпал бы из выборки, — поэтому поднимаем отдельную ошибку:
        # сборщик пометит корзину сбойной и пойдёт дальше.
        # Повторы не помогли — ни по содержимому, ни по загрузке.
        # Корзина помечается сбойной, обход идёт дальше.
        raise PageWithoutData(f"Страница не далась после "
                              f"{PAGE_RETRIES} попыток: {last_err}")

    def districts_tree(self, location_id: int = config.REGION_MOSCOW) -> list[dict]:
        """Справочник районов здесь не запрашивается — и не нужен.

        Служебный эндпоинт, которым он брался раньше, отвечает 404: служба
        у площадки переехала. Искать новый не понадобилось — справочник
        собирается из уже скачанных страниц (`python -m etl.districts`),
        потому что каждое объявление несёт свою географию целиком, с теми
        же идентификаторами. Обход читает готовый файл из кеша.

        Если файла ещё нет, обход не ломается: нарезка пойдёт по
        комнатности и цене, запросов будет больше, покрытие останется
        полным.
        """
        return []

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._page = self._context = self._playwright = None
