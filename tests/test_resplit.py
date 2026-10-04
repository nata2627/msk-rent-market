"""Тесты переделения корзины по недобору.

Площадка может пообещать больше, чем отдаст. 04.10.2026 корзина «вся
Москва, 4 комнаты» заявила 1231 объявление, уложилась в порог деления,
честно прошла все 44 страницы без единой ошибки и отдала 633 уникальных:
примерно с двадцать третьей страницы пошли повторы уже показанного.

Поймать такое заранее нельзя — решение делить принимается до листания, по
одному заявленному числу. Поэтому недобор теперь не только записывается в
журнал, но и лечится: корзина делится и проходится заново.

Проверяется поведение целиком, через collect.run с подставным клиентом:
правило живёт в развилке посреди цикла обхода, и проверять его на кусках
значило бы проверять не его.
"""
from datetime import date

import pytest

from etl import collect, config
from etl.partition import Bucket


class FakeClient:
    """Клиент, отвечающий по заранее заданной таблице.

    Ключ — подпись корзины, значение — пара «сколько заявлено, сколько
    отдать уникальных». Страницы не моделируются: важно не как площадка
    врёт, а что сбор делает с расхождением.
    """

    transport = "fake"

    def __init__(self, table, default=(0, 0)):
        self.table = table
        self.default = default
        self.requests_made = 0
        self.seen_buckets = []

    def _key(self, bucket):
        return bucket.label()

    def search(self, bucket, page=1):
        self.requests_made += 1
        claimed, unique = self.table.get(self._key(bucket), self.default)
        if page == 1:
            self.seen_buckets.append(self._key(bucket))
        # Уникальные выдаём только на первой странице, дальше повторы —
        # ровно так вела себя площадка на четырёхкомнатных.
        offers = ([{"id": f"{self._key(bucket)}#{i}"} for i in range(unique)]
                  if page == 1 else [{"id": f"{self._key(bucket)}#0"}])
        return offers, claimed

    def describe_query(self, bucket):
        return {}

    def close(self):
        pass


def run_with(table, monkeypatch, rooms, districts=None):
    """Прогнать обход на подставном клиенте и вернуть его же.

    Клиент создаётся внутри collect.run, снаружи его не передать, поэтому
    подменяется сам класс. Экземпляр ловится в ящик: проверяем мы именно
    то, какие корзины он увидел.
    """
    client = FakeClient(table)
    monkeypatch.setattr(collect, "CianClient", lambda **kw: client)
    monkeypatch.setattr(collect, "load_districts",
                        lambda c, refresh=False: districts or [])
    collect.run("full", date(2026, 10, 4), None, dry_run=True,
                refresh_districts=False, pets_pass=False,
                transport="html", rooms=rooms)
    return client


DISTRICTS = [{"id": 1, "name": "Гольяново", "okrug": "ВАО"},
             {"id": 2, "name": "Хамовники", "okrug": "ЦАО"}]


def test_underfilled_bucket_is_split_and_walked_again(monkeypatch):
    """Корзина 04.10: заявлено 1231, отдано 633. Должна поделиться."""
    table = {"вся Москва, комнат=4": (1231, 633),
             "Гольяново, комнат=4": (300, 300),
             "Хамовники, комнат=4": (900, 900)}
    client = run_with(table, monkeypatch, rooms=["4"], districts=DISTRICTS)
    assert "Гольяново, комнат=4" in client.seen_buckets
    assert "Хамовники, комнат=4" in client.seen_buckets


def test_healthy_bucket_is_not_split(monkeypatch):
    """Недобор на единицы объявлений — живая выдача, а не поломка.

    Если делить и такие, обход размножит запросы к площадке на пустом
    месте: по всей истории проекта доля ниже 0.9 встречается у 174 корзин,
    и только у четырёх из них недобор переваливает за сотню.
    """
    table = {"вся Москва, комнат=4": (1200, 1190)}
    client = run_with(table, monkeypatch, rooms=["4"], districts=DISTRICTS)
    assert client.seen_buckets == ["вся Москва, комнат=4"]


def test_small_bucket_with_bad_ratio_is_not_split(monkeypatch):
    """Четыре недобранных из тридцати трёх — это 88%, но делить незачем.

    Доля без абсолютного числа цепляла бы сотни мелких корзин ради
    нескольких объявлений каждая.
    """
    table = {"вся Москва, комнат=4": (33, 29)}
    client = run_with(table, monkeypatch, rooms=["4"], districts=DISTRICTS)
    assert client.seen_buckets == ["вся Москва, комнат=4"]


def test_big_absolute_gap_with_good_ratio_is_not_split(monkeypatch):
    """Недобор 110 из 1200 — те же проценты шума, а не поломка.

    Нужны оба условия сразу, иначе правило начнёт делить здоровые
    крупные корзины. Заявленное число взято под порогом обычного
    деления: выше 1285 корзина делится ещё до листания, и недобор до неё
    просто не доходит.
    """
    table = {"вся Москва, комнат=4": (1200, 1090)}
    client = run_with(table, monkeypatch, rooms=["4"], districts=DISTRICTS)
    assert client.seen_buckets == ["вся Москва, комнат=4"]


def test_truncated_bucket_is_not_resplit(monkeypatch):
    """Корзина больше предела выдачи делится ДО листания.

    Если она дошла до недобора, значит делить её было нечем, и второй
    заход кончился бы тем же — только запросов стало бы вдвое больше.
    """
    table = {"вся Москва, комнат=4": (config.BUCKET_CAPACITY + 500, 600)}
    client = run_with(table, monkeypatch, rooms=["4"], districts=None)
    # Районов нет, комнатность задана, остаётся цена — но корзина за
    # пределом выдачи помечается усечённой и по недобору не переделяется.
    assert client.seen_buckets.count("вся Москва, комнат=4") == 1


def test_resplit_stops_when_there_is_nothing_left_to_split(monkeypatch):
    """Дно рекурсии. Корзина недобирает, но осей больше нет.

    Без этой проверки правило ушло бы в бесконечный цикл: делим, снова
    недобираем, снова делим.
    """
    bucket = Bucket(rooms="4", district_id=1, district="Гольяново",
                    price_min=100_000, price_max=100_000,
                    depth=config.MAX_BUCKET_DEPTH)
    from etl.partition import split
    assert split(bucket, DISTRICTS) == []


@pytest.mark.parametrize("claimed,collected,expected", [
    (1231, 633, True),    # случай 04.10.2026
    (1280, 659, True),    # трёшки дороже 240 тысяч, 22.09.2026
    (856, 662, True),     # недобор 194 — ещё по эту сторону разрыва
    (597, 516, False),    # недобор 81 — уже по ту
    (33, 29, False),
    (1200, 1090, False),
])
def test_rule_matches_the_table_it_was_derived_from(claimed, collected, expected):
    """Правило должно вести себя ровно так, как таблица в config.

    Пороги выведены из всех живых корзин за историю проекта, и этот тест
    держит их привязанными к тем самым случаям, а не к круглым числам.
    """
    missing = claimed - collected
    fires = (missing >= config.RESPLIT_MIN_MISSING
             and collected < claimed * config.RESPLIT_RATIO)
    assert fires is expected
