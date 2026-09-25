"""Проверка самой арифметики регрессии на данных с известным ответом.

Смысл этих тестов не в том, что «код запускается». Оценка, которой потом
объясняют рынок, должна быть проверена там, где правильный ответ известен
заранее, — иначе ошибка в формуле выглядит как открытие.
"""
import numpy as np

from src.linreg import fit


def test_recovers_known_coefficients():
    rng = np.random.default_rng(0)
    n = 5000
    x1 = rng.normal(size=n)
    x2 = rng.normal(size=n)
    y = 1.5 + 2.0 * x1 - 0.5 * x2 + rng.normal(scale=0.1, size=n)
    X = np.column_stack([np.ones(n), x1, x2])
    f = fit(y, X, ["const", "x1", "x2"])
    assert abs(f.get("x1")[0] - 2.0) < 0.01
    assert abs(f.get("x2")[0] + 0.5) < 0.01
    assert abs(f.get("const")[0] - 1.5) < 0.01
    assert f.r2 > 0.99


def test_ols_matches_closed_form():
    rng = np.random.default_rng(1)
    X = np.column_stack([np.ones(200), rng.normal(size=200)])
    y = rng.normal(size=200)
    expected = np.linalg.lstsq(X, y, rcond=None)[0]
    assert np.allclose(fit(y, X, ["const", "x"]).beta, expected)


def test_collinear_column_is_dropped_not_the_first_one():
    # Порядок столбцов — не косметика: при зависимости выбрасывается более
    # правый, поэтому изучаемый признак должен стоять слева от контролей.
    rng = np.random.default_rng(2)
    n = 300
    treat = rng.integers(0, 2, size=n).astype(float)
    copy = treat.copy()
    y = 1.0 + 0.3 * treat + rng.normal(scale=0.1, size=n)
    X = np.column_stack([np.ones(n), treat, copy])
    f = fit(y, X, ["const", "treat", "копия"])
    assert "treat" in f.names
    assert "копия" not in f.names
    assert abs(f.get("treat")[0] - 0.3) < 0.05


def test_robust_errors_exceed_naive_under_heteroskedasticity():
    # Разброс остатков растёт вместе с x — ровно как разброс цен растёт
    # вместе с площадью. Обычная ошибка в такой ситуации занижена.
    rng = np.random.default_rng(3)
    n = 4000
    x = rng.uniform(0.5, 3.0, size=n)
    y = 1.0 + 0.5 * x + rng.normal(scale=x, size=n)
    X = np.column_stack([np.ones(n), x])
    f = fit(y, X, ["const", "x"])
    resid_var = float((f.resid ** 2).sum()) / (n - 2)
    naive = np.sqrt(resid_var * np.linalg.inv(X.T @ X)[1, 1])
    assert f.get("x")[1] > naive


def test_cluster_errors_exceed_robust_when_shock_is_group_wide():
    # Общий шок внутри группы: объявления одного района не независимы.
    rng = np.random.default_rng(4)
    groups = np.repeat(np.arange(40), 100)
    shock = rng.normal(scale=1.0, size=40)[groups]
    x = rng.normal(size=groups.size)
    y = 0.4 * x + shock + rng.normal(scale=0.2, size=groups.size)
    X = np.column_stack([np.ones(groups.size), x])
    robust = fit(y, X, ["const", "x"]).get("x")[1]
    clustered = fit(y, X, ["const", "x"], cluster=groups).get("x")[1]
    assert clustered > robust


def test_fixed_effects_absorb_group_means():
    # С фиксированными эффектами оценка должна опираться только на
    # различия внутри групп. Проверяем на данных, где между группами
    # разница огромная, а внутри — известная и маленькая.
    rng = np.random.default_rng(5)
    g = np.repeat(np.arange(20), 200)
    level = (np.arange(20) * 5.0)[g]
    treat = rng.integers(0, 2, size=g.size).astype(float)
    y = level + 0.25 * treat + rng.normal(scale=0.1, size=g.size)
    dummies = np.column_stack([(g == j).astype(float) for j in range(1, 20)])
    X = np.column_stack([np.ones(g.size), treat, dummies])
    names = ["const", "treat"] + [f"g{j}" for j in range(1, 20)]
    f = fit(y, X, names, cluster=g)
    assert abs(f.get("treat")[0] - 0.25) < 0.02
