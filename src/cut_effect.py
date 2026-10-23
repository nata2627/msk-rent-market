"""Ускоряет ли снижение цены уход объявления с рынка.

    make cut-effect

Оговорка стоит первой, потому что без нее все остальное читается
неправильно.

* **Уход объявления не равен сдаче квартиры.** Объявление снимают, когда
  сдали, когда передумали, когда переложили к другому агенту, и когда у
  публикации вышел срок (5,3% снятий, всплеск на тридцатом дне).
* **Снижение цены не случайно.** Его делает тот, кто видит, что звонков
  нет, или кто решил сдать к сроку. Контроли убирают видимое,
  ненаблюдаемое остается в оценке. Честная формулировка здесь
  «насколько чаще уходят снизившие, чем похожие не снизившие в тот же
  день и в том же возрасте», а не «эффект снижения».
* Цены запрашиваемые. Уход после снижения может значить и сдачу дешевле,
  и снятие без сдачи.

Протокол записан до расчета и запушен отдельным коммитом.

Выборка. Собственный сбор, 1 и 2 комнаты, без дублей и посуточных,
известна дата публикации. Строка на объявление и день успешного сбора
обеих комнатностей. Объявление входит в группу риска с первого дня, в
который его застали (позднее вхождение), возраст считается от
публикации. Последние три дня сбора отрезаны: снятие подтверждается
только через три собранных дня отсутствия.

Событие. Последний день, в который объявление было в выдаче, если оно
потом снято и снятие подтверждено.

1. Дискретная модель риска, линейная вероятностная на уровне
   объявление-день. Слева событие, справа «цена уже снижалась»
   (меняется во времени), фиксированные эффекты район x комнатность и
   календарного дня, фиктивные по неделе возраста, логарифм цены к
   медиане похожих (marts.comparable_median, ячейка от пяти), тип
   продавца. Ошибки кластерные по объявлению. Оценка читается как
   прибавка к дневной вероятности ухода в процентных пунктах и как
   отношение к базовому риску.
2. Сопоставление. Каждому объявлению, впервые снизившему цену в день d
   в возрасте a, подбираются не снижавшие до d объявления той же
   комнатности, той же недели возраста, того же квинтиля цены к
   похожим, бывшие в выдаче в день d. Траектория до снижения у обеих
   групп одна, цену не меняли. Исход: ушло в течение 7 дней после d.
   Разность долей с бутстреп-интервалом по снизившим (1000 повторов).
3. Плацебо. Среди никогда не снижавших каждому объявлению ставится
   ложное «снижение» в случайный день его наблюдения, и модель 1
   оценивается на них. Ожидается ноль: если нет, модель ловит возраст
   или календарь, а не снижение. 200 повторов, доля интервалов,
   накрывших ноль, и среднее.
4. Неоднородность. Риск снятия зависит не только от возраста (README,
   «Что не готово»). Проверка: модель 1 на когорте, которую видели с
   рождения (первая встреча не позже двух дней после публикации), и с
   дополнительными признаками объявления (число фотографий, продавец
   собственник). Если оценка меняется больше чем на половину своего
   интервала, это пишется как неустойчивость.

Чего это не закрывает. Uplift в узком смысле (кому снижение помогает
больше всего, модель условного эффекта) здесь не оценивается: на месяце
панели и с неслучайным снижением такая модель была бы украшением.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import timedelta
from pathlib import Path

import numpy as np

from src.panel_index import COVERED, cluster_ols, demean

log = logging.getLogger("cut_effect")

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
CONFIRM_DAYS = 3          # etl.db.DISAPPEARED_AFTER_DAYS
HORIZON = 7               # исход сопоставления: ушло за неделю
AGE_WEEKS_CAP = 10        # дальше десятой недели объявлений мало

SQL = f"""
with covered as ({COVERED})
select ob.offer_id, ob.snapshot_date, ob.price::float8,
       o.published_at::date, o.first_seen_date, o.last_seen_date,
       o.disappeared_date, o.district, o.rooms_count, ob.seller_type,
       ob.photos_count, cm.med_price::float8
from core.offer_observation ob
join core.offer o using (source, offer_id)
left join core.offer_duplicate d using (source, offer_id)
left join marts.comparable_median cm
       on cm.district = o.district and cm.rooms_count = o.rooms_count
where ob.source = 'cian' and o.rooms_count in (1, 2)
  and not ob.is_daily and ob.price > 0
  and o.published_at is not null and o.district is not null
  and coalesce(d.canonical_id, o.offer_id) = o.offer_id
  and ob.snapshot_date in (select snapshot_date from covered)
  and o.published_at::date <= ob.snapshot_date
order by ob.offer_id, ob.snapshot_date
"""


def build_panel(rows, last_day):
    """Панель объявление-день с событием и признаком «цена снижалась».

    Возвращает словарь столбцов numpy. Отрезано все позже cutoff: уход
    в последние три дня еще не подтвержден, и такие объявления выглядели
    бы живыми."""
    cutoff = last_day - timedelta(days=CONFIRM_DAYS)
    cols = {k: [] for k in ("offer", "day", "age", "y", "cut", "first_cut",
                            "relp", "rooms", "cell", "seller", "photos",
                            "born", "exit7", "obs7")}
    i, n = 0, len(rows)
    while i < n:
        j = i
        while j < n and rows[j][0] == rows[i][0]:
            j += 1
        g = [r for r in rows[i:j] if r[1] <= cutoff]
        i = j
        if not g:
            continue
        (offer, _, _, pub, first_seen, last_seen, gone, district,
         rooms, _, _, med) = g[0]
        left = gone is not None and gone <= cutoff
        cut, prev = False, None
        for k, r in enumerate(g):
            day, price = r[1], r[2]
            first_cut = False
            if prev is not None and price < prev and not cut:
                cut, first_cut = True, True
            prev = price
            cols["offer"].append(offer)
            cols["day"].append(day)
            cols["age"].append((day - pub).days)
            cols["y"].append(1.0 if left and k == len(g) - 1 else 0.0)
            cols["cut"].append(1.0 if cut else 0.0)
            cols["first_cut"].append(first_cut)
            cols["relp"].append(math.log(price / med) if med else np.nan)
            cols["rooms"].append(rooms)
            cols["cell"].append(f"{district}|{rooms}")
            cols["seller"].append(r[9] or "нет данных")
            cols["photos"].append(r[10] if r[10] is not None else -1)
            cols["born"].append((first_seen - pub).days <= 2)
            # Исход сопоставления: ушло в ближайшие HORIZON дней. Видно,
            # только если от d до cutoff не меньше HORIZON дней.
            cols["obs7"].append(day + timedelta(days=HORIZON) <= cutoff)
            cols["exit7"].append(left and last_seen < day + timedelta(days=HORIZON))
    return {k: np.array(v) for k, v in cols.items()}


def design(p, treat, extra=False):
    """Матрица модели 1. Эффекты ячейки район x комнатность поглощаются
    вычитанием средних, день и неделя возраста идут фиктивными."""
    days = sorted(set(p["day"]))
    weeks = np.minimum(p["age"] // 7, AGE_WEEKS_CAP)
    cols, names = [treat], ["снижала цену"]
    for d in days[1:]:
        cols.append((p["day"] == d).astype(float)); names.append(f"день:{d}")
    for w in range(1, AGE_WEEKS_CAP + 1):
        cols.append((weeks == w).astype(float)); names.append(f"неделя:{w}")
    cols.append(p["relp"]); names.append("log цены к похожим")
    for s in sorted(set(p["seller"]) - {"realtor_based"}):
        cols.append((p["seller"] == s).astype(float)); names.append(f"продавец:{s}")
    if extra:
        ph = p["photos"].astype(float)
        cols.append(np.log1p(np.maximum(ph, 0))); names.append("log фотографий")
        cols.append((ph < 0).astype(float)); names.append("фото: нет данных")
    return np.column_stack(cols), names


def hazard(p, treat=None, mask=None, extra=False):
    """Модель 1: прибавка к дневной вероятности ухода, п.п., и интервал."""
    m = ~np.isnan(p["relp"]) if mask is None else (mask & ~np.isnan(p["relp"]))
    q = {k: v[m] for k, v in p.items()}
    t = (q["cut"] if treat is None else treat[m]).astype(float)
    X, names = design(q, t, extra)
    keep = np.abs(np.linalg.qr(demean(X, q["cell"]))[1].diagonal()) > 1e-9
    keep[0] = True
    Xd = demean(X, q["cell"])[:, keep]
    beta, se = cluster_ols(demean(q["y"], q["cell"]), Xd, q["offer"],
                           n_absorbed=len(set(q["cell"])))
    base = float(q["y"][t == 0].mean())
    b, s = float(beta[0]), float(se[0])
    return {"rows": int(m.sum()), "offers": int(len(set(q["offer"]))),
            "events": int(q["y"].sum()), "base_daily_pct": round(100 * base, 3),
            "effect_pp": round(100 * b, 3), "lo95_pp": round(100 * (b - 1.96 * s), 3),
            "hi95_pp": round(100 * (b + 1.96 * s), 3),
            "ratio": round((base + b) / base, 3) if base > 0 else None}


def matching(p, rng, reps=1000):
    """Модель 2: снизившие в день d против похожих не снижавших в тот же день."""
    ok = ~np.isnan(p["relp"])
    qedges = np.quantile(p["relp"][ok], [0.2, 0.4, 0.6, 0.8])
    qbin = np.searchsorted(qedges, np.nan_to_num(p["relp"], nan=0.0))
    weeks = np.minimum(p["age"] // 7, AGE_WEEKS_CAP)
    key = list(zip(p["day"], p["rooms"], weeks, qbin))
    pool = {}
    for i in np.where((p["cut"] == 0) & p["obs7"] & ok)[0]:
        pool.setdefault(key[i], []).append(i)
    diffs, n_ctrl = [], 0
    for i in np.where(p["first_cut"] & p["obs7"] & ok)[0]:
        ctrl = pool.get(key[i])
        if not ctrl:
            continue
        n_ctrl += len(ctrl)
        diffs.append(float(p["exit7"][i]) - float(p["exit7"][ctrl].mean()))
    diffs = np.array(diffs)
    boots = [rng.choice(diffs, diffs.size).mean() for _ in range(reps)]
    treated = [i for i in np.where(p["first_cut"] & p["obs7"] & ok)[0] if pool.get(key[i])]
    return {"treated": int(diffs.size), "controls_used": int(n_ctrl),
            "treated_exit7_pct": round(100 * float(p["exit7"][treated].mean()), 2),
            "att_pp": round(100 * float(diffs.mean()), 2),
            "lo95_pp": round(100 * float(np.quantile(boots, 0.025)), 2),
            "hi95_pp": round(100 * float(np.quantile(boots, 0.975)), 2)}


def placebo(p, rng, reps=200):
    """Модель 3: ложное снижение в случайный день у никогда не снижавших."""
    ever = {}
    for o, c in zip(p["offer"], p["cut"]):
        ever[o] = ever.get(o, False) or bool(c)
    never = np.array([not ever[o] for o in p["offer"]])
    idx = {}
    for i in np.where(never)[0]:
        idx.setdefault(p["offer"][i], []).append(i)
    effects, covers = [], 0
    for _ in range(reps):
        fake = np.zeros(len(p["y"]))
        for rows_ in idx.values():
            start = rng.integers(0, len(rows_))
            fake[rows_[start:]] = 1.0
        r = hazard(p, treat=fake, mask=never)
        effects.append(r["effect_pp"])
        covers += r["lo95_pp"] <= 0 <= r["hi95_pp"]
    return {"reps": reps, "mean_effect_pp": round(float(np.mean(effects)), 3),
            "sd_effect_pp": round(float(np.std(effects)), 3),
            "share_ci_covering_zero": round(covers / reps, 3)}


def placebo_age(p, rng, reps=200):
    """Плацебо, исправленное после того, как увидено записанное.

    Записанный вариант (placebo выше) ставит ложное снижение в случайный
    день внутри уже известного отрезка наблюдения. Последний день отрезка
    при этом всегда оказывается «после снижения», а событие стоит именно
    на нем, и модель находит огромный «эффект» из ничего: выбор дня
    подглядывает в будущее. Настоящее снижение о дне ухода не знает.

    Здесь ложный возраст снижения тянется из распределения настоящих
    возрастов первого снижения и от судьбы объявления не зависит. Ушло
    раньше этого возраста, значит так и не «снизило», как и в жизни."""
    ever = {}
    for o, c in zip(p["offer"], p["cut"]):
        ever[o] = ever.get(o, False) or bool(c)
    never = np.array([not ever[o] for o in p["offer"]])
    cut_ages = p["age"][p["first_cut"]]
    offers = np.array(sorted({o for o, n in zip(p["offer"], never) if n}))
    pos = {o: k for k, o in enumerate(offers)}
    which = np.array([pos.get(o, -1) for o in p["offer"]])
    effects, covers = [], 0
    for _ in range(reps):
        fake_age = rng.choice(cut_ages, offers.size)
        fake = np.where(never, p["age"] >= fake_age[np.maximum(which, 0)], 0).astype(float)
        r = hazard(p, treat=fake, mask=never)
        effects.append(r["effect_pp"])
        covers += r["lo95_pp"] <= 0 <= r["hi95_pp"]
    return {"reps": reps, "mean_effect_pp": round(float(np.mean(effects)), 3),
            "sd_effect_pp": round(float(np.std(effects)), 3),
            "share_ci_covering_zero": round(covers / reps, 3)}


def main() -> int:
    from etl import db
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    rng = np.random.default_rng(20261023)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(f"select max(snapshot_date) from ({COVERED}) c")
        last_day = cur.fetchone()[0]
        cur.execute(SQL)
        rows = cur.fetchall()
    p = build_panel(rows, last_day)
    log.info("Панель: %d строк, %d объявлений, %d событий",
             len(p["y"]), len(set(p["offer"])), int(p["y"].sum()))
    main_ = hazard(p)
    log.info("Модель риска: %s", main_)
    born = hazard(p, mask=p["born"])
    extra = hazard(p, extra=True)
    log.info("С рождения: %s; с доп. признаками: %s", born, extra)
    match = matching(p, rng)
    log.info("Сопоставление: %s", match)
    plc = placebo(p, rng)
    log.info("Плацебо записанное: %s", plc)
    plc_age = placebo_age(p, rng)
    log.info("Плацебо исправленное: %s", plc_age)
    # «Половина интервала» из протокола: половина его полной ширины.
    half = (main_["hi95_pp"] - main_["lo95_pp"]) / 2
    payload = {
        "last_day": last_day.isoformat(),
        "cutoff": (last_day - timedelta(days=CONFIRM_DAYS)).isoformat(),
        "hazard": main_,
        "hazard_born_cohort": born,
        "hazard_extra_controls": extra,
        "stable": all(abs(r["effect_pp"] - main_["effect_pp"]) <= half
                      for r in (born, extra)),
        "matching": match,
        "placebo_preregistered": plc,
        "placebo_preregistered_note": "негодно: случайный день внутри известного отрезка "
                                      "всегда ставит день ухода после ложного снижения",
        "placebo_age_amended": plc_age,
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "cut_effect.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    log.info("results/cut_effect.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
