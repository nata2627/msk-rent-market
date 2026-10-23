"""Разбор /price и карточка ответа. База подменяется функцией: проверяем
и то, что отвечает бот, и то, какой SQL он вообще посмел собрать."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "box_price", Path(__file__).resolve().parents[1] / "scripts" / "box_price.py")
bp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bp)

DISTRICTS = ["Хамовники", "Тверской", "Северное Бутово", "Южное Бутово"]


def test_parse_link_and_bare_id():
    assert bp.parse("https://www.cian.ru/rent/flat/312345678/?utm=x", DISTRICTS) == \
        {"kind": "offer", "offer_id": "312345678"}
    assert bp.parse(" 312345678 ", DISTRICTS)["offer_id"] == "312345678"


def test_parse_words_variants():
    q = bp.parse("2к 45 м2 Хамовники", DISTRICTS)
    assert (q["rooms"], q["area"], q["district"]) == (2, 45, "Хамовники")
    q = bp.parse("двушка? нет, 1-комнатная 38,5 кв м в хамовниках", DISTRICTS)
    assert (q["rooms"], q["area"], q["district"]) == (1, 38, "Хамовники")
    q = bp.parse("студия 25м2 Тверской", DISTRICTS)
    assert (q["rooms"], q["area"], q["district"]) == (0, 25, "Тверской")


def test_colloquial_rooms():
    q = bp.parse("однушка 38 метров тверской", DISTRICTS)
    assert (q["rooms"], q["area"], q["district"]) == (1, 38, "Тверской")
    assert bp.parse("трёшка 70 м2 хамовники", DISTRICTS)["rooms"] == 3


def test_longest_district_wins():
    q = bp.parse("2к 50 м2 Южное Бутово", DISTRICTS)
    assert q["district"] == "Южное Бутово"


class Fake:
    def __init__(self, answers):
        self.answers, self.sql = answers, []

    def __call__(self, sql):
        self.sql.append(sql)
        for key, rows in self.answers.items():
            if key in sql:
                return rows
        return []


GRID = {"distinct district": [[d] for d in DISTRICTS]}


def test_offer_card():
    row = ["2", "Хамовники", "45.0", "85000", "78000", "70000", "87000", "76000",
           "72000", "80000", "12", "2026-10-22"]
    f = Fake({**GRID, "ml.price_estimate": [row]})
    out = bp.answer("https://www.cian.ru/rent/flat/312345678/", f)
    assert "Просят 85 тыс, модель 78 тыс (80%: 70-87)" in out
    assert "Дороже похожих на 9%" in out
    assert "Оценка ЦИАНа 72-80 тыс" in out and "висят 12 дн." in out


def test_offer_not_in_base_is_said_plainly():
    out = bp.answer("312345678", Fake(GRID))
    assert "нет среди тех, что сейчас в выдаче" in out
    assert "не хожу" in out


def test_words_card_and_missing_parts():
    # Ключи проверяются по порядку: запрос списка районов тоже читает
    # ml.price_grid, поэтому он стоит первым.
    f = Fake({**GRID, "from ml.price_grid": [["78000", "70000", "87000", "41", "45"]]})
    out = bp.answer("2к 45 м2 Хамовники", f)
    assert out.startswith("Типичная 2к 45 м2, Хамовники") and "41 объявлений" in out
    assert "Не понял: район" in bp.answer("2к 45 м2 Марс", Fake(GRID))


def test_user_text_never_reaches_sql():
    """Кавычка и точка с запятой из сообщения не попадают в запрос."""
    f = Fake(GRID)
    bp.answer("2к 45 м2 Хамовники'; drop table core.offer; --", f)
    assert all("drop table" not in s for s in f.sql)


def test_empty_gives_help():
    assert "/price 2к 45 м2 Хамовники" in bp.answer("   ", Fake(GRID))
