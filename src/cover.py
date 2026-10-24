"""Обложка README: шесть главных результатов одной картинкой.

    make cover

Рисуется из файлов репозитория (site/data/site.json и results/), без
базы, поэтому пересобирается где угодно и не расходится с витриной.
Исключение одно, оценки надбавки за животных: команда make hedonic
пишет их в журнал, а не в файл, и здесь стоят числа снимка 04.10.2026
из раздела README о животных. Если оценка будет пересчитана, числа
меняются здесь и в methods/pets.md вместе.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from src.figures import AXIS, INK, INK_2, MUTED, S1, S2, SURFACE, save  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site" / "data" / "site.json"
RESULTS = ROOT / "results"

# Снимок 04.10.2026, make hedonic: оценка и 95% интервал, %.
PETS_0410 = [("без контролей", 14.0, 8.0, 20.3),
             ("эффекты района", 7.1, 3.8, 10.5),
             ("все контроли", -0.3, -1.1, 0.6),
             ("соседи по ячейке", -0.8, -1.7, 0.1)]


def style(ax):
    ax.set_facecolor(SURFACE)
    ax.grid(True, axis="y", color=AXIS, linewidth=0.6, alpha=0.7)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
        ax.spines[side].set_linewidth(0.8)
    ax.tick_params(colors=MUTED, labelsize=8.5, length=3, width=0.8)


def head(ax, number, title, note):
    """Крупное число, под ним вывод одной строкой и мелко, как считали."""
    ax.text(0, 1.30, number, transform=ax.transAxes, color=INK, fontsize=22,
            fontweight="bold", va="bottom")
    ax.text(0, 1.17, title, transform=ax.transAxes, color=INK, fontsize=10.5,
            va="bottom")
    ax.text(0, 1.05, note, transform=ax.transAxes, color=MUTED, fontsize=8.5,
            va="bottom")


def pets(ax):
    """Надбавка тает по мере контролей: интервалы, ноль подсвечен."""
    style(ax)
    ax.grid(False)
    ys = list(range(len(PETS_0410)))[::-1]
    for y, (name, est, lo, hi) in zip(ys, PETS_0410):
        color = S1 if lo <= 0 <= hi else MUTED
        ax.plot([lo, hi], [y, y], color=color, lw=2, solid_capstyle="round")
        ax.plot([est], [y], "o", color=color, markersize=8,
                markeredgecolor=SURFACE, markeredgewidth=2)
        ax.text(hi + 0.8, y, f"{est:+.1f}%".replace(".", ","), color=INK_2,
                fontsize=8.5, va="center")
    ax.axvline(0, color=INK_2, lw=0.8)
    ax.set_yticks(ys, [p[0] for p in PETS_0410])
    ax.set_xlim(-4, 25)
    ax.set_xlabel("разница в цене, %", color=MUTED, fontsize=8.5)
    head(ax, "0%", "надбавки за животных нет",
         "гедоническая регрессия, 20 151 объявление, 04.10")


def survival(ax, site):
    style(ax)
    pts = [p for p in site["survival"] if p["age"] <= 60]
    ax.plot([p["age"] for p in pts], [100 * p["surv"] for p in pts], color=S1, lw=2)
    med = next(p["age"] for p in pts if p["surv"] <= 0.5)
    ax.axhline(50, color=AXIS, lw=0.8)
    ax.plot([med], [50], "o", color=S1, markersize=8,
            markeredgecolor=SURFACE, markeredgewidth=2, zorder=5)
    ax.annotate(f"медиана, {med} дней", (med, 50), xytext=(8, 6),
                textcoords="offset points", color=INK_2, fontsize=8.5)
    ax.set_ylim(0, 102)
    ax.set_xlabel("дней от публикации", color=MUTED, fontsize=8.5)
    ax.set_ylabel("доживает, %", color=MUTED, fontsize=8.5)
    head(ax, f"{med} дней", "живет объявление до снятия",
         f"Каплан-Мейер с поздним вхождением, данные на {ru(site['data_date'])}")


def drops(ax, site):
    style(ax)
    ax.grid(True, axis="x", color=AXIS, linewidth=0.6, alpha=0.7)
    ax.grid(False, axis="y")
    rows = [r for r in site["drops"] if r["seller"] != "не указан"][::-1]
    ys = range(len(rows))
    colors = [S2 if r["seller"] == "собственник" else S1 for r in rows]
    ax.barh(list(ys), [r["share_pct"] for r in rows], color=colors, height=0.6)
    for y, r in zip(ys, rows):
        ax.text(r["share_pct"] + 0.6, y, f"{r['share_pct']:.1f}%".replace(".", ","),
                va="center", color=INK_2, fontsize=8.5)
    ax.set_yticks(list(ys), [r["seller"] for r in rows])
    ax.set_xlim(0, 45)
    ax.set_xlabel("снизили цену хотя бы раз, %", color=MUTED, fontsize=8.5)
    agency = next(r for r in rows if r["seller"] == "агентство")["share_pct"]
    owner = next(r for r in rows if r["seller"] == "собственник")["share_pct"]
    gap = f"{agency - owner:+.1f}".replace(".", ",")
    head(ax, f"{gap} п.п.", "агентства снижают цену чаще собственников",
         f"{sum(r['offers'] for r in rows):,} объявлений".replace(",", " ")
         + ", поправки на район и квартиру разрыв не меняют")


def index(ax):
    style(ax)
    d = json.loads((RESULTS / "panel_index.json").read_text())
    hed = d["hedonic"]
    xs = [date.fromisoformat(p["date"]) for p in hed]
    ax.fill_between(xs, [p["lo95"] for p in hed], [p["hi95"] for p in hed],
                    color=S1, alpha=0.18, lw=0)
    ax.plot(xs, [p["index"] for p in hed], color=S1, lw=2)
    ch = [(xs[0], 100.0)] + [(date.fromisoformat(p["date"]), p["index"]) for p in d["chain"]]
    ax.plot([x for x, _ in ch], [v for _, v in ch], color=S2, lw=2)
    ax.annotate("рынок", (xs[-1], hed[-1]["index"]), xytext=(4, 0),
                textcoords="offset points", va="center", color=INK_2, fontsize=8.5)
    ax.annotate("то же\nобъявление", ch[-1], xytext=(4, 0),
                textcoords="offset points", va="center", color=INK_2, fontsize=8.5)
    ax.axhline(100, color=MUTED, lw=0.8)
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=10))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.set_ylabel("первый день = 100", color=MUTED, fontsize=8.5)
    last = f"{hed[-1]['index'] - 100:+.1f}%".replace(".", ",")
    head(ax, last, "рынок за месяц почти стоит",
         "гедонический индекс по панели, полоса 95%")


def forecast(ax, site):
    style(ax)
    hist = [(date.fromisoformat(m + "-01"), v / 1000) for m, v in site["rosstat"]["1"]]
    ax.plot([m for m, _ in hist], [v for _, v in hist], color=S1, lw=2)
    fc = site["forecast"]["series"]["1"]
    xs = [hist[-1][0]] + [date.fromisoformat(r["month"] + "-01") for r in fc]
    last = hist[-1][1]
    for lo, hi, alpha in (("lo95", "hi95", 0.12), ("lo80", "hi80", 0.24)):
        ax.fill_between(xs, [last] + [r[lo] / 1000 for r in fc],
                        [last] + [r[hi] / 1000 for r in fc], color=S1, alpha=alpha, lw=0)
    ax.plot(xs, [last] + [r["forecast"] / 1000 for r in fc], color=S1, lw=2, ls=(0, (3, 2)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.set_ylabel("тыс. руб. в месяц", color=MUTED, fontsize=8.5)
    end = fc[-1]
    head(ax, f"{end['forecast'] / 1000:.0f} тыс. руб.",
         f"однушка в {ru_month(end['month'])}, прогноз",
         f"Росстат, 80%: {end['lo80'] / 1000:.0f}-{end['hi80'] / 1000:.0f} тыс., "
         "правило выбора модели записано до расчета")


def model(ax):
    style(ax)
    pm = json.loads((RESULTS / "price_model.json").read_text())
    names = (("hedonic", "регрессия", MUTED), ("catboost", "CatBoost", S1))
    vals = [pm["models"][k]["test"]["mape_pct"] for k, _, _ in names]
    ax.bar([n for _, n, _ in names], vals, color=[c for _, _, c in names], width=0.55)
    for i, v in enumerate(vals):
        ax.text(i, v + 0.4, f"{v:.1f}%".replace(".", ","), ha="center",
                color=INK_2, fontsize=8.5)
    ax.set_ylim(0, max(vals) * 1.25)
    ax.set_ylabel("MAPE, %", color=MUTED, fontsize=8.5)
    cov = pm["models"]["catboost"]["coverage80"]
    head(ax, f"{vals[1]:.1f}%".replace(".", ","), "ошибка модели справедливой цены",
         f"интервал 80% накрывает {cov:.2f}".replace(".", ",")
         + ", переобучение каждую неделю")


MONTHS_GEN = ("январе феврале марте апреле мае июне июле августе сентябре "
              "октябре ноябре декабре").split()
MONTHS_R = ("января февраля марта апреля мая июня июля августа сентября "
            "октября ноября декабря").split()


def ru(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {MONTHS_R[d.month - 1]}"


def ru_month(ym: str) -> str:
    y, m = ym.split("-")
    return f"{MONTHS_GEN[int(m) - 1]} {y}"


def main() -> int:
    site = json.loads(SITE.read_text())
    fig, axes = plt.subplots(2, 3, figsize=(15, 8.6), dpi=130)
    fig.patch.set_facecolor(SURFACE)
    pets(axes[0][0])
    survival(axes[0][1], site)
    drops(axes[0][2], site)
    index(axes[1][0])
    forecast(axes[1][1], site)
    model(axes[1][2])
    fig.subplots_adjust(left=0.08, right=0.97, top=0.84, bottom=0.07,
                        wspace=0.42, hspace=0.95)
    save(fig, "cover.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
