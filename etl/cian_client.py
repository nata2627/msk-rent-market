"""Вежливый клиент к поиску ЦИАНа.

Правила, которые модуль соблюдает и которые важнее скорости:

* один поток, пауза со случайным джиттером между запросами;
* экспоненциальное отступление при сетевых ошибках, 429 и 5xx;
* браузерный User-Agent и обычные заголовки, живая сессия с куками;
* при явном ответе «доступ закрыт» сбор ОСТАНАВЛИВАЕТСЯ.

Последний пункт — не осторожность, а позиция. Капчу мы не обходим,
прокси не крутим, отпечаток клиента не подделываем. Если площадка перестала
отдавать данные, правильный ответ — снизить темп или остановиться. Во-первых,
это правовая гигиена. Во-вторых, репозиторий публичный, и в нём не должно
быть ничего, за что стыдно на собеседовании.
"""
from __future__ import annotations

import json
import logging
import random
import time

import requests

from etl import config
from etl.partition import Bucket, build_query, build_url_params
from etl.serp_html import SerpNotFound, extract_offers

log = logging.getLogger(__name__)


class PageWithoutData(RuntimeError):
    """Страница пришла, но состояния выдачи в ней нет.

    Отличается от Blocked принципиально. Blocked — площадка закрыла
    доступ, и продолжать нельзя. Здесь же чаще всего разовый сбой: не
    догрузился скрипт, оборвалось соединение на середине ответа. Такое
    лечится повтором, и ронять из-за одной страницы полуторачасовой обход
    незачем.

    Принимать такую страницу за пустую корзину по-прежнему нельзя: район
    молча выпал бы из выборки. Поэтому корзина помечается сбойной и
    попадает в журнал недобора.
    """


class Blocked(RuntimeError):
    """Площадка показала капчу или заблокировала доступ.

    Отдельный тип исключения, потому что реакция на него принципиально иная,
    чем на сетевую ошибку: повторять запрос нельзя, обход нужно прекратить.
    """


class CianClient:
    def __init__(self, pause_min: float | None = None, pause_max: float | None = None,
                 transport: str | None = None):
        # 'html' — публичная страница выдачи, 'api' — внутренний эндпоинт
        # фронтенда. По умолчанию первое: страница общедоступна, а служебный
        # интерфейс SPA защищён строже.
        self.transport = transport or config.TRANSPORT
        self.pause_min = config.PAUSE_MIN if pause_min is None else pause_min
        self.pause_max = config.PAUSE_MAX if pause_max is None else pause_max
        self.requests_made = 0
        self._warmed_up = False
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
            "Origin": "https://www.cian.ru",
            "Referer": "https://www.cian.ru/",
        })

    # ------------------------------------------------------------------
    @staticmethod
    def _looks_blocked(text: str) -> bool:
        """Признаки страницы-заглушки вместо данных.

        ЦИАН отдаёт её с кодом 200, поэтому проверять только HTTP-статус
        недостаточно: молча приняли бы HTML за пустую выдачу и решили, что
        в районе нет объявлений.
        """
        # Маркер '<!doctype html' сюда НЕ годится: страница выдачи тоже HTML.
        # Ловим явные признаки заглушки, а отсутствие данных на внешне
        # нормальной странице отлавливается отдельно, разбором состояния.
        markers = ("cian_waf_block", "captcha", "antibot", "vpnblock",
                   "подозрительный трафик")
        head = text[:4000].lower()
        return any(m in head for m in markers)

    def _sleep(self) -> None:
        time.sleep(random.uniform(self.pause_min, self.pause_max))

    def _warm_up(self) -> None:
        """Открыть главную страницу, чтобы получить сессионные куки.

        Так делает любой браузер: сначала грузится сайт и выставляет куки,
        и только потом его скрипты обращаются к API. Идти сразу в
        api.cian.ru без единой куки — некорректное поведение клиента,
        и площадка вправе не отвечать.

        Это не обход защиты: мы не подделываем отпечаток и ничего не решаем
        за пользователя, а просто устанавливаем сессию так, как она и
        должна устанавливаться. Делается один раз за запуск.
        """
        if self._warmed_up:
            return
        self._warmed_up = True
        try:
            resp = self.session.get(
                "https://www.cian.ru/",
                headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                         "Sec-Fetch-Dest": "document",
                         "Sec-Fetch-Mode": "navigate",
                         "Sec-Fetch-Site": "none",
                         "Upgrade-Insecure-Requests": "1"},
                timeout=config.REQUEST_TIMEOUT,
            )
            self.requests_made += 1
        except requests.RequestException as err:
            log.warning("Главная страница не открылась (%s), идём дальше без сессии", err)
            return

        if self._looks_blocked(resp.text):
            raise Blocked("Главная страница ЦИАНа отдала заглушку. Сбор остановлен.")
        log.info("Сессия установлена, кук получено: %d", len(self.session.cookies))
        self._sleep()

    # ------------------------------------------------------------------
    def _post(self, url: str, payload: dict) -> dict:
        self._warm_up()
        last_err: Exception | None = None

        for attempt in range(config.MAX_RETRIES):
            try:
                resp = self.session.post(
                    url, json=payload, timeout=config.REQUEST_TIMEOUT,
                )
            except requests.RequestException as err:
                last_err = err
                wait = config.BACKOFF_BASE * (2 ** attempt)
                log.warning("Сеть не отвечает (%s), повтор через %.0f с", err, wait)
                time.sleep(wait)
                continue

            self.requests_made += 1

            if self._looks_blocked(resp.text):
                raise Blocked(
                    "ЦИАН вернул страницу-заглушку вместо данных "
                    f"(HTTP {resp.status_code}). Сбор остановлен."
                )

            if resp.status_code == 429 or resp.status_code >= 500:
                wait = config.BACKOFF_BASE * (2 ** attempt)
                log.warning("HTTP %s, отступаем на %.0f с", resp.status_code, wait)
                time.sleep(wait)
                last_err = RuntimeError(f"HTTP {resp.status_code}")
                continue

            if resp.status_code == 403:
                raise Blocked("ЦИАН ответил 403. Сбор остановлен.")

            resp.raise_for_status()

            try:
                data = resp.json()
            except json.JSONDecodeError as err:
                raise Blocked(f"Ответ не разбирается как JSON: {err}") from err

            self._sleep()
            return data

        raise RuntimeError(f"Не удалось получить {url} за {config.MAX_RETRIES} попыток") from last_err

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def describe_query(self, bucket: Bucket, page: int = 1,
                       pets_only: bool = False) -> dict:
        """Запрос в том виде, в каком он уйдёт на площадку.

        Пишется в raw.bucket: по журналу должно быть видно не только что
        собрали, но и чем именно спрашивали — иначе обход не воспроизвести.
        """
        if self.transport == "html":
            return {"transport": "html", "url": config.SERP_URL,
                    "params": build_url_params(bucket, page, pets_only)}
        return {"transport": "api", "url": config.SEARCH_URL,
                **build_query(bucket, page, pets_only)}

    def search(self, bucket: Bucket, page: int = 1,
               pets_only: bool = False) -> tuple[list[dict], int]:
        """Одна страница выдачи.

        Возвращает (объявления, заявленное число результатов). Заявленное
        число — это то, что площадка сама считает размером корзины; именно
        с ним сравнивается реально собранное, чтобы увидеть недобор.

        Форма объявления от транспорта не зависит: и страница, и API отдают
        один и тот же объект в 137 полей, поэтому etl/parse.py общий.
        """
        if self.transport == "html":
            return self._search_html(bucket, page, pets_only)
        return self._search_api(bucket, page, pets_only)

    def _search_html(self, bucket: Bucket, page: int,
                     pets_only: bool) -> tuple[list[dict], int]:
        html = self._get_page(config.SERP_URL, build_url_params(bucket, page, pets_only))
        try:
            return extract_offers(html)
        except SerpNotFound as err:
            # Страница пришла, но состояния выдачи в ней нет. Чаще всего это
            # значит, что вместо выдачи отдали что-то другое, и принимать это
            # за пустую корзину нельзя: район молча выпал бы из выборки.
            raise Blocked(f"Страница выдачи без данных: {err}") from err

    def _search_api(self, bucket: Bucket, page: int,
                    pets_only: bool) -> tuple[list[dict], int]:
        payload = self._post(config.SEARCH_URL, build_query(bucket, page, pets_only))
        data = payload.get("data") or {}
        offers = data.get("offersSerialized") or []
        claimed = data.get("aggregatedCount")
        if claimed is None:
            claimed = data.get("offerCount")
        return offers, int(claimed or 0)

    def _get_page(self, url: str, params: dict) -> str:
        """GET публичной страницы с обычными заголовками браузера."""
        self._warm_up()
        last_err: Exception | None = None

        for attempt in range(config.MAX_RETRIES):
            try:
                resp = self.session.get(
                    url, params=params, timeout=config.REQUEST_TIMEOUT,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "same-origin",
                        "Upgrade-Insecure-Requests": "1",
                    },
                )
            except requests.RequestException as err:
                last_err = err
                wait = config.BACKOFF_BASE * (2 ** attempt)
                log.warning("Сеть не отвечает (%s), повтор через %.0f с", err, wait)
                time.sleep(wait)
                continue

            self.requests_made += 1

            if self._looks_blocked(resp.text):
                raise Blocked(
                    f"ЦИАН вернул заглушку вместо страницы выдачи "
                    f"(HTTP {resp.status_code}). Сбор остановлен."
                )
            if resp.status_code == 403:
                raise Blocked("ЦИАН ответил 403. Сбор остановлен.")
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = config.BACKOFF_BASE * (2 ** attempt)
                log.warning("HTTP %s, отступаем на %.0f с", resp.status_code, wait)
                time.sleep(wait)
                last_err = RuntimeError(f"HTTP {resp.status_code}")
                continue

            resp.raise_for_status()
            self._sleep()
            return resp.text

        raise RuntimeError(f"Страница не получена за {config.MAX_RETRIES} попыток") from last_err

    def districts_tree(self, location_id: int = config.REGION_MOSCOW) -> list[dict]:
        """Справочник районов Москвы: округ -> район с идентификаторами.

        Нужен как основная ось нарезки. Отдаётся GET-ом, поэтому запрос
        отдельный, а не через _post.
        """
        self._warm_up()
        for attempt in range(config.MAX_RETRIES):
            try:
                resp = self.session.get(
                    config.DISTRICTS_URL,
                    params={"locationId": location_id},
                    timeout=config.REQUEST_TIMEOUT,
                )
            except requests.RequestException as err:
                time.sleep(config.BACKOFF_BASE * (2 ** attempt))
                log.warning("Справочник районов не ответил (%s)", err)
                continue

            self.requests_made += 1
            if self._looks_blocked(resp.text):
                raise Blocked("Справочник районов вернул заглушку.")
            if resp.status_code == 403:
                raise Blocked("Справочник районов ответил 403.")
            if resp.status_code != 200:
                time.sleep(config.BACKOFF_BASE * (2 ** attempt))
                continue

            self._sleep()
            return flatten_districts(resp.json())

        raise RuntimeError("Справочник районов недоступен")


def flatten_districts(payload) -> list[dict]:
    """Дерево округ -> район в плоский список корзин.

    Структура ответа у площадки менялась, поэтому разбор терпимый: ищем узлы
    с id и name на любом уровне и запоминаем имя родителя как округ. Ошибка
    здесь дорого стоит — потеряется целый район, и его объявления не попадут
    в выборку вообще, — поэтому лучше собрать лишнее и отфильтровать, чем
    полагаться на точную форму дерева.
    """
    out: list[dict] = []

    def walk(node, parent_name: str | None):
        if isinstance(node, list):
            for item in node:
                walk(item, parent_name)
            return
        if not isinstance(node, dict):
            return

        node_id = node.get("id")
        name = node.get("name") or node.get("fullName")
        children = node.get("childs") or node.get("children") or node.get("items")

        if children:
            # Узел с потомками — это округ: сам он как корзина не нужен,
            # иначе объявления посчитались бы дважды, в округе и в районе.
            walk(children, name or parent_name)
            return

        if node_id is not None and name:
            out.append({"id": int(node_id), "name": name, "okrug": parent_name})

    walk(payload.get("data", payload) if isinstance(payload, dict) else payload, None)

    # Дубли по id возможны, если дерево пришло с повторами.
    seen, unique = set(), []
    for d in out:
        if d["id"] not in seen:
            seen.add(d["id"])
            unique.append(d)
    return unique
