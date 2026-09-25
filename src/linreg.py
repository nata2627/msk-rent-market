"""Линейная регрессия на numpy: оценка, ранг, устойчивые стандартные ошибки.

Почему не готовая библиотека. Во-первых, в зависимостях и так уже есть
numpy, а statsmodels тянет за собой pandas и scipy ради одной формулы.
Во-вторых — и это важнее — вся цена этой работы в том, что именно
считается: какие ошибки, на каком уровне кластеризация, что делать с
вырожденными столбцами. Через чужой вызов это не видно, а здесь видно.

Формулы стандартные:

    b   = (X'X)^-1 X'y
    HC1 = (X'X)^-1 X' diag(u²) X (X'X)^-1 · n/(n-k)
    CR1 = (X'X)^-1 (Σ_g X_g'u_g u_g'X_g) (X'X)^-1 · G/(G-1) · (n-1)/(n-k)

HC1 — поправка Уайта на гетероскедастичность: разброс остатков у дорогих
квартир заметно больше, чем у дешёвых, и обычные ошибки это занижают.
CR1 — кластеризация: объявления внутри района не независимы, общий
районный шок сидит в остатках. Когда в модели есть фиксированные эффекты
района, кластеризовать надо по нему же.
"""
from __future__ import annotations

import numpy as np


class Fit:
    def __init__(self, names, beta, vcov, resid, n, k, r2, y_mean, dropped=()):
        self.names = names
        # Столбцы, выброшенные из-за линейной зависимости или отсутствия
        # разброса. Молчать о них нельзя: пропавший контроль выглядит
        # как учтённый.
        self.dropped = list(dropped)
        self.beta = beta
        self.vcov = vcov
        self.resid = resid
        self.n = n
        self.k = k
        self.r2 = r2
        self.y_mean = y_mean

    @property
    def se(self):
        return np.sqrt(np.clip(np.diag(self.vcov), 0, None))

    def get(self, name: str) -> tuple[float, float]:
        """Коэффициент и его ошибка по имени столбца."""
        i = self.names.index(name)
        return float(self.beta[i]), float(self.se[i])

    def table(self, only: list[str] | None = None) -> str:
        rows = []
        for i, name in enumerate(self.names):
            if only is not None and name not in only:
                continue
            b, se = float(self.beta[i]), float(self.se[i])
            t = b / se if se > 0 else float("nan")
            lo, hi = b - 1.96 * se, b + 1.96 * se
            # Для логарифмической зависимой переменной коэффициент — это
            # примерно процент. При |b| больше 0,1 приближение врёт, поэтому
            # показываем точное exp(b)-1.
            rows.append(f"  {name:<28} {b:>8.4f} {se:>8.4f} {t:>7.1f} "
                        f"[{lo:>7.4f}; {hi:>7.4f}] {100*(np.exp(b)-1):>7.1f}%")
        head = (f"  {'признак':<28} {'коэфф.':>8} {'ошибка':>8} {'t':>7} "
                f"{'95% интервал':>19} {'эффект':>8}")
        return head + "\n" + "\n".join(rows)


def _full_rank_columns(X: np.ndarray, tol: float = 1e-9) -> np.ndarray:
    """Какие столбцы оставить, чтобы матрица стала полного ранга.

    QR без перестановок: столбцы проверяются слева направо, и при линейной
    зависимости выбрасывается более правый. Поэтому порядок столбцов не
    произволен — константа, потом изучаемый признак, потом контроли и
    только затем фиксированные эффекты. Так из зависимой пары уйдёт
    фиктивная переменная района, а не то, ради чего всё считается.
    """
    _, r = np.linalg.qr(X)
    diag = np.abs(np.diag(r))
    if diag.size == 0:
        return np.zeros(X.shape[1], dtype=bool)
    return diag > tol * max(diag.max(), 1.0)


def fit(y: np.ndarray, X: np.ndarray, names: list[str],
        cluster: np.ndarray | None = None) -> Fit:
    keep = _full_rank_columns(X)
    dropped = [n for n, k in zip(names, keep) if not k]
    X = X[:, keep]
    names = [n for n, k in zip(names, keep) if k]

    n, k = X.shape
    xtx_inv = np.linalg.inv(X.T @ X)
    beta = xtx_inv @ (X.T @ y)
    resid = y - X @ beta

    if cluster is None:
        meat = (X * (resid ** 2)[:, None]).T @ X
        scale = n / (n - k)
    else:
        groups = np.unique(cluster)
        meat = np.zeros((k, k))
        for g in groups:
            m = cluster == g
            xg_u = X[m].T @ resid[m]
            meat += np.outer(xg_u, xg_u)
        big = len(groups)
        scale = (big / (big - 1)) * ((n - 1) / (n - k))

    vcov = xtx_inv @ meat @ xtx_inv * scale
    tss = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float((resid ** 2).sum()) / tss if tss > 0 else float("nan")
    return Fit(names, beta, vcov, resid, n, k, r2, float(y.mean()), dropped)
