"""Числа README пересчитываются из базы, а не переписываются руками.

Зачем. README — публичный рассказ о проекте, и числа в нём устаревают
молча. На 04.10.2026 проверка нашла семь разошедшихся мест: таблица
источников отставала на девять дней собственного сбора и на шестикратный
рост архива, доля «парк рядом» была 17% при настоящих 44%, а абзац про
сырую разницу по животным утверждал равенство медиан, которого в данных
уже не было. Ни одно из расхождений себя не проявляло: текст читался
гладко и был неверен.

Как устроено. В README стоят невидимые при отрисовке метки

    <!--n:offers-->38 990<!--/n-->

и эта команда подменяет то, что между ними, свежим значением из базы.
Метки HTML-комментарии, поэтому на странице GitHub их не видно, а в
исходнике видно сразу, какое число живое, а какое написано руками.

Если метка в README есть, а факта в FACTS нет — команда падает. Молчаливо
пропущенная метка вернула бы ровно ту болезнь, от которой лечимся.

Запуск: `make readme-numbers`.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path

from etl import db

log = logging.getLogger("readme")

README = Path(__file__).resolve().parents[1] / "README.md"
MARK = re.compile(r"(<!--n:([a-z_0-9]+)-->)(.*?)(<!--/n-->)", re.S)


def thousands(n) -> str:
    """Неразрывный пробел между разрядами: иначе число ломается по строкам."""
    return f"{int(n):,}".replace(",", " ")


MONTHS = ("января февраля марта апреля мая июня июля августа сентября "
          "октября ноября декабря").split()


MONTHS_NOM = ("январь февраль март апрель май июнь июль август сентябрь "
              "октябрь ноябрь декабрь").split()


def ru_date(d) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def ru_period(a, b) -> str:
    if a == b:
        return ru_date(a)
    if a.year == b.year and a.month == b.month:
        return f"{a.day}–{b.day} {MONTHS[a.month - 1]} {a.year}"
    return f"{ru_date(a)} — {ru_date(b)}"


def block(lines: list[str]) -> str:
    """Многострочное значение обрамляется переводами строк.

    Без них таблица начиналась бы на одной строке с HTML-комментарием, и
    GitHub переставал бы видеть в ней таблицу: шапка обязана стоять в
    начале строки. Ошибка из тех, что видно только на отрисованной
    странице, а в исходнике выглядит нормально.
    """
    return "\n" + "\n".join(lines) + "\n"


# ---------------------------------------------------------------------
def collect(conn) -> dict[str, str]:
    def one(sql, params=None):
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def rows(sql, params=None):
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    f: dict[str, str] = {}

    offers, obs, dates, segment = one("""
        select (select count(*) from core.offer where source='cian'),
               (select count(*) from core.offer_observation where source='cian'),
               (select count(distinct snapshot_date) from core.offer_observation
                 where source='cian'),
               (select count(*) from core.offer
                 where source='cian' and rooms_count in (1,2))
    """)
    f["offers"] = thousands(offers)
    f["observations"] = thousands(obs)
    f["dates"] = str(dates)
    f["segment"] = thousands(segment)

    # Таблица источников целиком: строки появляются и исчезают, поэтому
    # подменяется блок, а не отдельные числа в нём.
    names = {"cian": "`cian` — собственный сбор",
             "cian_wayback": "`cian_wayback` — веб-архив",
             "cian_xlsx": "`cian_xlsx` — ручная выгрузка"}
    table = ["| Источник | Объявлений | Наблюдений | Дат | Период |",
             "|---|---:|---:|---:|---|"]
    for src, n_off, n_obs, n_dates, lo, hi in rows("""
        select o.source, count(distinct o.offer_id), count(*),
               count(distinct ob.snapshot_date), min(ob.snapshot_date), max(ob.snapshot_date)
        from core.offer_observation ob join core.offer o using (source, offer_id)
        group by 1 order by 2 desc
    """):
        table.append(f"| {names.get(src, src)} | {thousands(n_off)} | "
                     f"{thousands(n_obs)} | {n_dates} | {ru_period(lo, hi)} |")
    f["sources_table"] = block(table)

    coords, district, park, total = one("""
        select count(*) filter (where lat is not null),
               count(*) filter (where district is not null),
               count(*) filter (where large_park_distance_m is not null),
               count(*)
        from core.offer where source='cian'
    """)
    f["fill_coords"] = thousands(coords)
    f["fill_district"] = thousands(district)
    # Целой фразой, а не числом. Отставших бывает ноль, и тогда «у
    # остальных 0 координаты появились позже» — бессмыслица, которая
    # попадёт в публичный текст и там останется.
    behind = total - park
    f["park_fill_note"] = (
        f"посчитано у всех {thousands(park)}"
        if behind == 0 else
        f"посчитано у {thousands(park)}: у остальных {thousands(behind)} "
        f"координаты появились позже последнего пересчёта, "
        f"он догонит их следующим прогоном")

    (mkad,) = one("select count(*) from marts.offer_segment "
                  "where source='cian' and inside_mkad")
    f["inside_mkad"] = thousands(mkad)

    near_pct, med_park = one("""
        select round(100.0 * count(*) filter (where large_park_distance_m <= 1000)
                     / nullif(count(*) filter (where large_park_distance_m is not null), 0), 0),
               percentile_cont(0.5) within group (order by large_park_distance_m)::int
        from core.offer where source='cian'
    """)
    f["park_near_pct"] = f"{int(near_pct)}%"
    f["park_median_m"] = thousands(med_park)

    extra, share, groups = one("""
        select count(*) filter (where is_duplicate),
               round(100.0 * count(*) filter (where is_duplicate) / count(*), 1),
               (select count(distinct canonical_id) from core.offer_duplicate
                 where source='cian')
        from marts.offer_segment where source='cian'
    """)
    f["dup_extra"] = thousands(extra)
    f["dup_share"] = f"{share}".replace(".", ",") + "%"
    f["dup_groups"] = thousands(groups)

    (good,) = one("select count(*) from marts.offer_segment "
                  "where source='cian' and price_label is not null")
    f["good_price"] = thousands(good)

    for rooms in (1, 2):
        (med,) = one("""
            select percentile_cont(0.5) within group (order by price)::int
            from marts.offer_segment
            where source='cian' and rooms_count = %s
              and not coalesce(is_price_outlier, false)
        """, (rooms,))
        f[f"median_{rooms}"] = thousands(med)

    # Сырые медианы по животным. Раньше в README стояло «совпадают до
    # рубля» — утверждение, верное на снимке 26.09 и неверное сейчас.
    ru = {"allowed": "разрешено", "forbidden": "запрещено",
          "negotiable": "по договорённости", "unknown": "не указано"}
    tbl = ["| состояние | однокомнатные | двухкомнатные |", "|---|---:|---:|"]
    for state in ("allowed", "forbidden", "negotiable", "unknown"):
        cells = []
        for rooms in (1, 2):
            (m,) = one("""
                select percentile_cont(0.5) within group (order by price)::int
                from marts.offer_segment
                where source='cian' and rooms_count = %s and pets_state = %s
                  and not coalesce(is_price_outlier, false)
            """, (rooms, state))
            cells.append(thousands(m) if m else "—")
        tbl.append(f"| {ru[state]} | {cells[0]} | {cells[1]} |")
    f["pets_raw_table"] = block(tbl)

    buckets, = one("""
        select count(*) from raw.bucket b join raw.collection_run r using (run_id)
        where r.status='ok' and r.mode in ('full','narrow') and b.was_split=false
          and b.pets_filter=false and b.error is null and b.claimed_count > 0
    """)
    f["live_buckets"] = thousands(buckets)

    f.update(operations(rows))
    f.update(results_facts())

    # Тесты считаются сбором pytest, а не записываются руками: число
    # меняется почти каждым коммитом.
    out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"],
                         capture_output=True, text=True,
                         cwd=README.parent)
    m = re.search(r"(\d+) tests? collected", out.stdout)
    if m:
        f["tests"] = m.group(1)
    else:
        log.warning("не удалось сосчитать тесты, метка оставлена как есть")
    return f


# ---------------------------------------------------------------------
# Эксплуатация. Причина падения хранится свободным текстом в note, потому
# что пишет её тот, кто упал: исключение площадки, сторож, человек. Порядок
# правил важен: «убит сигналом» случается и посреди таймаутов страницы, и
# тогда настоящая причина — сигнал, а не страница.
REASONS = (
    ("блокировк", "похоже на блокировку, обход остановил себя сам"),
    ("убит|прерван|остановлен по просьбе", "прерван снаружи: сигнал, перерегистрация расписания, переезд"),
    ("закрыт задним числом", "завис без итога, закрыт задним числом"),
    ("предел в", "пробный прогон с пределом запросов"),
    ("собрано ноль", "пустой обход: ни одна корзина не прочиталась"),
    ("Target page, context or browser", "браузер закрылся посреди обхода"),
    ("PageWithoutData|TimeoutError|_cianConfig", "страница не далась после трех попыток, обход продолжен следующим запуском"),
)


def failure_reason(note: str | None) -> str:
    for pattern, label in REASONS:
        if note and re.search(pattern, note):
            return label
    return "прочее"


def day_kind(has_ok: bool, has_data: bool) -> str:
    """Покрытым день считается только по успешному прогону, как в итоге
    дня на боксе: от этого зависят даты снятий. Данные упавших прогонов
    при этом не пропадают, поэтому «собран частично» отделён от
    «потерян» — иначе 17.10 с 16 тысячами объявлений стоял бы рядом с
    09.10, где не было ни одного."""
    if has_ok:
        return "full"
    return "partial" if has_data else "lost"


def short_dates(days) -> str:
    """«9 и 11 октября», «13 октября». Пустой список — тире, а не пустота."""
    days = sorted(days)
    if not days:
        return "—"
    parts = [f"{d.day} {MONTHS[d.month - 1]}" for d in days]
    # Месяц пишется один раз, если все даты в одном месяце.
    if len({d.month for d in days}) == 1:
        parts = [str(d.day) for d in days[:-1]] + [parts[-1]]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " и " + parts[-1]


def operations(rows) -> dict[str, str]:
    """Числа раздела «Эксплуатация». Только собственный сбор: ручная
    выгрузка и веб-архив — разовые загрузки, а не работа машины."""
    f: dict[str, str] = {}
    runs = """
        select * from raw.collection_run
        where source = 'cian' and mode in ('full', 'narrow')
          and coalesce(note, '') not like 'ручная%%'
    """
    days = rows(f"""
        with r as ({runs}),
        d as (select snapshot_date,
                     bool_or(status = 'ok') has_ok,
                     bool_or(status in ('failed', 'aborted')) has_bad
              from r group by 1),
        o as (select distinct snapshot_date from core.offer_observation
              where source = 'cian')
        select g::date, coalesce(d.has_ok, false), coalesce(d.has_bad, false),
               o.snapshot_date is not null
        from generate_series((select min(snapshot_date) from r),
                             (select max(snapshot_date) from r), '1 day') g
        left join d on d.snapshot_date = g
        left join o on o.snapshot_date = g
        order by 1
    """)
    kinds = {day: day_kind(ok, data) for day, ok, _, data in days}
    # Своей фразой, а не ru_period: там длинное тире между датами.
    a, b = days[0][0], days[-1][0]
    f["ops_period"] = (f"с {a.day} {MONTHS[a.month - 1]} по {b.day} "
                       f"{MONTHS[b.month - 1]} {b.year}")
    f["ops_calendar_days"] = str(len(days))
    f["ops_days_full"] = str(sum(k == "full" for k in kinds.values()))
    partial = [d for d, k in kinds.items() if k == "partial"]
    lost = [d for d, k in kinds.items() if k == "lost"]
    f["ops_days_partial"] = str(len(partial))
    f["ops_days_partial_dates"] = short_dates(partial)
    f["ops_days_lost"] = str(len(lost))
    f["ops_days_lost_dates"] = short_dates(lost)
    # Догнан — день, где был сбой, но в итоге есть успешный прогон.
    f["ops_caught_up"] = str(sum(ok and bad for _, ok, bad, _ in days))

    # Идущий прогон не считается ни удачным, ни упавшим: итога у него нет.
    finished = rows(f"""
        select status, note, finished_at - started_at, requests_made
        from ({runs}) r where status <> 'running'
    """)
    ok = [r for r in finished if r[0] == "ok"]
    bad = [r for r in finished if r[0] != "ok"]
    f["ops_runs"] = str(len(finished))
    f["ops_runs_ok"] = str(len(ok))
    f["ops_runs_bad"] = str(len(bad))
    hours = sum((r[2].total_seconds() for r in finished if r[2]), 0) / 3600
    f["ops_hours"] = thousands(round(hours))
    f["ops_requests"] = thousands(sum(r[3] or 0 for r in finished))

    counts: dict[str, int] = {}
    for _, note, _, _ in bad:
        label = failure_reason(note)
        counts[label] = counts.get(label, 0) + 1
    tbl = ["| причина | прогонов |", "|---|---:|"]
    for label, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        tbl.append(f"| {label} | {n} |")
    f["ops_fail_table"] = block(tbl)
    return f


# ---------------------------------------------------------------------
# Индекс и прогноз. Числа берутся из файлов results/, которые пишут
# make index и make forecast, а не из базы: результат модели проверяется
# по репозиторию без базы, и README обязан совпадать именно с файлом.
RESULTS = README.parent / "results"
MODEL_NAMES = {"rw": "случайное блуждание", "snaive": "сезонный наивный",
               "ets": "ETS", "sarima": "SARIMA"}


def pct(v, digits=1, sign=False) -> str:
    s = f"{v:+.{digits}f}" if sign else f"{v:.{digits}f}"
    return s.replace(".", ",").replace("-", "−")


def results_facts() -> dict[str, str]:
    import json
    f: dict[str, str] = {}
    fc_path, ix_path = RESULTS / "forecast.json", RESULTS / "panel_index.json"
    if fc_path.exists():
        d = json.loads(fc_path.read_text())
        dec = d["decision"]
        f["fc_months"] = str(d["months"])
        f["fc_chosen"] = MODEL_NAMES[dec["chosen"]]
        tbl = ["| модель | MAPE, % | покрытие 80%, доля | покрытие 95%, доля |",
               "|---|---:|---:|---:|"]
        for m in ("rw", "snaive", "ets", "sarima"):
            cov95 = sum(d["backtest"][s][m]["cov95"] for s in ("nominal_1", "nominal_2")) / 2
            tbl.append(f"| {MODEL_NAMES[m]} | {pct(dec['mape'][m], 2)} | "
                       f"{pct(dec['cov80'][m], 2)} | {pct(cov95, 2)} |")
        f["fc_backtest_table"] = block(tbl)
        def month_name(ym):
            y, m = ym.split("-")
            return f"{MONTHS_NOM[int(m) - 1]} {y}"
        horizon = d["forecast"]["nominal_1"][-1]["month"]
        rows = [f"| комнат | {month_name(d['last_month'])}, руб. | "
                f"прогноз на {month_name(horizon)}, руб. | 80% | 95% |",
                "|---|---:|---:|---|---|"]
        for r in (1, 2):
            last = d["forecast"][f"nominal_{r}"][-1]
            rows.append(f"| {r} | {thousands(d['last_level'][f'nominal_{r}'])} | "
                        f"{thousands(last['forecast'])} | {thousands(last['lo80'])} - "
                        f"{thousands(last['hi80'])} | {thousands(last['lo95'])} - "
                        f"{thousands(last['hi95'])} |")
        f["fc_forecast_table"] = block(rows)
        dc = d["decomposition"]
        f["fc_season_amp"] = pct((dc["nominal_1"]["amplitude_pct"]
                                  + dc["nominal_2"]["amplitude_pct"]) / 2)
        f["fc_yoy_1"] = pct(dc["nominal_1"]["yoy_last_pct"])
        f["fc_yoy_real_1"] = pct(dc["real_1"]["yoy_last_pct"])
    if ix_path.exists():
        d = json.loads(ix_path.read_text())
        t, det = d["hedonic_total"], d["detectability"]
        f["ix_days"] = str(d["covered_days"])
        f["ix_rows"] = thousands(d["sample"]["rows"])
        f["ix_hed"] = pct(t["change_pct"], 2, sign=True)
        f["ix_hed_ci"] = f"{pct(t['lo95_pct'], 2, sign=True)}; {pct(t['hi95_pct'], 2, sign=True)}"
        f["ix_chain"] = pct(d["chain_total_pct"], 1, sign=True)
        f["ix_mde"] = pct(det["mde_pct"], 2)
        f["ix_rosstat_mom"] = pct(det["rosstat_median_abs_mom_pct"], 2)
        f["ix_sd_day"] = pct(det["sd_day_pct"], 2)
    pm_path = RESULTS / "price_model.json"
    if pm_path.exists():
        d = json.loads(pm_path.read_text())
        names = {"hedonic": "гедоническая регрессия", "catboost": "CatBoost"}
        tbl = ["| модель | MAE, руб. | MAPE, % | медиана APE, % | покрытие 80% | полуширина интервала, % |",
               "|---|---:|---:|---:|---:|---:|"]
        for k in ("hedonic", "catboost"):
            m = d["models"][k]
            tbl.append(f"| {names[k]} | {thousands(m['test']['mae_rub'])} | "
                       f"{pct(m['test']['mape_pct'], 1)} | {pct(m['test']['median_ape_pct'], 1)} | "
                       f"{pct(m['coverage80'], 2)} | {pct(m['interval_halfwidth_pct'], 0)} |")
        f["pm_table"] = block(tbl)
        rooms_lbl = {"0": "студия", "1": "1", "2": "2", "3": "3", "4": "4"}
        cb, hd = d["models"]["catboost"], d["models"]["hedonic"]
        tbl = ["| комнат | объявлений | MAPE регрессии, % | MAPE CatBoost, % | покрытие 80% |",
               "|---|---:|---:|---:|---:|"]
        for k, m in cb["by_rooms"].items():
            tbl.append(f"| {rooms_lbl.get(k, k)} | {thousands(m['n'])} | "
                       f"{pct(hd['by_rooms'][k]['mape_pct'], 1)} | {pct(m['mape_pct'], 1)} | "
                       f"{pct(cb['coverage80_by_rooms'][k], 2)} |")
        f["pm_rooms_table"] = block(tbl)
        f["pm_split"] = (f"{thousands(d['split']['train'])} в обучении, "
                         f"{thousands(d['split']['calib'])} в калибровке, "
                         f"{thousands(d['split']['test'])} в проверке")
        c = d["cian"]
        f["pm_cian_cov"] = pct(c["coverage_share_pct"], 0)
        f["pm_cian_ask_inside"] = pct(c["ask_inside_cian_range_pct"], 1)
        f["pm_cian_ours_inside"] = pct(c["ours_inside_cian_range_pct"], 0)
        f["pm_cian_mid"] = pct(c["ours_vs_cian_mid_median_pct"], 1, sign=True)
        e = d["exit"]
        f["pm_exit_vs_ask"] = pct(e["exit_model_vs_ask_model_median_pct"], 1, sign=True)
        f["pm_exit_cut_share"] = pct(e["share_with_cut_pct"], 0)
        f["pm_exit_n"] = thousands(e["exited_offers"])
        td = d["typical_days"]
        tbl = ["| комнат | дешевле оценки | в рынке | дороже оценки |", "|---|---:|---:|---:|"]
        for k in ("0", "1", "2", "3"):
            cells = [str(td.get(f"{k}|{b}", "—")) for b in ("дешевле", "в рынке", "дороже")]
            tbl.append(f"| {rooms_lbl[k]} | " + " | ".join(cells) + " |")
        f["pm_days_table"] = block(tbl)
        tbl = ["| неделя с | обучение | новых объявлений | MAPE, % |", "|---|---:|---:|---:|"]
        for w in d["drift"]["weeks"]:
            tbl.append(f"| {w['week_start'][8:10]}.{w['week_start'][5:7]} | {thousands(w['train'])} | "
                       f"{thousands(w['n'])} | {pct(w['mape_pct'], 1)} |")
        f["pm_drift_table"] = block(tbl)
        f["pm_estimates"] = thousands(d["serving"]["estimates"])

    ce_path = RESULTS / "cut_effect.json"
    if ce_path.exists():
        d = json.loads(ce_path.read_text())
        h, m = d["hazard"], d["matching"]
        f["ce_rows"] = thousands(h["rows"])
        f["ce_offers"] = thousands(h["offers"])
        f["ce_events"] = thousands(h["events"])
        f["ce_base"] = pct(h["base_daily_pct"], 2)
        f["ce_eff"] = pct(h["effect_pp"], 2, sign=True)
        f["ce_ci"] = f"{pct(h['lo95_pp'], 2, sign=True)}; {pct(h['hi95_pp'], 2, sign=True)}"
        f["ce_ratio"] = pct(h["ratio"], 2)
        rows = ["| оценка | прибавка, п.п. | 95% интервал | база |", "|---|---:|---|---:|"]
        for key, name in (("hazard", "модель риска, все"),
                          ("hazard_born_cohort", "модель риска, видели с рождения"),
                          ("hazard_extra_controls", "модель риска, больше признаков")):
            r = d[key]
            rows.append(f"| {name} | {pct(r['effect_pp'], 2, sign=True)} в день | "
                        f"{pct(r['lo95_pp'], 2, sign=True)}; {pct(r['hi95_pp'], 2, sign=True)} | "
                        f"{pct(r['base_daily_pct'], 2)}% в день |")
        rows.append(f"| сопоставление, уход за 7 дней | {pct(m['att_pp'], 1, sign=True)} | "
                    f"{pct(m['lo95_pp'], 1, sign=True)}; {pct(m['hi95_pp'], 1, sign=True)} | "
                    f"{pct(m['treated_exit7_pct'] - m['att_pp'], 1)}% за 7 дней |")
        f["ce_table"] = block(rows)
        f["ce_treated"] = thousands(m["treated"])
        rel = [100 * (d[k]["ratio"] - 1) for k in
               ("hazard", "hazard_born_cohort", "hazard_extra_controls")]
        f["ce_rel_range"] = f"{round(min(rel))}-{round(max(rel))}"
        pr, pa = d["placebo_preregistered"], d["placebo_age_amended"]
        f["ce_plc_pre"] = pct(pr["mean_effect_pp"], 1, sign=True)
        f["ce_plc_age"] = pct(pa["mean_effect_pp"], 2, sign=True)
        f["ce_plc_age_cover"] = str(round(100 * pa["share_ci_covering_zero"]))
        f["ce_plc_reps"] = str(pa["reps"])
    return f


# ---------------------------------------------------------------------
def apply(text: str, facts: dict[str, str]) -> tuple[str, int, list[str]]:
    changed, missing = 0, []

    def sub(m):
        nonlocal changed
        open_, name, old, close = m.groups()
        if name not in facts:
            missing.append(name)
            return m.group(0)
        new = facts[name]
        if new != old:
            changed += 1
        return f"{open_}{new}{close}"

    return MARK.sub(sub, text), changed, missing


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with db.connect() as conn:
        facts = collect(conn)

    text = README.read_text()
    marks = {m.group(2) for m in MARK.finditer(text)}
    if not marks:
        log.error("В README нет ни одной метки <!--n:...-->. Нечего обновлять.")
        return 1

    new_text, changed, missing = apply(text, facts)
    if missing:
        log.error("Метки есть, а фактов нет: %s", ", ".join(sorted(set(missing))))
        log.error("Либо добавьте факт в FACTS, либо уберите метку — "
                  "молча пропущенная метка это снова разъехавшееся число.")
        return 1

    README.write_text(new_text)
    unused = sorted(set(facts) - marks)
    log.info("Меток в README: %d, обновлено значений: %d", len(marks), changed)
    if unused:
        log.info("Посчитано, но в README не вставлено: %s", ", ".join(unused))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
