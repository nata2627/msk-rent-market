#!/usr/bin/env python3
"""Ответ бота на /price: оценка справедливой цены одной карточкой.

    box_price.py "https://www.cian.ru/rent/flat/123456789/"
    box_price.py "2к 45 м2 Хамовники"

Только стандартная библиотека: на боксе бот работает системным python3
без пакетов. Модели в пути ответа нет. Оценки заранее посчитаны
недельным переобучением и лежат в ml.price_estimate и ml.price_grid,
здесь их только читают.

На площадку по запросу не ходим никогда. Второй браузер в том же
профиле ломает сессию обходу, а внеплановые запросы меняют темп,
оговоренный в правилах сбора. Объявления нет в базе, значит так и
отвечаем.

Все, что попадает в SQL, проверено до запроса: id объявления только из
цифр, комнатность и площадь числа, район из списка, полученного из той
же базы. Текст человека в запрос не попадает ни в каком виде.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

PG = os.environ.get("PG_CONTAINER", "rent_pg")


def psql(sql: str) -> list[list[str]]:
    out = subprocess.run(
        ["timeout", "30", "docker", "exec", "-i", PG, "psql", "-qtA", "-F", "|",
         "-U", "rent", "-d", "rent", "-c", sql],
        capture_output=True, text=True)
    return [line.split("|") for line in out.stdout.splitlines() if line.strip()]


def norm(s: str) -> str:
    return s.lower().replace("ё", "е").strip()


ROOMS_RE = re.compile(r"(?:^|\s)(\d)\s*-?\s*(?:к\b|к(?=\s|$)|комн|комнатн)")
AREA_RE = re.compile(r"(\d{2,3}(?:[.,]\d)?)\s*(?:м2|м²|кв\.?\s*м|кв|метр|м\b|м(?=\s|$))")
OFFER_RE = re.compile(r"cian\.ru/rent/flat/(\d{5,12})|^\s*(\d{6,12})\s*$")


def parse(text: str, districts: list[str]) -> dict:
    """Что спросили: конкретное объявление или квартиру словами."""
    m = OFFER_RE.search(text)
    if m:
        return {"kind": "offer", "offer_id": m.group(1) or m.group(2)}
    t = norm(text)
    rooms = 0 if "студ" in t else None
    # Так квартиры называют в разговоре и в самих объявлениях.
    for stem, k in (("однушк", 1), ("однокомн", 1), ("двушк", 2), ("двухкомн", 2),
                    ("трешк", 3), ("трехкомн", 3), ("четырехкомн", 4)):
        if stem in t:
            rooms = k
    m = ROOMS_RE.search(" " + t)
    if m:
        rooms = int(m.group(1))
    m = AREA_RE.search(t)
    area = round(float(m.group(1).replace(",", "."))) if m else None
    # Район ищется как подстрока, начиная с самых длинных названий, чтобы
    # «Северное Бутово» не съедалось «Бутово», если такое когда-нибудь
    # появится в справочнике.
    district = None
    for d in sorted(districts, key=len, reverse=True):
        if norm(d) in t:
            district = d
            break
    if district is None:
        # Хватит начала слова: «хамовн», «тверск».
        words = [w for w in re.findall(r"[а-яa-z-]{4,}", t)]
        for d in sorted(districts, key=len, reverse=True):
            nd = norm(d)
            if any(nd.startswith(w) or w.startswith(nd[:5]) for w in words if len(w) >= 5):
                district = d
                break
    return {"kind": "words", "rooms": rooms, "area": area, "district": district}


def thousands(v) -> str:
    return f"{round(float(v) / 1000):d}"


def rooms_label(r) -> str:
    return "студия" if int(r) == 0 else f"{int(r)}к"


def offer_card(row: list[str]) -> str:
    (rooms, district, area, ask, est, lo, hi, exit_est, cl, ch, days, on) = row
    ask_f, est_f = float(ask), float(est)
    diff = round(100 * (ask_f / est_f - 1))
    if abs(diff) < 5:
        verdict = "в рынке"
    elif diff > 0:
        verdict = f"дороже похожих на {diff}%"
    else:
        verdict = f"дешевле похожих на {-diff}%"
    lines = [f"{rooms_label(rooms)}, {round(float(area))} м2, {district}",
             f"Просят {thousands(ask)} тыс, модель {thousands(est)} тыс "
             f"(80%: {thousands(lo)}-{thousands(hi)})",
             verdict.capitalize()]
    if cl and ch:
        lines.append(f"Оценка ЦИАНа {thousands(cl)}-{thousands(ch)} тыс")
    else:
        lines.append("У ЦИАНа оценки для этого объявления нет")
    if exit_est:
        lines.append(f"Такие с рынка уходят по {thousands(exit_est)} тыс")
    if days:
        lines.append(f"Такие обычно висят {days} дн.")
    lines.append(f"\nМодель от {on[8:10]}.{on[5:7]}. Цены запрашиваемые, "
                 f"уход с рынка не равен сдаче.")
    return "\n".join(lines)


def grid_card(q: dict, row: list[str]) -> str:
    est, lo, hi, n, area = row
    return (f"Типичная {rooms_label(q['rooms'])} {area} м2, {q['district']}\n"
            f"{thousands(est)} тыс в месяц (80%: {thousands(lo)}-{thousands(hi)})\n"
            f"\nПрочие признаки взяты по медиане района, в ячейке {n} объявлений. "
            f"Цены запрашиваемые.")


HELP = ("Пришлите ссылку на объявление или квартиру словами, например\n"
        "/price 2к 45 м2 Хамовники")


def answer(text: str, query=psql) -> str:
    if not text.strip():
        return HELP
    districts = [r[0] for r in query("select distinct district from ml.price_grid")]
    if not districts:
        return "Оценок пока нет: модель еще не переобучалась на этой машине."
    q = parse(text, districts)
    if q["kind"] == "offer":
        rows = query(f"""select rooms, district, total_area, ask, estimate, lo80, hi80,
                            coalesce(exit_estimate::text, ''), coalesce(cian_low::text, ''),
                            coalesce(cian_high::text, ''), coalesce(typical_days::text, ''),
                            estimated_on
                         from ml.price_estimate where offer_id = '{q['offer_id']}'""")
        if rows:
            return offer_card(rows[0])
        return ("Этого объявления нет среди тех, что сейчас в выдаче. "
                "На площадку по запросу я не хожу, оцениваю только собранное.\n\n" + HELP)
    missing = [w for w, k in (("комнатность", "rooms"), ("площадь", "area"),
                              ("район", "district")) if q[k] is None]
    if missing:
        return "Не понял: " + ", ".join(missing) + ".\n\n" + HELP
    rows = query(f"""select estimate, lo80, hi80, comparable_n, area_m2
                     from ml.price_grid
                     where district = '{q['district'].replace("'", "''")}'
                       and rooms = {int(q['rooms'])}
                     order by abs(area_m2 - {int(q['area'])}) limit 1""")
    if not rows:
        return (f"Для {rooms_label(q['rooms'])} в районе {q['district']} меньше пяти "
                f"объявлений, оценивать не берусь.")
    return grid_card(q, rows[0])


if __name__ == "__main__":
    print(answer(" ".join(sys.argv[1:])))
