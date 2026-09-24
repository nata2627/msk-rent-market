"""Насколько разбор описаний угадывает животных — проверка по эталону.

    python -m etl.pets_calibrate data/raw/offers.xlsx

Зачем. В JSON выдачи ЦИАНа поля про животных нет вовсе, поэтому при обычном
сборе состояние приходится вычитывать из текста описания. Выгрузка в Excel
содержит анкетную галочку «Можно с животными» — это эталон, по которому
видно, насколько текстовое правило вообще работает.

Как читать результат. Галочка — это НЕ полная истина. Её отсутствие означает
«не отметили», а не «животных нельзя»: анкету могли просто не заполнить.
Поэтому:

* когда правило говорит «разрешено», а галочки нет — это не обязательно
  ошибка правила, возможно автор описал условия словами, но анкету не тронул;
* а вот когда галочка стоит, а правило молчит — это честный пропуск правила,
  и полнота считается именно по таким случаям.

Выводы верны для той выборки, из которой сделана выгрузка, и только.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from etl.parse import pets_from_text
from etl.xlsx_import import column, read_rows


def calibrate(rows: list[dict]) -> dict:
    table = Counter()
    examples: dict[str, list] = {"пропуск": [], "правило нашло без галочки": []}

    for row in rows:
        cell = (str(column(row, "Можно с детьми/животными") or "")).lower()
        checked = "можно с животными" in cell
        description = column(row, "Описание")
        verdict = pets_from_text(description) or "молчит"
        table[(checked, verdict)] += 1

        if checked and verdict == "молчит" and len(examples["пропуск"]) < 3:
            examples["пропуск"].append(str(description)[:150])
        if not checked and verdict == "allowed" \
                and len(examples["правило нашло без галочки"]) < 3:
            examples["правило нашло без галочки"].append(str(description)[:150])

    return {"table": table, "examples": examples, "total": len(rows)}


def report(result: dict) -> None:
    table, total = result["table"], result["total"]
    verdicts = ["allowed", "negotiable", "forbidden", "молчит"]

    print(f"Объявлений в выгрузке: {total}\n")
    print("Строки — галочка площадки, столбцы — вердикт правила по тексту:\n")
    print(f"{'':<22}" + "".join(f"{v:>13}" for v in verdicts) + f"{'всего':>9}")
    for checked, label in ((True, "галочка стоит"), (False, "галочки нет")):
        row = [table[(checked, v)] for v in verdicts]
        print(f"{label:<22}" + "".join(f"{n:>13}" for n in row) + f"{sum(row):>9}")

    checked_total = sum(table[(True, v)] for v in verdicts)
    missed = table[(True, "молчит")]
    found = checked_total - missed

    print()
    if checked_total:
        print(f"ПОЛНОТА правила. Из {checked_total} объявлений с галочкой текст "
              f"даёт хоть какой-то вердикт у {found} — это "
              f"{100 * found / checked_total:.0f}%.")
        print(f"Молчит у {missed} ({100 * missed / checked_total:.0f}%): "
              f"в описании про животных просто не написано.")

    strict = table[(True, "allowed")]
    if found:
        print(f"Из тех, где вердикт есть, «разрешено» правило ставит "
              f"{strict} раз, «по договорённости» — {table[(True, 'negotiable')]}, "
              f"а «запрещено» — {table[(True, 'forbidden')]}.")
        if table[(True, "forbidden")]:
            print("  Последнее — настоящие ошибки правила: галочка стоит, "
                  "а текст прочитан как запрет. Их и надо разбирать.")

    no_check = sum(table[(False, v)] for v in verdicts)
    if no_check:
        print(f"\nБез галочки: {no_check} объявлений. Правило находит "
              f"разрешение у {table[(False, 'allowed')]}, "
              f"договорённость у {table[(False, 'negotiable')]}, "
              f"запрет у {table[(False, 'forbidden')]}.")
        print("  Это не ошибки: галочку могли не поставить, а условия "
              "описать словами. Именно ради таких случаев текст и разбирается.")

    print("\nВЫВОД ДЛЯ СБОРА.")
    if checked_total and missed / checked_total > 0.4:
        print("  Текст молчит слишком часто, чтобы заменить анкету. Значит "
              "второй проход с фильтром площадки в сборщике обязателен, а не\n"
              "  опционален: без него больше трети объявлений с разрешёнными\n"
              "  животными останутся помеченными как «не указано».")
    else:
        print("  Текстовое правило покрывает большую часть случаев и годится\n"
              "  как запасной вариант.")

    for title, items in result["examples"].items():
        if items:
            print(f"\nПримеры «{title}»:")
            for t in items:
                print("   -", t.replace("\n", " ")[:140])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Проверка правила про животных")
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    report(calibrate(read_rows(args.path)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
