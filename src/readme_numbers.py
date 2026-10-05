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
