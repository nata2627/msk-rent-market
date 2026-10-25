"""Замер разбора фраз на 80 замороженных тестовых фразах.

Разбор настраивался только на tests/fixtures/nl/dev.json. Тестовый набор
tests/fixtures/nl/test.json прогоняется этой командой один раз, результат
ложится в results/nl_eval.json, и по правилу PRIMARY_RULE из
scripts/box_nl.py там же записано, какой разбор в боте главный. Повторный
прогон перезаписал бы честный первый замер, поэтому без --again команда
отказывается.

Точный разбор значит, что фильтры совпали с ожидаемыми целиком. Отдельно
считается, заметил ли разбор пожелание, для которого фильтра нет
(ремонт, балкон, студия): о нем бот обязан сказать, а не молча
пропустить.

Запуск: `make nl-eval`. Нужна сеть до GigaChat и ключ в .env, база не
нужна: справочник районов лежит в tests/fixtures/nl/districts.txt.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import box_nl  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "nl"
OUT = ROOT / "results" / "nl_eval.json"


def score(cases, parse) -> dict:
    exact = flagged = 0
    fields: dict[str, int] = {}
    misses, latency = [], []
    for c in cases:
        t0 = time.monotonic()
        try:
            r = parse(c["text"])
        except Exception as e:  # сбой сети считается промахом, а не пропуском
            r = {"filters": {}, "unknown": [], "error": f"{type(e).__name__}: {e}"}
        latency.append(time.monotonic() - t0)
        got, want = r["filters"], c["want"]
        exact += got == want
        flagged += bool(r["unknown"]) == bool(c.get("unknown"))
        for k in set(got) | set(want):
            if got.get(k) != want.get(k):
                fields[k] = fields.get(k, 0) + 1
        if got != want:
            misses.append({"text": c["text"], "got": got, "want": want,
                           **({"error": r["error"]} if r.get("error") else {})})
    return {"exact": exact, "unknown_flag": flagged, "field_errors": dict(sorted(fields.items())),
            "latency_median_s": round(statistics.median(latency), 2), "misses": misses}


def main() -> int:
    if OUT.exists() and "--again" not in sys.argv:
        print(f"{OUT.name} уже есть, тест прогоняется один раз. Перезаписать: --again")
        return 1
    districts = [d for d in (FIX / "districts.txt").read_text().splitlines() if d]
    cases = json.loads((FIX / "test.json").read_text())
    rules = score(cases, lambda t: box_nl.rules(t, districts))
    giga = score(cases, lambda t: box_nl.gigachat(t, districts))
    primary = "gigachat" if giga["exact"] >= rules["exact"] else "rules"
    # Как разбирает бот: главный разбор, а если он ничего не нашел или
    # упал, запасной.
    box_nl.primary = lambda: primary
    bot = score(cases, lambda t: box_nl.parse(t, districts))
    res = {"date": date.today().isoformat(), "model": box_nl.MODEL, "n": len(cases),
           "rule": box_nl.PRIMARY_RULE, "primary": primary, "gigachat": giga, "rules": rules,
           "bot": bot}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n")
    print(f"{len(cases)} фраз. GigaChat {giga['exact']} точно, правила {rules['exact']}, бот {bot['exact']}. "
          f"Главный разбор {primary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
