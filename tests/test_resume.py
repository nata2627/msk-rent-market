"""Тесты продолжения прерванного обхода.

Площадка 12.10.2026 после аварии отдавала выдачу рывками, обход падал
через минуты, и каждый следующий заход строил список районов заново. За
пять часов и четыре падения очередь не ушла дальше первых двух десятков
районов: прогоны 155 и 156 целиком потратились на повтор уже собранного.

Отсюда режим продолжения — пропуск корзин, собранных за эту дату снимка.
Цена ошибки здесь выше обычной: функция решает, чего НЕ спрашивать, и
лишний пропуск не виден в данных никак. Поэтому проверяется и то, что
пропускается нужное, и то, что не пропускается лишнее.

Проверяется через collect.run с подставным клиентом — правило живёт в
развилке посреди цикла обхода, и проверять его на кусках значило бы
проверять не его. Набор ключей передаётся готовым: откуда он взялся,
обходу безразлично.
"""
from datetime import date

from etl import collect
from etl.partition import Bucket

DISTRICTS = [{"id": 1, "name": "Гольяново", "okrug": "ВАО"},
             {"id": 2, "name": "Хамовники", "okrug": "ЦАО"}]


class FakeClient:
    """Клиент, отвечающий по таблице подписей корзин."""

    transport = "fake"

    def __init__(self, table, default=(0, 0)):
        self.table = table
        self.default = default
        self.requests_made = 0
        self.seen_buckets = []

    def search(self, bucket, page=1):
        self.requests_made += 1
        claimed, unique = self.table.get(bucket.label(), self.default)
        if page == 1:
            self.seen_buckets.append(bucket.label())
        offers = [{"id": f"{bucket.label()}#{i}"} for i in range(unique)]
        return offers, claimed

    def describe_query(self, bucket):
        return {}

    def close(self):
        pass


def run_with(table, monkeypatch, rooms, resume_keys=None, districts=None):
    client = FakeClient(table)
    monkeypatch.setattr(collect, "CianClient", lambda **kw: client)
    monkeypatch.setattr(collect, "load_districts",
                        lambda c, refresh=False: districts or [])
    collect.run("full", date(2026, 10, 12), None, dry_run=True,
                refresh_districts=False, pets_pass=False,
                transport="html", rooms=rooms, resume=True,
                resume_keys=resume_keys)
    return client


def test_собранная_корзина_не_запрашивается_снова(monkeypatch):
    """Ключ совпал — корзины в обходе нет вовсе, ни одного запроса."""
    table = {"вся Москва, комнат=4": (300, 300)}
    key = (None, "4", None, None)
    client = run_with(table, monkeypatch, rooms=["4"], resume_keys={key})
    assert client.seen_buckets == []
    assert client.requests_made == 0


def test_несобранная_корзина_запрашивается(monkeypatch):
    """Чужой ключ пропуска не вызывает: лишнего не теряем."""
    table = {"вся Москва, комнат=4": (300, 300)}
    key = (None, "2", None, None)          # другая комнатность
    client = run_with(table, monkeypatch, rooms=["4"], resume_keys={key})
    assert client.seen_buckets == ["вся Москва, комнат=4"]


def test_пустой_набор_ничего_не_меняет(monkeypatch):
    """Обычный день: пропускать нечего, обход идёт как раньше."""
    table = {"вся Москва, комнат=4": (300, 300)}
    client = run_with(table, monkeypatch, rooms=["4"], resume_keys=set())
    assert client.seen_buckets == ["вся Москва, комнат=4"]


def test_ключ_с_пустым_районом_совпадает(monkeypatch):
    """Корзина «вся Москва» имеет district_id = None.

    В SQL сравнение с NULL никогда не истинно, и ключ бы не совпал. Здесь
    ключи сравниваются множеством кортежей в Python, где None == None, —
    тест держит именно это свойство, чтобы перенос сравнения в запрос не
    прошёл незамеченным.
    """
    assert (None, "4", None, None) == (None, "4", None, None)
    table = {"вся Москва, комнат=4": (300, 300)}
    client = run_with(table, monkeypatch, rooms=["4"],
                      resume_keys={(None, "4", None, None)})
    assert client.seen_buckets == []


def test_границы_цены_входят_в_ключ(monkeypatch):
    """Корзины одного района с разными ценами — разные корзины.

    Если бы цена в ключ не входила, собранная дешёвая половина заперла бы
    дорогую, и дорогие квартиры пропали бы из дня целиком.
    """
    assert Bucket(district_id=1, rooms="4", price_min=0, price_max=50_000) \
        != Bucket(district_id=1, rooms="4", price_min=50_000)
    # Ключ собранной половины не должен гасить вторую.
    table = {"вся Москва, комнат=4": (300, 300)}
    собранная_половина = (1, "4", 0, 50_000)
    client = run_with(table, monkeypatch, rooms=["4"],
                      resume_keys={собранная_половина})
    assert client.seen_buckets == ["вся Москва, комнат=4"]


def test_пустой_обход_не_считается_успешным(monkeypatch):
    """Все корзины сбойные — статус не 'ok'.

    Прогон 149 от 12.10.2026 закрылся успешным с двумя нечитаемыми
    корзинами и нулём объявлений, из-за чего день попал в покрытие и
    грозил 14 тысячами мнимых снятий. Порог MAX_FAILED_BUCKETS этого не
    ловит: он срабатывает на десяти сбоях подряд, а корзин было две.
    """
    from etl.cian_client import PageWithoutData

    class BrokenClient(FakeClient):
        def search(self, bucket, page=1):
            self.requests_made += 1
            raise PageWithoutData("страница не далась")

    client = BrokenClient({})
    monkeypatch.setattr(collect, "CianClient", lambda **kw: client)
    monkeypatch.setattr(collect, "load_districts", lambda c, refresh=False: [])
    code = collect.run("narrow", date(2026, 10, 12), None, dry_run=True,
                       refresh_districts=False, pets_pass=False,
                       transport="html")
    # Код возврата ненулевой — обход сообщает о беде, а не о success.
    assert code != 0


def test_сбой_страницы_не_роняет_обход(monkeypatch):
    """Страница в середине корзины не далась — обход идёт дальше.

    17.10.2026 день собрал нормальные 16 161 объявление и всё равно
    закрылся как failed: исключение с сороковой страницы одной корзины
    летело наружу мимо обработчика, который прикрывал только первую.
    Непокрытый день выпадает из расчёта снятий, то есть цена такой
    мелочи — дыра в сроках жизни объявлений.
    """
    from etl.cian_client import PageWithoutData

    class FlakyPages(FakeClient):
        """Первая страница даётся, вторая нет."""

        def search(self, bucket, page=1):
            self.requests_made += 1
            if page == 1:
                self.seen_buckets.append(bucket.label())
                # Заявляем много, чтобы обход пошёл листать дальше.
                return [{"id": f"{bucket.label()}#0"}], 200
            raise PageWithoutData("страница не далась")

    client = FlakyPages({})
    monkeypatch.setattr(collect, "CianClient", lambda **kw: client)
    monkeypatch.setattr(collect, "load_districts", lambda c, refresh=False: [])
    code = collect.run("narrow", date(2026, 10, 17), None, dry_run=True,
                       refresh_districts=False, pets_pass=False,
                       transport="html")

    # Обе корзины узкого среза пройдены: обход не свалился на первой.
    assert client.seen_buckets == ["вся Москва, комнат=1", "вся Москва, комнат=2"]
    # И вернулся не «успех»: корзины недобраны, это honest failure.
    assert code != 0
