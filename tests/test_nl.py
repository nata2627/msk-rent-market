"""Тесты разбора фраз (scripts/box_nl.py).

Сеть не трогается: GigaChat подменяется. Проверяется то, на чем держится
безопасность и честность замера. Ответ модели проходит сито кода, и
мусор в файл фильтров не попадает. Откат на правила срабатывает, когда
модель молчит. Тестовые фразы не протекли в подсказку модели, иначе
замер на них ничего не стоит.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import box_nl  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "nl"
DISTRICTS = [d for d in (FIX / "districts.txt").read_text().splitlines() if d]
DEV = json.loads((FIX / "dev.json").read_text())
TEST = json.loads((FIX / "test.json").read_text())


@pytest.fixture(autouse=True)
def no_network(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("тест полез в сеть")
    monkeypatch.setattr(box_nl.urllib.request, "urlopen", boom)
    monkeypatch.setattr(box_nl, "STATE", tmp_path / "bot")
    monkeypatch.setattr(box_nl, "primary", lambda: "gigachat")


def test_наборы_фраз_не_пересекаются_и_заморожены():
    assert len(DEV) == 30 and len(TEST) == 80
    dev = {box_nl.norm(c["text"]) for c in DEV}
    assert not dev & {box_nl.norm(c["text"]) for c in TEST}


def test_тестовые_фразы_не_протекли_в_подсказку():
    prompt = box_nl.norm(box_nl.SYSTEM)
    leaked = [c["text"] for c in TEST if box_nl.norm(c["text"]) in prompt]
    assert not leaked


@pytest.mark.parametrize("case", DEV, ids=[c["text"] for c in DEV])
def test_правила_на_настроечных_фразах(case):
    r = box_nl.rules(case["text"], DISTRICTS)
    assert r["filters"] == case["want"]
    assert bool(r["unknown"]) == bool(case.get("unknown"))


@pytest.mark.parametrize("text, want", [
    ("однушка в Сокольниках", "Сокольники"),
    ("однушка на Соколе", "Сокол"),
    ("двушка в Северном Бутово", "Северное Бутово"),
    ("в Марьиной роще", "Марьина роща"),
    ("в Теплом Стане", "Теплый Стан"),
])
def test_район_по_основе_самое_длинное_совпадение(text, want):
    assert box_nl.find_district(text, DISTRICTS)[0] == want


def test_парк_в_названии_района_не_парк():
    r = box_nl.rules("двушка в Филевском парке", DISTRICTS)
    assert r["filters"] == {"rooms": [2], "district": "Филевский парк"}


def test_сито_отбрасывает_мусор_модели():
    raw = {"price_max": 5000, "area_min": "40; drop table", "rooms": [2, 9],
           "metro_max": True, "pets": "можно", "park": "да", "inside_mkad": 1,
           "district": "Хогвартс", "sql": "delete", "entry_max": 150.0}
    r = box_nl.validate(raw, DISTRICTS)
    assert r["filters"] == {"entry": 150}
    assert any("price" in u for u in r["unknown"])
    assert "Хогвартс" in r["unknown"]


def test_сито_приводит_район_модели_к_справочнику():
    r = box_nl.validate({"district": "район Хамовники", "rooms": [2, 1, 2]}, DISTRICTS)
    assert r["filters"] == {"district": "Хамовники", "rooms": [1, 2]}


def test_молчание_модели_откат_на_правила(monkeypatch):
    def down(*a, **k):
        raise TimeoutError("нет ответа")
    monkeypatch.setattr(box_nl, "gigachat_raw", down)
    r = box_nl.parse("двушка до 90 с котом", DISTRICTS)
    assert r["source"] == "rules"
    assert r["filters"] == {"rooms": [2], "price": 90, "pets": "allowed"}
    assert "TimeoutError" in r["error"]


def test_отказ_ключа_помечается_для_тревоги(monkeypatch):
    def rejected(*a, **k):
        raise box_nl.KeyRejected("OAuth ответил 401")
    monkeypatch.setattr(box_nl, "gigachat_raw", rejected)
    r = box_nl.parse("абракадабра", DISTRICTS)
    assert r["source"] == "none"
    assert r["error"].startswith("key:")


def test_главные_правила_модель_не_зовется(monkeypatch):
    monkeypatch.setattr(box_nl, "primary", lambda: "rules")
    monkeypatch.setattr(box_nl, "gigachat_raw", lambda *a, **k: pytest.fail("позвали модель"))
    assert box_nl.parse("однушка до 70", DISTRICTS)["source"] == "rules"


def test_файл_фильтров_в_формате_кнопок_и_свежесть_сохраняется():
    lines = box_nl.file_lines({"rooms": [1, 2], "price": 90, "park": True, "district": "Арбат"},
                              {"seen": "2", "area": "45"})
    assert lines == ["price=90", "rooms=1,2", "park=1", "district=Арбат", "seen=2"]


def test_лимит_фраз_в_сутки():
    assert all(box_nl.take_quota("7", "guest") for _ in range(20))
    assert not box_nl.take_quota("7", "guest")
    assert box_nl.take_quota("42", "owner")
    assert not box_nl.take_quota("9", "stranger")


def test_применение_пишет_фильтры_и_прежние_рядом(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(box_nl, "districts_from_db", lambda: DISTRICTS)
    monkeypatch.setattr(box_nl, "gigachat_raw",
                        lambda *a, **k: {"rooms": [2], "price_max": 90, "unsupported": ["балкон"]})
    f = tmp_path / "filters.42"
    f.write_text("price=80\nmkad=1\nseen=1\n")
    box_nl.cmd_apply(str(f), "42", "owner", "двушка до 90 с балконом")
    out = capsys.readouterr().out.splitlines()
    assert out == ["ok gigachat", "? балкон"]
    assert f.read_text() == "price=90\nrooms=2\nseen=1\n"
    assert (tmp_path / "filters.42.prev").read_text() == "price=80\nmkad=1\nseen=1\n"


def test_неразобранное_не_трогает_фильтры(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(box_nl, "districts_from_db", lambda: DISTRICTS)
    monkeypatch.setattr(box_nl, "gigachat_raw", lambda *a, **k: {})
    f = tmp_path / "filters.42"
    f.write_text("price=80\n")
    box_nl.cmd_apply(str(f), "42", "owner", "привет")
    assert capsys.readouterr().out.strip() == "none"
    assert f.read_text() == "price=80\n"
    assert not (tmp_path / "filters.42.prev").exists()


def test_бот_отдает_фразу_разбору_а_гость_может_писать_словами():
    src = (ROOT / "scripts" / "box_bot.sh").read_text()
    assert '*) cmd_phrase "$text" ;;' in src
    assert "понимаю только команды" not in src
    gate = src.split("guest_allowed() {")[1].split("}")[0]
    assert "*) return 0 ;;" in gate
