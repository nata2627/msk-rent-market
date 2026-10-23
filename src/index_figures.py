"""Графики задачи об индексе и прогнозе. Рисуются из файлов results/,
а не из базы: так картинка и числа в репозитории не могут разойтись, и
пересобрать их можно без базы.

Оформление то же, что у src/figures.py: палитра и холст берутся оттуда.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import matplotlib.dates as mdates

from src.figures import INK_2, MUTED, S1, S2, canvas, save, titles

RESULTS = Path(__file__).resolve().parents[1] / "results"


def _month(s: str) -> date:
    y, m = s.split("-")[:2]
    return date(int(y), int(m), 1)


def fig_forecast(history: dict[str, list[tuple[date, float]]]) -> None:
    """Ряд Росстата с 2021 года и прогноз выбранным правилом методом."""
    d = json.loads((RESULTS / "forecast.json").read_text())
    fig, ax = canvas()
    for key, color, label in (("nominal_1", S1, "однокомнатная"),
                              ("nominal_2", S2, "двухкомнатная")):
        hist = [(m, v) for m, v in history[key] if m.year >= 2021]
        ax.plot([m for m, _ in hist], [v / 1000 for _, v in hist], color=color, lw=2)
        fc = d["forecast"][key]
        xs = [hist[-1][0]] + [_month(r["month"]) for r in fc]
        last = hist[-1][1] / 1000
        for lo, hi, alpha in (("lo95", "hi95", 0.12), ("lo80", "hi80", 0.22)):
            ax.fill_between(xs, [last] + [r[lo] / 1000 for r in fc],
                            [last] + [r[hi] / 1000 for r in fc],
                            color=color, alpha=alpha, lw=0)
        ax.plot(xs, [last] + [r["forecast"] / 1000 for r in fc], color=color, lw=2,
                ls=(0, (3, 2)))
        ax.annotate(f"{label}, {fc[-1]['forecast'] / 1000:.1f}",
                    (xs[-1], fc[-1]["forecast"] / 1000), xytext=(6, 0),
                    textcoords="offset points", va="center", color=INK_2, fontsize=9)
    ax.set_ylabel("тыс. руб. в месяц", color=MUTED, fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    chosen = d["decision"]["chosen"]
    names = {"rw": "случайное блуждание", "snaive": "сезонный наивный",
             "ets": "ETS", "sarima": "SARIMA"}
    titles(ax, "Аренда в Москве по Росстату и прогноз на полгода",
           f"Метод по правилу, записанному до расчета: {names[chosen]}. "
           f"Интервалы 80% и 95%")
    save(fig, "forecast.png")


def fig_panel_index() -> None:
    d = json.loads((RESULTS / "panel_index.json").read_text())
    fig, ax = canvas()
    hed = d["hedonic"]
    xs = [date.fromisoformat(p["date"]) for p in hed]
    ax.fill_between(xs, [p["lo95"] for p in hed], [p["hi95"] for p in hed],
                    color=S1, alpha=0.18, lw=0)
    ax.plot(xs, [p["index"] for p in hed], color=S1, lw=2)
    ch = [(date.fromisoformat(hed[0]["date"]), 100.0)] + \
         [(date.fromisoformat(p["date"]), p["index"]) for p in d["chain"]]
    ax.plot([x for x, _ in ch], [v for _, v in ch], color=S2, lw=2)
    ax.annotate("все объявления, гедонический", (xs[-1], hed[-1]["index"]),
                xytext=(6, 4), textcoords="offset points", color=INK_2, fontsize=9)
    ax.annotate("одни и те же объявления, цепной", ch[-1], xytext=(6, 0),
                textcoords="offset points", va="center", color=INK_2, fontsize=9)
    ax.axhline(100, color=MUTED, lw=0.8)
    ax.set_ylabel("индекс, первый день = 100", color=MUTED, fontsize=9)
    # Раз в неделю: по дням подписи слипаются, а пропущенные дни сбора
    # делают шаг автоматической разметки неровным.
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=7))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    titles(ax, "Индекс запрашиваемой ставки по панели, 1-2 комнаты",
           "Голубая полоса: 95% интервал гедонического индекса")
    save(fig, "panel_index.png")
