"""Тесты вопросов о рынке (scripts/box_ask.py).

Сеть и база не трогаются. Проверяется то, на чем держатся безопасность и
честность замера: параметры модели проходят белый список, в SQL значения
идут переменными psql, а не вклейкой строки, ответ собирает код, и
тестовые вопросы не протекли в подсказку модели.
"""
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import box_ask  # noqa: E402
import box_nl  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "ask"
DISTRICTS = [d for d in (ROOT / "tests" / "fixtures" / "nl" / "districts.txt").read_text().splitlines() if d]
DEV = json.loads((FIX / "dev.json").read_text())
TEST = json.loads((FIX / "test.json").read_text())


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("тест полез в сеть")
    monkeypatch.setattr(box_nl.urllib.request, "urlopen", boom)


def test_наборы_не_пересекаются_и_заморожены():
    assert len(DEV) == 30 and len(TEST) == 60
    dev = {box_nl.norm(c["text"]) for c in DEV}
    assert not dev & {box_nl.norm(c["text"]) for c in TEST}


def test_тестовые_вопросы_не_протекли_в_подсказку():
    prompt = box_nl.norm(box_ask._system("json") + box_ask._system("functions"))
    assert not [c["text"] for c in TEST if box_nl.norm(c["text"]) in prompt]


@pytest.mark.parametrize("case", DEV + TEST, ids=[c["text"] for c in DEV + TEST])
def test_ожидаемый_вызов_уже_в_каноническом_виде(case):
    # Ожидание должно пройти сито кода без изменений, иначе точного вызова
    # не получить ни при каком ответе модели.
    w = case["want"]
    got = box_ask.validate({"function": w["fn"], "arguments": w["args"]}, DISTRICTS)
    assert got["fn"] == w["fn"] and got["args"] == w["args"] and not got["rejected"]


@pytest.mark.parametrize("raw, fn, rejected", [
    ({"function": "drop_table", "arguments": {}}, "none", []),
    ({"function": None}, "none", []),
    ({"function": "market_price", "arguments": {"rooms": [0, 9]}}, "market_price", ["комнаты [0, 9]"]),
    ({"function": "market_price", "arguments": {"district": "'; drop table x; --"}}, "market_price",
     ["район '; drop table x; --"]),
    ({"function": "price_drops", "arguments": {"days": 400}}, "price_drops", ["дней 400"]),
    ({"function": "price_drops", "arguments": {"days": True}}, "price_drops", ["дней True"]),
])
def test_сито_не_пропускает_чужое(raw, fn, rejected):
    got = box_ask.validate(raw, DISTRICTS)
    assert got["fn"] == fn and got["rejected"] == rejected
    if fn != "none":
        assert set(got["args"]) <= set(box_ask.FN[fn]["args"])


def test_лишний_параметр_отбрасывается():
    got = box_ask.validate({"function": "data_status", "arguments": {"district": "Арбат"}}, DISTRICTS)
    assert got == {"fn": "data_status", "args": {}, "rejected": []}


def test_район_склоняется_к_справочнику():
    got = box_ask.validate({"function": "pets_share", "arguments": {"district": "в Хамовниках"}}, DISTRICTS)
    assert got["args"]["district"] == "Хамовники"


@pytest.mark.parametrize("fn", list(box_ask.SQL))
def test_значения_в_sql_только_переменными(fn):
    sql = box_ask.SQL[fn]
    assert "{" not in sql and "%" not in sql
    for v in re.findall(r"(?<!:):(?!:)'?(\w+)'?", sql):
        assert v in ("rooms", "district", "days", "order", "limit")


def test_у_каждой_функции_есть_запрос():
    assert set(box_ask.SQL) == set(box_ask.FN)


@pytest.mark.parametrize("fn, args, row, has", [
    ("market_price", {"rooms": [2], "district": "Арбат"},
     ["2026-10-28", "91", "150000", "116250", "250000", "2682"], "Медиана 150 000"),
    ("price_drops", {"rooms": [1], "days": 7}, ["2026-10-28", "120", "100", "-5.2"], "5,2%"),
    ("exits", {"rooms": [1, 2], "days": 1}, ["2026-10-28", "300", "67500"], "за 1 день"),
    ("new_offers", {"rooms": [1, 2], "days": 30}, ["2026-10-28", "900", "80000", "2026-10-01"],
     "Панель собирается с 01.10"),
    ("pets_share", {"rooms": [1, 2]}, ["2026-10-28", "100", "26", "2", "15"], "Не указано 57%"),
    ("data_status", {}, ["2026-10-28", "сегодня, 28.10", "2026-10-28", "t", "99.26", "16482", "0"],
     "закрыт успешным"),
])
def test_ответ_собирает_код_из_строки_базы(fn, args, row, has):
    assert has in box_ask.render(fn, args, [row])


def test_рейтинг_районов_списком():
    rows = [["2026-10-28", "Троицк", "30", "1009"], ["2026-10-28", "Щербинка", "37", "1053"]]
    out = box_ask.render("district_rank", {"rooms": [1], "order": "cheap", "limit": 2}, rows)
    assert "1. Троицк: 1 009" in out and "2. Щербинка" in out


def test_модель_бота_по_умолчанию_без_замера(monkeypatch, tmp_path):
    monkeypatch.setattr(box_ask, "REPO", tmp_path)
    assert box_ask.bot_model() == "GigaChat-2"
