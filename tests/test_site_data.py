"""Сторож публичной витрины: в site/data/site.json только агрегаты.

Проверяется сам файл, который уйдет наружу, а не код, который его
собирает: код может поменяться, а опубликовано будет то, что в файле."""
import json
from pathlib import Path

SITE = Path(__file__).resolve().parents[1] / "site" / "data" / "site.json"
FORBIDDEN = ("offer_id", "url", "address", "description", "phone", "seller_id",
             "seller_name", "lat", "lon", "cian.ru")


def keys(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            keys(v, out)
    return out


def test_no_offer_level_fields():
    text = SITE.read_text()
    found = keys(json.loads(text), set())
    for f in FORBIDDEN:
        assert f not in found, f"в витрине поле {f}"
    assert "cian.ru" not in text


def test_cells_respect_thresholds():
    d = json.loads(SITE.read_text())
    assert all(x["offers"] >= 10 for x in d["districts"])
    assert all(x["at_risk"] >= 5 for x in d["survival"])
    assert all(x["offers"] >= 50 for x in d["drops"])


def test_data_date_present():
    d = json.loads(SITE.read_text())
    assert len(d["data_date"]) == 10 and d["panel_days"] > 0
