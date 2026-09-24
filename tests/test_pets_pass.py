"""Тесты разметки животных двойным проходом.

Идея: каждая корзина обходится дважды — без фильтра и с фильтром площадки
«можно с животными». Первый проход даёт полный набор и сохраняет
контрольную группу, второй помечает подмножество. Это разметка, а не отбор.
"""
from etl.partition import Bucket, build_query


def test_main_pass_has_no_pets_filter():
    """Основной проход обязан собирать всё. Без объявлений, где животные
    не разрешены, не с чем сравнивать, и надбавку не посчитать."""
    query = build_query(Bucket(district_id=21, rooms="2"))["jsonQuery"]
    assert "pets" not in query


def test_labelling_pass_adds_the_filter():
    query = build_query(Bucket(district_id=21, rooms="2"), pets_only=True)["jsonQuery"]
    assert query["pets"] == {"type": "term", "value": True}


def test_both_passes_describe_the_same_bucket():
    """Иначе разница двух множеств означала бы не «разрешены животные»,
    а «разные корзины»."""
    bucket = Bucket(district_id=21, rooms="2", price_min=60_000, price_max=119_999)
    plain = build_query(bucket)["jsonQuery"]
    pets = build_query(bucket, pets_only=True)["jsonQuery"]
    assert pets.pop("pets")
    assert plain == pets


def test_filter_does_not_leak_into_pagination():
    a = build_query(Bucket(rooms="1"), page=3, pets_only=True)["jsonQuery"]
    assert a["page"]["value"] == 3 and "pets" in a
    b = build_query(Bucket(rooms="1"), page=3)["jsonQuery"]
    assert "pets" not in b
