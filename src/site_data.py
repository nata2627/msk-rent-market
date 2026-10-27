"""Данные публичной витрины: один файл site/data/site.json.

    make site-data

Только агрегаты. Объявления целиком, сырые снимки и выгрузки наружу не
идут: так требуют правила площадки и право изготовителя базы данных
(ст. 1334 ГК РФ). Каждая ячейка от порога проекта и выше: район от
десяти объявлений (как marts.district_level), точка кривой дожития от
пяти объявлений в группе риска, доля снизивших от пятидесяти.

Витрина живет без машины сбора. Файл лежит в репозитории, сайт собирается
из него в GitHub Actions, и если выгрузка остановится, сайт покажет
последнюю дату данных и скажет, сколько дней назад она была.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path

from etl import db
from src import drops as drops_mod
from src import survival as surv
from src.panel_index import COVERED

log = logging.getLogger("site_data")

ROOT = Path(__file__).resolve().parents[1]
# На машине сбора код лежит копией без git, а наружу файлы уходят из
# отдельного клона репозитория. Поэтому оба пути можно переопределить.
OUT = Path(os.environ.get("SITE_OUT", ROOT / "site" / "data" / "site.json"))
RESULTS = Path(os.environ.get("RESULTS_DIR", ROOT / "results"))
MIN_DISTRICT = 10
MIN_RISK = 5
MIN_SHARE = 50

SELLERS = {"homeowner": "собственник", "realtor_based": "агентство",
           "realtor_not_commerce": "частный агент"}


def districts(cur) -> list[dict]:
    cur.execute("""select district, offers, district_ppm::float8, ppm_vs_city::float8
                   from marts.district_level where offers >= %s order by district""",
                (MIN_DISTRICT,))
    return [{"district": d, "offers": n, "ppm": round(p), "vs_city": round(v, 3)}
            for d, n, p, v in cur.fetchall()]


def survival_curve(conn) -> list[dict]:
    rows, cutoff = surv.load(conn, [1, 2])
    curve = surv.km(surv.spans(rows, cutoff), max_age=60)
    return [{"age": a, "at_risk": n, "surv": round(s, 4)}
            for a, n, _, s in curve if n >= MIN_RISK]


def drop_shares(conn) -> list[dict]:
    rows = drops_mod.load(conn, [1, 2])
    groups: dict[str, list[bool]] = {}
    for r in rows:
        groups.setdefault(SELLERS.get(r["seller_type"], "не указан"), []).append(r["cuts"] > 0)
    return sorted([{"seller": k, "offers": len(v), "share_pct": round(100 * sum(v) / len(v), 1)}
                   for k, v in groups.items() if len(v) >= MIN_SHARE],
                  key=lambda x: -x["share_pct"])


def rosstat_history(cur) -> dict[str, list]:
    cur.execute("""select rooms, to_char(period, 'YYYY-MM'), price::float8
                   from core.rosstat_rent where region = 'Москва' and period >= '2021-01-01'
                   order by rooms, period""")
    out: dict[str, list] = {"1": [], "2": []}
    for r, m, p in cur.fetchall():
        out[str(r)].append([m, round(p)])
    return out


def quality(cur) -> dict:
    """Качество сбора по дням. Здесь только счетчики прогонов и корзин,
    объявлений в ячейках нет, поэтому порог ячейки не нужен."""
    cur.execute("""select snapshot_date, is_covered, runs_failed, completeness_pct::float8,
                          underfilled_pct::float8, offers_12
                     from marts.collection_quality order by snapshot_date""")
    days = [{"date": d.isoformat(), "covered": c, "runs_failed": f,
             "completeness_pct": p, "underfilled_pct": u, "offers": n}
            for d, c, f, p, u, n in cur.fetchall()]
    covered = [d for d in days if d["covered"]]
    pct = sorted(d["completeness_pct"] for d in covered if d["completeness_pct"] is not None)
    und = sorted(d["underfilled_pct"] for d in covered if d["underfilled_pct"] is not None)
    # Насколько велик недобор там, где он есть: заявленное площадкой
    # число неточное, и мелкий недобор от него не отличить.
    cur.execute("""select percentile_disc(0.5) within group (order by b.claimed_count - b.collected_count)
                     from raw.collection_run r join raw.bucket b using (run_id)
                    where r.source = 'cian' and r.status = 'ok' and not b.was_split
                      and b.error is null and b.collected_count < b.claimed_count""")
    short = cur.fetchone()[0]
    return {"days": days,
            "days_total": len(days),
            "days_covered": len(covered),
            "missed": [d["date"] for d in days if not d["covered"]],
            "completeness_median": pct[len(pct) // 2] if pct else None,
            "completeness_min": pct[0] if pct else None,
            "underfilled_median": und[len(und) // 2] if und else None,
            "shortfall_median": short}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(f"select max(snapshot_date) from ({COVERED}) c")
        data_date: date = cur.fetchone()[0]
        cur.execute("select count(distinct snapshot_date) from core.offer_observation "
                    "where source = 'cian'")
        days = cur.fetchone()[0]
        payload = {
            "data_date": data_date.isoformat(),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
            "panel_days": days,
            "districts": districts(cur),
            "survival": survival_curve(conn),
            "drops": drop_shares(conn),
            "rosstat": rosstat_history(cur),
            "quality": quality(cur),
        }
    fc = json.loads((RESULTS / "forecast.json").read_text())
    payload["forecast"] = {"chosen": fc["decision"]["chosen"],
                           "last_month": fc["last_month"],
                           "series": {k: fc["forecast"][f"nominal_{k}"] for k in ("1", "2")}}
    ix = json.loads((RESULTS / "panel_index.json").read_text())
    payload["panel_index"] = {
        "hedonic": [[p["date"], p["index"], p["lo95"], p["hi95"]] for p in ix["hedonic"]],
        "chain": [[ix["hedonic"][0]["date"], 100.0]] + [[p["date"], p["index"]] for p in ix["chain"]],
    }
    pm = json.loads((RESULTS / "price_model.json").read_text())
    payload["model_drift"] = [{"week": w["week_start"], "n": w["n"], "mape_pct": w["mape_pct"]}
                              for w in pm["drift"]["weeks"]]
    payload["model"] = {"version": pm["version"],
                        "mape_pct": pm["models"]["catboost"]["test"]["mape_pct"],
                        "coverage80": pm["models"]["catboost"]["coverage80"],
                        "threshold_pct": pm["drift"]["threshold_pct"]}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    log.info("%s: данные на %s, районов %d, точек дожития %d",
             OUT, data_date, len(payload["districts"]), len(payload["survival"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
