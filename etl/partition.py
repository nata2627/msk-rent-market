"""Нарезка пространства поиска на корзины.

Зачем это нужно. ЦИАН не отдаёт выдачу глубже 54-й страницы по 28 объявлений,
то есть не больше 1512 штук на запрос, сколько бы их ни нашлось. Активных
объявлений о длительной аренде в Москве — десятки тысяч. Значит «собрать всю
Москву» одним широким запросом физически невозможно, и вопрос не в терпении,
а в том, как разрезать запрос на части, каждая из которых гарантированно
помещается в предел.

Как это работает. Корзина — это точка в пространстве (район, комнатность,
диапазон цены). Обход рекурсивный: спрашиваем у площадки, сколько объявлений
в корзине; если заявленное число упирается в предел выдачи — делим корзину по
очередной оси и повторяем для каждой половины. Делим по осям в порядке
район -> комнатность -> цена, от самой крупной к самой мелкой.

Про цену отдельно. Диапазоны цены здесь — ТЕХНИЧЕСКИЙ способ нарезки, а не
фильтр отбора. Они идут встык и покрывают шкалу от нуля до бесконечности:
[0, 59999], [60000, +inf). Ни одно объявление не может провалиться между
диапазонами, и ни одно не попадёт в два сразу. Это инвариант, который
проверяется тестами, потому что от него зависит несмещённость всей выборки:
обрезание данных по цене сместило бы оценки регрессии, где цена — как раз
объясняемая переменная.

Модуль намеренно не ходит в сеть: он превращает корзину в словарь запроса и
делит корзины. Всё, что можно проверить без площадки, проверяется тестами.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from etl.config import (
    DEAL_TYPE_LONG_TERM,
    MAX_BUCKET_DEPTH,
    PRICE_FIRST_SPLIT,
    PRICE_MIN_WIDTH,
    PRICE_SPLIT_FLOOR,
    REGION_MOSCOW,
    ROOM_CODES,
)


@dataclass(frozen=True)
class Bucket:
    """Одна корзина поиска.

    None на оси означает «ось не задействована, берём всё». price_max = None
    означает открытый сверху диапазон, а не отсутствие верхней границы
    как ошибку: именно так покрывается хвост дорогих квартир.
    """

    district_id: int | None = None
    district: str | None = None
    okrug: str | None = None
    rooms: str | None = None
    price_min: int | None = None
    price_max: int | None = None
    depth: int = 0

    # ------------------------------------------------------------------
    def label(self) -> str:
        parts = [self.district or self.okrug or "вся Москва"]
        if self.rooms:
            parts.append(f"комнат={self.rooms}")
        if self.price_min is not None or self.price_max is not None:
            lo = self.price_min if self.price_min is not None else 0
            hi = self.price_max if self.price_max is not None else "inf"
            parts.append(f"цена {lo}..{hi}")
        return ", ".join(parts)

    def as_columns(self) -> dict:
        """Поля для raw.bucket."""
        return {
            "okrug": self.okrug,
            "district": self.district,
            "district_id": self.district_id,
            "rooms": self.rooms,
            "price_min": self.price_min,
            "price_max": self.price_max,
            "depth": self.depth,
        }


# ---------------------------------------------------------------------
# Запрос к площадке
# ---------------------------------------------------------------------
def build_query(bucket: Bucket, page: int = 1, pets_only: bool = False) -> dict:
    """Корзина -> тело запроса jsonQuery.

    Цена задаётся только границами текущей корзины, а корзины покрывают всю
    шкалу — это нарезка, а не отсев.

    pets_only включает фильтр площадки «можно с животными». Это НЕ нарушение
    правила «не фильтровать по тому, что изучаем», а способ разметки. Каждая
    корзина обходится дважды: без фильтра — и это полный набор, контрольная
    группа сохраняется целиком, — и с фильтром, что даёт подмножество,
    которое сама площадка считает разрешающим животных. Метка ставится по
    разнице двух множеств.

    Так надёжнее разбора описания: в выдаче ЦИАНа поля petsAllowed нет вовсе
    (проверено на живой карточке), и без этого прохода про животных можно
    было бы судить только по тексту.
    """
    query: dict = {
        "_type": "flatrent",
        "engine_version": {"type": "term", "value": 2},
        "region": {"type": "terms", "value": [REGION_MOSCOW]},
        # Длительная аренда. Посуточные — отдельный рынок с другой ценой за
        # месяц; если такое объявление всё же придёт, оно не выбрасывается,
        # а помечается флагом is_daily и исключается уже в анализе.
        "for_day": {"type": "term", "value": "!1"},
        "page": {"type": "term", "value": page},
    }
    if bucket.district_id is not None:
        query["district"] = {"type": "terms", "value": [bucket.district_id]}
    if bucket.rooms is not None:
        query["room"] = {"type": "terms", "value": [int(bucket.rooms)]}
    if pets_only:
        query["pets"] = {"type": "term", "value": True}

    price = {}
    if bucket.price_min is not None:
        price["gte"] = bucket.price_min
    if bucket.price_max is not None:
        price["lte"] = bucket.price_max
    if price:
        query["price"] = {"type": "range", "value": price}

    return {"jsonQuery": query}


def build_url_params(bucket: Bucket, page: int = 1, pets_only: bool = False) -> dict:
    """Корзина -> параметры адреса публичной страницы выдачи.

    Имена параметров взяты не из догадок, а из самой площадки: в состоянии
    архивной страницы лежит список paginationUrls, который ЦИАН строит себе
    сам, и в нём видно и набор ключей (deal_type, offer_type, engine_version,
    type, region), и то, что номер страницы передаётся как p.

    Комнатность задаётся отдельным параметром на каждое значение: room1=1,
    room2=1 и так далее — это видно в сохранённых адресах вида
    `room5=1&room6=1&room7=1`. Соответствие кода студии и свободной
    планировки проверяется по числу результатов, см. check_room_codes.
    """
    params: dict[str, str | int] = {
        "deal_type": "rent",
        "offer_type": "flat",
        "engine_version": 2,
        "type": DEAL_TYPE_LONG_TERM,
        "region": REGION_MOSCOW,
        # Не подмешивать соседние регионы. Область — другой рынок, и её
        # объявления в московских медианах занизили бы всё сразу. Параметр
        # подсмотрен в адресах, которые строит сама площадка.
        "with_neighbors": 0,
    }
    if page > 1:
        params["p"] = page
    if bucket.district_id is not None:
        params["district[0]"] = bucket.district_id
    if bucket.rooms is not None:
        params[f"room{bucket.rooms}"] = 1
    if bucket.price_min is not None:
        params["minprice"] = bucket.price_min
    if bucket.price_max is not None:
        params["maxprice"] = bucket.price_max
    if pets_only:
        params["pets"] = 1
    return params


# Контрольные числа со страницы поиска без фильтров, снятые вручную
# 2026-09-22. По ним проверяется, что коды комнатности расставлены верно:
# если запрос с room1=1 вернёт не около семи тысяч, а что-то другое,
# значит параметр означает не то, что мы думаем.
KNOWN_ROOM_COUNTS = {
    "1": 7_247,
    "2": 9_451,
    "3": 4_733,
    "7": 2_616,   # студии
}


def check_room_codes(claimed: dict[str, int], tolerance: float = 0.25) -> list[str]:
    """Какие коды комнатности разошлись с контрольными числами.

    Возвращает список подозрительных кодов. Допуск широкий: рынок живой,
    и числа меняются день ото дня. Ловим не расхождение в проценты,
    а ошибку в разы — то есть перепутанный параметр.
    """
    suspicious = []
    for code, expected in KNOWN_ROOM_COUNTS.items():
        got = claimed.get(code)
        if got is None:
            continue
        if not (expected * (1 - tolerance) <= got <= expected * (1 + tolerance)):
            suspicious.append(code)
    return suspicious


# ---------------------------------------------------------------------
# Деление корзины
# ---------------------------------------------------------------------
def split_price(price_min: int | None, price_max: int | None) -> list[tuple[int, int | None]]:
    """Разделить диапазон цены на два встык.

    Три случая:

    1. Диапазон не задан вовсе: делим в PRICE_FIRST_SPLIT — примерно медиане
       рынка. Деление пополам от нуля до бесконечности не определено, а точка
       около медианы сразу даёт две сопоставимые по наполнению половины.

    2. Диапазон открыт сверху [a, inf): делим в 2a. Арифметическая середина
       здесь тоже не определена, а удвоение — естественный шаг по шкале,
       которая скошена вправо: дорогих квартир мало, и хвост надо проходить
       крупными шагами, иначе дробление будет долгим и бесполезным.

    3. Диапазон конечен [a, b]: делим в геометрическом среднем sqrt(a*b), а не
       в арифметическом (a+b)/2. Арендные ставки распределены примерно
       логнормально, поэтому по логарифмической шкале половины получаются
       равными по числу объявлений, а по линейной — нет: середина между
       30 и 300 тысячами это 165 тысяч, выше которых почти ничего нет, и
       такое деление не уменьшило бы корзину.

    Возвращает список [(min, max), ...] встык: верх первого куска на единицу
    меньше низа второго, дыр и пересечений нет.
    """
    lo = price_min if price_min is not None else 0

    if price_max is None:
        cut = PRICE_FIRST_SPLIT if lo == 0 else lo * 2
        return [(lo, cut - 1), (cut, None)]

    if price_max - lo < PRICE_MIN_WIDTH:
        return []                      # дальше дробить нечего

    # Опора снизу: от нуля геометрическое среднее схлопывается к корню
    # (середина между 0 и 60 000 вышла бы 244 рубля), и половина корзин
    # оказалась бы пустой. Границы корзин при этом не меняются — меняется
    # только точка разреза, поэтому покрытие остаётся сплошным.
    cut = int((max(lo, PRICE_SPLIT_FLOOR) * price_max) ** 0.5)
    # Геометрическое среднее может прилипнуть к краю на узком диапазоне —
    # тогда одна половина вышла бы пустой, а вторая равной исходной, и
    # рекурсия не сошлась бы.
    cut = min(max(cut, lo + 1), price_max)
    if cut <= lo or cut > price_max:
        return []
    return [(lo, cut - 1), (cut, price_max)]


def split(bucket: Bucket, districts: list[dict] | None = None) -> list[Bucket]:
    """Разделить корзину по следующей доступной оси.

    Порядок осей — от крупной к мелкой: район, затем комнатность, затем цена.
    Смысл порядка в том, что район и комнатность это осмысленные разрезы, по
    которым всё равно нужна аналитика, а деление по цене — чисто техническое.
    Поэтому цену трогаем последней, когда без этого уже не обойтись.

    Пустой список означает, что делить больше нечем: корзину придётся забрать
    не полностью и честно пометить как усечённую.
    """
    # Предел глубины: см. комментарий к MAX_BUCKET_DEPTH в config.
    if bucket.depth >= MAX_BUCKET_DEPTH:
        return []

    # Ось 1: район.
    if bucket.district_id is None and districts:
        return [
            replace(
                bucket,
                district_id=d["id"],
                district=d["name"],
                okrug=d.get("okrug"),
                depth=bucket.depth + 1,
            )
            for d in districts
        ]

    # Ось 2: комнатность.
    if bucket.rooms is None:
        return [
            replace(bucket, rooms=code, depth=bucket.depth + 1)
            for code in ROOM_CODES
        ]

    # Ось 3: цена.
    return [
        replace(bucket, price_min=lo, price_max=hi, depth=bucket.depth + 1)
        for lo, hi in split_price(bucket.price_min, bucket.price_max)
    ]


def root_buckets(districts: list[dict] | None) -> list[Bucket]:
    """С чего начинается полный обход.

    Если справочник районов доступен, сразу стартуем с районов: одна корзина
    на всю Москву заведомо в предел не влезет, и запрос по ней был бы потрачен
    впустую. Если справочника нет — стартуем с одной корзины, и рекурсия
    разделит её по комнатности и цене. Покрытие в обоих случаях полное,
    во втором просто больше запросов.
    """
    if districts:
        return [
            Bucket(district_id=d["id"], district=d["name"], okrug=d.get("okrug"))
            for d in districts
        ]
    return [Bucket()]


def covers_whole_scale(ranges: Iterable[tuple[int, int | None]]) -> bool:
    """Проверка инварианта: диапазоны идут встык от нуля до бесконечности.

    Вынесено в функцию, а не только в тест, чтобы то же самое можно было
    проверить на журнале реального обхода в raw.bucket.
    """
    ordered = sorted(ranges, key=lambda r: r[0])
    if not ordered or ordered[0][0] != 0:
        return False
    for (_, hi), (next_lo, _) in zip(ordered, ordered[1:]):
        if hi is None or hi + 1 != next_lo:
            return False
    return ordered[-1][1] is None
