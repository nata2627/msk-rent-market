"""Тесты модели справедливой цены: разбиение, интервал, признаки.
CatBoost здесь не обучается: его нет в CI, и проверять нужно то, что
написано у нас, а не библиотеку."""
import math
from datetime import date, timedelta

import numpy as np
import pytest

from src import price_model as pm

LAST = date(2026, 10, 22)


def r(offer, first, group=None, **kw):
    base = dict(offer_id=offer, first_date=first, dup_group=group or offer,
                price=60000.0, total_area=45.0, rooms=1, floor=3, floors_total=9,
                build_year=1980, metro_walk_min=10, large_park_distance_m=800,
                inside_mkad=True, is_apartments=False, has_furniture=True,
                has_washer=True, has_dishwasher=False, has_conditioner=False,
                has_lift=True, photos_count=12, seller_type="realtor_based",
                building_material="panel", district="Хамовники")
    base.update(kw)
    return base


def test_split_by_time_and_calibration_window():
    rows = [r("a", LAST - timedelta(days=30)), r("b", LAST - timedelta(days=12)),
            r("c", LAST - timedelta(days=2))]
    assert list(pm.split(rows, LAST)) == ["train", "calib", "test"]


def test_duplicate_group_never_straddles_the_border():
    """Одна квартира у двух агентств: второе объявление появилось в дни
    проверки, но группа родилась раньше, и обе строки идут в обучение."""
    rows = [r("a", LAST - timedelta(days=30), group="g"),
            r("b", LAST - timedelta(days=1), group="g")]
    assert list(pm.split(rows, LAST)) == ["train", "train"]


def test_conformal_quantile_finite_sample():
    resid = np.arange(1, 11) / 100          # 0.01..0.10
    # ceil(11 * 0.8) = 9, девятый по величине модуль
    assert pm.conformal_q(resid, alpha=0.2) == pytest.approx(0.09)
    assert pm.conformal_q(-resid, alpha=0.2) == pytest.approx(0.09)


def test_outlier_rule_matches_mart():
    assert pm.is_outlier(85_000_000, 55)
    assert pm.is_outlier(9000, 30)
    assert pm.is_outlier(60000, 300)        # 200 руб. за метр
    assert not pm.is_outlier(60000, 45)


def test_hedonic_unseen_district_gets_city_average():
    rng = np.random.default_rng(0)
    rows = [r(str(i), LAST, district=d, total_area=float(a))
            for i, (d, a) in enumerate(zip(["А", "Б"] * 100, rng.uniform(30, 80, 200)))]
    y = np.array([math.log(1000 * x["total_area"]) + (0.3 if x["district"] == "Б" else 0)
                  for x in rows])
    m = pm.Hedonic().fit(rows, y)
    new = [r("n", LAST, district="Неизвестный", total_area=50.0)]
    base = [r("a", LAST, district="А", total_area=50.0)]
    assert m.predict(new)[0] == pytest.approx(m.predict(base)[0])


def test_metrics_and_bands():
    m = pm.metrics(np.array([100.0, 200.0]), np.array([110.0, 180.0]))
    assert m["mae_rub"] == 15 and m["mape_pct"] == 10.0
    bands = pm.price_band(np.array([50000, 70000, 90000, 200000]),
                          np.array([63000, 82000, 130000]))
    assert list(bands) == ["до 63 тыс", "63-82 тыс", "82-130 тыс", "от 130 тыс"]


def test_drift_threshold_needs_four_weeks():
    weeks = [{"mape_pct": v} for v in (12.8, 13.3, 13.4)]
    assert pm.drift_threshold(weeks) is None
    t = pm.drift_threshold(weeks + [{"mape_pct": 13.0}])
    assert t == pytest.approx(np.mean([12.8, 13.3, 13.4, 13.0])
                              + 2 * np.std([12.8, 13.3, 13.4, 13.0], ddof=1), abs=0.01)
