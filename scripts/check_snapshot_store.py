"""Проверка хранения снимков без повторов на настоящих данных.

    python -m scripts.check_snapshot_store 2026-10-18 2026-10-21

Каждый снимок за период прогоняется через новую логику (etl/snapshot_store),
и если он записался бы ссылкой, карточка восстанавливается и разбирается
тем же parse_offer, что и при сборе. Разбор восстановленной сравнивается с
разбором оригинала по всем полям, кроме ссылок (адрес объявления и фото
несут метки показа, а адрес пишется в core при первой встрече, когда
карточка полная). Ни одного расхождения, иначе хранение ломает данные.

Читает потоком, по одному объявлению: неделя карточек целиком в память
макбука не помещается.
"""
from __future__ import annotations

import json
import sys
from collections import Counter

import psycopg

from etl.config import DB
from etl.parse import parse_offer
from etl.snapshot_store import payload_hash, restore

SKIP = {"url"}


def flatten(parsed: dict) -> dict:
    out = {}
    for part, val in parsed.items():
        if part == "photos":
            out["photos"] = len(val)
        elif isinstance(val, dict):
            for k, v in val.items():
                if k not in SKIP:
                    out[f"{part}.{k}"] = v
        else:
            out[part] = val
    return out


def main(a: str, b: str) -> int:
    rows = refs = mism = 0
    bytes_all = bytes_kept = 0
    fields = Counter()
    with psycopg.connect(**DB) as conn, conn.cursor(name="snap") as cur:
        cur.itersize = 2000
        cur.execute("""select offer_id, snapshot_date, payload from raw.offer_snapshot
                       where source = 'cian' and snapshot_date between %s and %s
                       order by offer_id, snapshot_id""", (a, b))
        last_id, last_full, last_hash = None, None, None
        for offer_id, day, payload in cur:
            if offer_id != last_id:
                last_id, last_full, last_hash = offer_id, None, None
            rows += 1
            size = len(json.dumps(payload, ensure_ascii=False).encode())
            bytes_all += size
            h = payload_hash(payload)
            if last_full is not None and h == last_hash:
                refs += 1
                got = flatten(parse_offer(restore(last_full, payload.get("addedTimestamp")), day))
                want = flatten(parse_offer(payload, day))
                diff = [k for k in want if want[k] != got.get(k)]
                if diff:
                    mism += 1
                    fields.update(diff)
            else:
                bytes_kept += size
                last_full, last_hash = payload, h
    print(f"снимков {rows}, записались бы ссылкой {refs} ({100 * refs / rows:.1f}%)")
    print(f"объем карточек {bytes_all / 1e6:.0f} МБ, осталось бы {bytes_kept / 1e6:.0f} МБ")
    print(f"расхождений разбора {mism}", dict(fields.most_common(10)))
    return 1 if mism else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
