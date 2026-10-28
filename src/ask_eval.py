"""Замер выбора функции по вопросу (scripts/box_ask.py).

    python -m src.ask_eval dev     # настройка: оба режима вызова на dev, сколько угодно раз
    python -m src.ask_eval test    # замер: обе модели на test, один раз

Настройка идет только на tests/fixtures/ask/dev.json, на нем же по
MODE_RULE выбирается режим вызова. Тестовые 60 вопросов и правило выбора
модели (MODEL_RULE) ушли в репозиторий коммитом до первого прогона test,
и test прогоняется один раз: повторный прогон перезаписал бы честный
первый замер, поэтому без --again команда отказывается.

Точный вызов значит, что функция и все параметры после проверки кода
совпали с ожидаемыми. Для вопроса не по теме точный ответ это отказ
(функция none).

Нужна сеть до GigaChat и ключ в .env, база не нужна: справочник районов
лежит в tests/fixtures/nl/districts.txt.
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
import box_ask  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "ask"
DISTRICTS = [d for d in (ROOT / "tests" / "fixtures" / "nl" / "districts.txt").read_text().splitlines() if d]
OUT = ROOT / "results" / "ask_eval.json"


def score(cases, model: str, mode: str) -> dict:
    exact = fn_ok = 0
    misses, latency = [], []
    for c in cases:
        t0 = time.monotonic()
        try:
            got = box_ask.call(c["text"], DISTRICTS, model, mode)
        except Exception as e:  # сбой сети считается промахом, а не пропуском
            got = {"fn": "error", "args": {}, "error": f"{type(e).__name__}: {e}"}
        latency.append(time.monotonic() - t0)
        want = c["want"]
        same_fn = got["fn"] == want["fn"]
        ok = same_fn and (want["fn"] == "none" or got["args"] == want["args"])
        fn_ok += same_fn
        exact += ok
        if not ok:
            misses.append({"text": c["text"], "got": {"fn": got["fn"], "args": got["args"]},
                           "want": want, **({"error": got["error"]} if got.get("error") else {})})
    n = len(cases)
    return {"model": model, "mode": mode, "n": n, "exact": exact,
            "exact_pct": round(100 * exact / n, 1), "function_right": fn_ok,
            "latency_median_s": round(statistics.median(latency), 2), "misses": misses}


def dev() -> int:
    cases = json.loads((FIX / "dev.json").read_text())
    res = {m: score(cases, box_ask.MODELS[0], m) for m in ("functions", "json")}
    for m, r in res.items():
        print(f"{m}: точно {r['exact']} из {r['n']}, функция верна {r['function_right']}, "
              f"медиана {r['latency_median_s']} с")
        for x in r["misses"]:
            print("   ", x["text"], "|", json.dumps(x["got"], ensure_ascii=False),
                  "| надо", json.dumps(x["want"], ensure_ascii=False), x.get("error", ""))
    chosen = "functions" if res["functions"]["exact"] >= res["json"]["exact"] else "json"
    print(f"По MODE_RULE режим: {chosen}")
    return 0


def test() -> int:
    if OUT.exists() and "--again" not in sys.argv:
        print(f"{OUT.name} уже есть, тест прогоняется один раз. Перезаписать: --again")
        return 1
    cases = json.loads((FIX / "test.json").read_text())
    lite, pro = (score(cases, m, box_ask.MODE) for m in box_ask.MODELS)
    model = box_ask.MODELS[1] if pro["exact_pct"] - lite["exact_pct"] >= 5 else box_ask.MODELS[0]
    res = {"date": date.today().isoformat(), "n": len(cases), "mode": box_ask.MODE,
           "mode_rule": box_ask.MODE_RULE, "rule": box_ask.MODEL_RULE, "model": model,
           "results": [lite, pro]}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n")
    print(f"{len(cases)} вопросов. {lite['model']} {lite['exact']} точно, "
          f"{pro['model']} {pro['exact']}. Модель бота {model}")
    return 0


if __name__ == "__main__":
    sys.exit({"dev": dev, "test": test}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: (print(__doc__), 1)[1])())
