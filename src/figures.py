"""Графики для README. Считаются из панели, а не рисуются руками.

Почему отдельным модулем, а не кодом внутри анализа. Картинка в README —
это утверждение о данных, и оно должно пересобираться той же командой,
что и числа рядом с ним. Иначе через неделю график и таблица разойдутся,
и заметить это будет нечем.

Оформление подчинено одному правилу: график читают, а не рассматривают.
Отсюда тонкие штрихи, сплошная сетка на тон от фона, подписи только там,
где они несут смысл, и ровно один смысловой акцент на картинку. Цвета
взяты из палитры, проверенной на различимость при дальтонизме: синий,
оранжевый и бирюзовый дают худшую пару ΔE 9.2 при пороге 8.

Запуск: `make figures` или `python -m src.figures`.
"""
from __future__ import annotations

import logging
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from etl import db  # noqa: E402
from src import drops as drops_mod  # noqa: E402
from src import survival as surv  # noqa: E402

log = logging.getLogger("figures")

OUT = Path(__file__).resolve().parents[1] / "figures"

# Палитра. Роли, а не просто цвета: первый слот — то, о чём график,
# второй — то, с чем сравниваем, серый — фон истории.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
AXIS = "#c3c2b7"
S1 = "#2a78d6"   # синий
S2 = "#eb6834"   # оранжевый
S3 = "#1baf7a"   # бирюзовый


def canvas(width=9.0, height=4.6):
    fig, ax = plt.subplots(figsize=(width, height), dpi=140)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    # Сетка сплошная и на тон от фона. Пунктир читается как «порог» или
    # «прогноз», а это всего лишь сетка.
    ax.grid(True, axis="y", color=AXIS, linewidth=0.6, alpha=0.7)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
        ax.spines[side].set_linewidth(0.8)
    ax.tick_params(colors=MUTED, labelsize=9, length=3, width=0.8)
    return fig, ax


def titles(ax, title, subtitle=None):
    ax.set_title(title, color=INK, fontsize=13, loc="left", pad=22 if subtitle else 10)
    if subtitle:
        ax.text(0, 1.02, subtitle, transform=ax.transAxes, color=INK_2,
                fontsize=10, va="bottom")


def save(fig, name):
    OUT.mkdir(exist_ok=True)
    path = OUT / name
    fig.savefig(path, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    log.info("%s", path.relative_to(OUT.parent))


# ---------------------------------------------------------------------
def fig_survival(curve_life, curve_cut) -> None:
    """Две доли на одной оси: доживает и ещё не снижал.

    Обе измеряются в одних единицах, поэтому помещаются на одну шкалу.
    Второй оси здесь быть не может — она превратила бы совпадение
    масштабов в видимость связи.
    """
    fig, ax = canvas()
    xs = range(0, 46)
    life = [100 * s for a, n, d, s in curve_life if a <= 45]
    cut = [100 * s for a, n, d, s in curve_cut if a <= 45]
    ax.plot(xs, life, color=S1, linewidth=2, solid_capstyle="round")
    ax.plot(xs, cut, color=S2, linewidth=2, solid_capstyle="round")

    ax.axhline(50, color=AXIS, linewidth=0.8)
    med_life = surv.median_age(curve_life)
    med_cut = surv.median_age(curve_cut)
    # Подписи уведены в пустой верхний правый угол и привязаны выносками:
    # поставленные вплотную к точкам, они легли прямо на линии.
    for age, color, label, xy in ((med_life, S1, "медиана жизни", (20, 76)),
                                  (med_cut, S2, "медиана до первого снижения", (26, 66))):
        if age is None:
            continue
        ax.plot([age], [50], "o", color=color, markersize=8,
                markeredgecolor=SURFACE, markeredgewidth=2, zorder=5)
        ax.annotate(f"{label} — {age} дней", xy=(age, 50), xytext=xy,
                    color=color, fontsize=9.5,
                    arrowprops=dict(arrowstyle="-", color=color,
                                    linewidth=0.8, alpha=0.6))

    # Подписи концов в одну строку: двухстрочные наезжали друг на друга,
    # потому что к сорок пятому дню кривые сходятся на восемь пунктов.
    ax.text(45.6, cut[-1] + 1.5, "ещё не снижал цену", color=S2,
            fontsize=9.5, va="center")
    ax.text(45.6, life[-1] - 1.5, "доживает в выдаче", color=S1,
            fontsize=9.5, va="center")

    ax.set_xlim(0, 58)
    ax.set_ylim(0, 102)
    ax.set_xlabel("возраст объявления, дней от публикации", color=INK_2, fontsize=10)
    ax.set_ylabel("доля объявлений, %", color=INK_2, fontsize=10)
    titles(ax, "Половина объявлений исчезает за две недели",
           "Каплан-Мейер с поправкой на позднее вхождение, комнатности 1-2")
    save(fig, "survival.png")


def fig_expiry(spans_) -> None:
    """Снятия по точному возрасту. Акцент один — всплеск на 30-31 дне."""
    from collections import Counter
    by_age = Counter(exit_ for _, exit_, e in spans_ if e)
    ages = list(range(18, 43))
    vals = [by_age[a] for a in ages]
    colors = [S2 if a in (30, 31) else MUTED for a in ages]

    fig, ax = canvas()
    ax.bar(ages, vals, width=0.78, color=colors)
    peak = max(by_age[30], by_age[31])
    ax.annotate("срок жизни публикации,\nа не сдача квартиры",
                xy=(30.5, peak), xytext=(33.5, peak * 0.92),
                color=S2, fontsize=10,
                arrowprops=dict(arrowstyle="-", color=S2, linewidth=1))
    # Одна подпись на два столбца: поставленные над каждым, числа
    # слипались в «263 263» и читались как одно четырёхзначное.
    ax.text(30.5, peak + 8, f"{by_age[30]} и {by_age[31]}", ha="center",
            color=S2, fontsize=9.5)
    ax.set_xlabel("возраст объявления в день снятия, дней", color=INK_2, fontsize=10)
    ax.set_ylabel("снятий", color=INK_2, fontsize=10)
    titles(ax, "На тридцатый день объявления снимают втрое чаще",
           "Площадка снимает публикацию по истечении срока; к сдаче квартиры это отношения не имеет")
    save(fig, "expiry.png")


def fig_who_cuts(rows) -> None:
    """Доля снизивших цену по типу продавца. Акцент на собственнике."""
    names = {"realtor_based": "агентство",
             "realtor_not_commerce": "частный риелтор",
             "homeowner": "собственник"}
    buckets: dict[str, list] = {}
    for r in rows:
        if r["seller_type"] in names:
            buckets.setdefault(r["seller_type"], []).append(r)
    data = [(names[k], 100 * sum(1 for x in v if x["cut"]) / len(v), len(v))
            for k, v in buckets.items()]
    data.sort(key=lambda t: t[1])

    fig, ax = canvas(height=3.4)
    ax.grid(False, axis="y")
    ax.grid(True, axis="x", color=AXIS, linewidth=0.6, alpha=0.7)
    ys = range(len(data))
    colors = [S1 if i == 0 else MUTED for i in ys]
    ax.barh(list(ys), [d[1] for d in data], height=0.42, color=colors)
    for i, (name, share, n) in enumerate(data):
        ax.text(share + 0.6, i, f"{share:.1f}%", va="center",
                color=INK if i == 0 else INK_2, fontsize=10)
        ax.text(share + 5.2, i, f"({n} объявлений)", va="center",
                color=MUTED, fontsize=9)
    ax.set_yticks(list(ys), [d[0] for d in data], color=INK_2, fontsize=10)
    ax.set_xlim(0, 48)
    ax.set_xlabel("снизили цену хотя бы раз, %", color=INK_2, fontsize=10)
    titles(ax, "Собственники держат цену, агентства уступают",
           "Только объявления, прожившие в наблюдении не меньше недели")
    save(fig, "who_cuts.png")


def fig_trap(rows) -> None:
    """Одна и та же величина, посчитанная до и после снижения."""
    def shares(value):
        vals = sorted(((value(r), r) for r in rows if value(r) is not None),
                      key=lambda t: t[0])
        size = len(vals) // 5
        out = []
        for i in range(5):
            chunk = vals[i * size: (i + 1) * size if i < 4 else len(vals)]
            out.append(100 * sum(1 for _, r in chunk if r["cut"]) / len(chunk))
        return out

    wrong = shares(lambda r: r["last_to_market"])
    right = shares(lambda r: r["first_to_market"])
    xs = [1, 2, 3, 4, 5]

    fig, ax = canvas(height=4.2)
    ax.plot(xs, wrong, color=S2, linewidth=2, marker="o", markersize=8,
            markeredgecolor=SURFACE, markeredgewidth=2)
    ax.plot(xs, right, color=S1, linewidth=2, marker="o", markersize=8,
            markeredgecolor=SURFACE, markeredgewidth=2)
    ax.text(5.1, wrong[-1], "по последней цене\n(неверно)", color=S2,
            fontsize=9.5, va="center")
    ax.text(5.1, right[-1], "по первой цене\n(верно)", color=S1,
            fontsize=9.5, va="center")
    ax.set_xticks(xs, ["дешевле\nвсех", "2", "3", "4", "дороже\nвсех"],
                  color=INK_2, fontsize=9.5)
    ax.set_ylim(20, 44)
    ax.set_xlim(0.7, 6.6)
    ax.set_xlabel("пятина по цене относительно похожих квартир", color=INK_2, fontsize=10)
    ax.set_ylabel("снизили цену, %", color=INK_2, fontsize=10)
    titles(ax, "Почему цену нельзя мерить после снижения",
           "Один вопрос, два способа счёта: наклон меняет знак и почти исчезает")
    save(fig, "price_trap.png")


# ---------------------------------------------------------------------
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    rooms = [1, 2]
    with db.connect() as conn:
        life_rows, cutoff = surv.load(conn, rooms)
        drop_rows = drops_mod.load(conn, rooms)

    sp = surv.spans(life_rows, cutoff)
    curve_life = surv.km(sp, max_age=60)

    for r in drop_rows:
        r["cut"] = r["cuts"] > 0
        r["days_seen"] = (r["last_seen_date"] - r["first_seen_date"]).days
        r["first_to_market"] = float(r["first_price"]) / float(r["med_price"])
        r["last_to_market"] = float(r["last_price"]) / float(r["med_price"])

    cut_spans = []
    for r in drop_rows:
        entry = (r["first_seen_date"] - r["published"]).days
        end = r["first_cut_date"] or r["last_seen_date"]
        exit_ = (end - r["published"]).days
        if entry < 0 or exit_ < entry:
            continue
        cut_spans.append((entry, exit_, r["first_cut_date"] is not None))
    curve_cut = surv.km(cut_spans, max_age=60)

    long = [r for r in drop_rows if r["days_seen"] >= drops_mod.MIN_DAYS]

    fig_survival(curve_life, curve_cut)
    fig_expiry(sp)
    fig_who_cuts(long)
    fig_trap(long)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
