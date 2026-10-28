#!/usr/bin/env python3
"""Вопрос о рынке словами: «сколько двушек в Хамовниках подешевело за
неделю» превращается в один из семи заранее написанных запросов к
витринам.

    box_ask.py answer <чат> <owner|guest> "<вопрос>"   # ответ для бота
    box_ask.py call "<вопрос>"                         # выбор функции, без базы

Модель выбирает функцию и ее параметры, и больше ничего. SQL пишет не
она: запросы лежат в этом файле, параметры проходят белый список
(функция из списка, комнатность 1-5, дни 1-30, район только из
справочника базы), и в запрос попадают уже проверенные значения. Ответ
человеку собирает код из строки результата, поэтому каждое число в нем
взято из базы, а не из текста модели.

Если модель не ответила за 10 секунд или выбрала то, чего нет, бот
говорит, что умеет, а не выдумывает ответ.

Какой режим вызова и какая модель в боте, решено правилами ниже
(MODE_RULE, MODEL_RULE) до замера; замер делает src/ask_eval.py.

Только стандартная библиотека, как весь бот на Mac mini. Ключ, токен,
сертификат и суточный лимит фраз общие с разбором фильтров (box_nl.py):
пакет токенов GigaChat один на всех.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import box_nl  # noqa: E402

REPO = box_nl.REPO
DEADLINE = 10.0

# Режим вызова выбирается на настроечных вопросах (dev), это настройка,
# а не замер: родной вызов функций GigaChat (functions) против списка
# функций в подсказке и ответа одним JSON. Выигрывает режим с большим
# числом точных вызовов на dev, при равенстве functions.
# Настройка 28.10.2026 на GigaChat-2, три прогона с правками подсказки:
# functions 24, 27, 25 из 30, json 28, 28, 29. Родной вызов то не вызывал
# функцию вовсе, то терял район и срок; json ошибался только лишней
# комнатностью, которую убрали примеры в подсказке.
MODE_RULE = "functions if exact_dev(functions) >= exact_dev(json) else json"
MODE = "json"

# Правило выбора модели, записанное и запушенное до прогона тестовых
# вопросов. Тест прогоняется один раз на обеих моделях. Модель бота
# GigaChat-2-Pro, если у нее точных вызовов на тесте больше, чем у
# GigaChat-2, не меньше чем на 5 процентных пунктов; иначе GigaChat-2.
# Порог нужен потому, что Pro отвечает медленнее и тратит более дорогой
# пакет, и разницу в один-два вопроса из шестидесяти нельзя отличить от
# шума.
MODEL_RULE = "GigaChat-2-Pro if pct(pro) - pct(lite) >= 5 else GigaChat-2"
MODELS = ("GigaChat-2", "GigaChat-2-Pro")

DAYS = (1, 30)
LIMIT = (1, 10)
ROOMS_DEFAULT = [1, 2]

# Описания функций. Одни и те же для обоих режимов: в functions они идут
# схемой, в json списком в подсказке.
FUNCS = [
    {"name": "market_price",
     "description": "Сколько сейчас стоит аренда: медиана запрашиваемой цены в месяц, "
                    "разброс, цена за метр и число активных объявлений.",
     "args": ("rooms", "district")},
    {"name": "price_drops",
     "description": "Снижения цены за последние дни: сколько объявлений подешевело "
                    "и на сколько процентов в медиане.",
     "args": ("rooms", "district", "days")},
    {"name": "exits",
     "description": "Сколько объявлений ушло с площадки (сняты с публикации) за последние "
                    "дни и по какой медианной цене.",
     "args": ("rooms", "district", "days")},
    {"name": "new_offers",
     "description": "Сколько новых объявлений появилось за последние дни и по какой "
                    "медианной цене.",
     "args": ("rooms", "district", "days")},
    {"name": "district_rank",
     "description": "Районы Москвы, самые дешевые или самые дорогие по медианной цене "
                    "за квадратный метр.",
     "args": ("rooms", "order", "limit")},
    {"name": "pets_share",
     "description": "Какая доля активных объявлений разрешает животных, по договоренности "
                    "или запрещает.",
     "args": ("rooms", "district")},
    {"name": "data_status",
     "description": "Свежесть и полнота данных панели: на какой день данные и как прошел "
                    "последний сбор.",
     "args": ()},
]
FN = {f["name"]: f for f in FUNCS}

ARG_SCHEMA = {
    "rooms": {"type": "array", "items": {"type": "integer"},
              "description": "Число комнат: однушка [1], двушка [2], трешка [3]. "
                             "Не заполняй, если комнатность не названа."},
    "district": {"type": "string",
                 "description": "Район Москвы в именительном падеже. Не заполняй, если район не назван."},
    "days": {"type": "integer",
             "description": "За сколько последних дней: сегодня или вчера 1, неделя 7, "
                            "две недели 14, месяц 30. Не заполняй, если срок не назван."},
    "order": {"type": "string", "enum": ["cheap", "expensive"],
              "description": "cheap самые дешевые, expensive самые дорогие."},
    "limit": {"type": "integer", "description": "Сколько районов показать, если число названо."},
}

SYSTEM = """Ты помогаешь человеку, который ищет квартиру в долгосрочную аренду в Москве, отвечать на вопросы о рынке по данным панели объявлений. Ты не отвечаешь сам: ты выбираешь одну функцию из списка и ее параметры. Заполняй только параметры, которые в вопросе названы явно. Если ни одна функция не подходит (вопрос не о рынке аренды, о покупке, о конкретном объявлении, о погоде), функцию не вызывай.

Однушка, однокомнатная, 1к дают rooms [1]; двушка, 2к дают [2]; трешка [3]; «однушки и двушки» [1, 2]. Слова «квартира», «объявления», «хозяева» без числа комнат комнатность не называют: rooms тогда не заполняй. Район пиши в именительном падеже: «в Хамовниках» дает Хамовники. Аренда офиса, покупка, ипотека это не аренда квартир: функцию не вызывай."""

JSON_TAIL = """

Ответ только одним JSON-объектом без пояснений: {"function": "<имя>", "arguments": {...}}. Если ни одна функция не подходит: {"function": "none"}.

Примеры. «сколько стоят квартиры в Митино» дает {"function": "market_price", "arguments": {"district": "Митино"}}. «сколько трешек подешевело за 3 дня» дает {"function": "price_drops", "arguments": {"rooms": [3], "days": 3}}.

Функции:
"""


def _schema(f: dict) -> dict:
    return {"name": f["name"], "description": f["description"],
            "parameters": {"type": "object",
                           "properties": {a: ARG_SCHEMA[a] for a in f["args"]}}}


def _system(mode: str) -> str:
    if mode == "functions":
        return SYSTEM
    lines = [f"{f['name']}({', '.join(f['args'])}): {f['description']}" for f in FUNCS]
    args = [f"{a}: {s['description']}" for a, s in ARG_SCHEMA.items()]
    return SYSTEM + JSON_TAIL + "\n".join(lines) + "\n\nПараметры:\n" + "\n".join(args)


def model_raw(text: str, model: str, mode: str = MODE) -> dict:
    """Ответ модели как есть: {"function": имя или None, "arguments": {...}}."""
    start = time.monotonic()
    token = box_nl._token(DEADLINE)
    body = {"model": model, "temperature": 0.01,
            "messages": [{"role": "system", "content": _system(mode)},
                         {"role": "user", "content": text}]}
    if mode == "functions":
        body["functions"] = [_schema(f) for f in FUNCS]
        body["function_call"] = "auto"
    req = urllib.request.Request(box_nl.CHAT, data=json.dumps(body).encode(), headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json"})
    left = max(1.0, DEADLINE - (time.monotonic() - start))
    try:
        with urllib.request.urlopen(req, context=box_nl._ctx(), timeout=left) as r:
            ans = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            (box_nl.STATE / "gigachat.token").unlink(missing_ok=True)
        raise
    msg = ans["choices"][0]["message"]
    if mode == "functions":
        call = msg.get("function_call") or {}
        a = call.get("arguments") or {}
        if isinstance(a, str):
            a = json.loads(a or "{}")
        return {"function": call.get("name"), "arguments": a if isinstance(a, dict) else {}}
    m = re.search(r"\{.*\}", msg.get("content") or "", re.S)
    raw = json.loads(m.group(0)) if m else {}
    if not isinstance(raw, dict):
        return {"function": None, "arguments": {}}
    a = raw.get("arguments")
    return {"function": raw.get("function"), "arguments": a if isinstance(a, dict) else {}}


def _int(v, lo: int, hi: int) -> int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v):
        return None
    return int(v) if lo <= int(v) <= hi else None


def validate(raw: dict, districts: list[str]) -> dict:
    """Белый список. Возвращает {"fn": имя или "none", "args": {...},
    "rejected": [...]}: недопустимый параметр не подменяется молча, а
    попадает в rejected, и бот о нем скажет."""
    name = raw.get("function")
    if name not in FN:
        return {"fn": "none", "args": {}, "rejected": []}
    got = raw.get("arguments") or {}
    args: dict = {}
    rejected: list[str] = []
    for a in FN[name]["args"]:
        v = got.get(a)
        if a == "rooms":
            if isinstance(v, int) and not isinstance(v, bool):
                v = [v]
            if isinstance(v, list) and v and all(_int(x, 1, 5) for x in v):
                args["rooms"] = sorted({int(x) for x in v})
            else:
                if v not in (None, []):
                    rejected.append(f"комнаты {v}")
                args["rooms"] = ROOMS_DEFAULT
        elif a == "district":
            if isinstance(v, str) and v.strip():
                d = box_nl.match_district(v, districts)
                if d:
                    args["district"] = d
                else:
                    rejected.append(f"район {v[:40]}")
        elif a == "days":
            n = _int(v, *DAYS) if v is not None else 7
            if n is None:
                rejected.append(f"дней {v}")
                n = 7
            args["days"] = n
        elif a == "order":
            args["order"] = v if v in ("cheap", "expensive") else "cheap"
        elif a == "limit":
            n = _int(v, *LIMIT) if v is not None else 5
            args["limit"] = n or 5
    return {"fn": name, "args": args, "rejected": rejected}


def call(text: str, districts: list[str], model: str, mode: str = MODE) -> dict:
    return validate(model_raw(text, model, mode), districts)


# ---------------------------------------------------------------------
# Запросы
# ---------------------------------------------------------------------
# База отбора одна для всех функций и та же, что у витрин рынка: дубли,
# выбросы цены и комнаты в квартире не считаются. День отсчета это день
# данных витрин (marts.data_freshness), а не календарь: «за неделю»
# значит семь последних дней панели, даже если сбор сегодня еще идет.
# Значения в SQL идут переменными psql (:'district', :days), после
# проверки выше; комнатность проверена как целые 1-5.

BASE = """
with d0 as (select mart_day as d from marts.data_freshness),
s as (
  select * from marts.offer_scored
   where source = 'cian' and price > 0 and rooms_count = any(:'rooms'::int[])
     and (:'district' = '' or district = :'district')
     and not coalesce(is_duplicate, false)
     and not coalesce(is_price_outlier, false)
     and not coalesce(is_room_share, false))
"""

SQL = {
    "market_price": BASE + """
select (select d from d0), count(*),
       percentile_cont(0.5) within group (order by price)::int,
       percentile_cont(0.25) within group (order by price)::int,
       percentile_cont(0.75) within group (order by price)::int,
       (percentile_cont(0.5) within group (order by price / nullif(total_area, 0)))::int
  from s where is_active""",
    "price_drops": BASE + """
select (select d from d0), count(*), count(distinct p.offer_id),
       round(percentile_cont(0.5) within group (order by p.delta_pct)::numeric, 1)
  from marts.price_drops p join s using (source, offer_id)
 where p.direction = 'down' and p.changed_on > (select d from d0) - :days""",
    # Дата ухода как у marts.exit_day: последний день, когда объявление
    # видели. Последние дни недосчитаны: снятие подтверждает только
    # следующий обход.
    "exits": BASE + """
select (select d from d0), count(*), percentile_disc(0.5) within group (order by price)::int
  from s where disappeared_date is not null and last_seen_date > (select d from d0) - :days""",
    "new_offers": BASE + """
select (select d from d0), count(*), percentile_disc(0.5) within group (order by price)::int,
       (select min(snapshot_date) from raw.collection_run where source = 'cian' and status = 'ok')
  from s where first_seen_date > (select d from d0) - :days""",
    "district_rank": BASE + """
, r as (select district, count(*) n,
               percentile_cont(0.5) within group (order by price / total_area) ppm
          from s where is_active and total_area > 0 and district is not null
         group by 1 having count(*) >= 10)
select (select d from d0), district, n, ppm::int from r
 order by case when :'order' = 'cheap' then ppm else -ppm end
 limit :limit""",
    "pets_share": BASE + """
select (select d from d0), count(*),
       count(*) filter (where pets_state = 'allowed'),
       count(*) filter (where pets_state = 'negotiable'),
       count(*) filter (where pets_state = 'forbidden')
  from s where is_active""",
    "data_status": """
select f.mart_day, f.verdict, q.snapshot_date, q.is_covered, q.completeness_pct,
       q.offers_12, q.runs_failed
  from marts.data_freshness f,
       (select * from marts.collection_quality order by snapshot_date desc limit 1) q""",
}


def run_sql(fn: str, args: dict) -> list[list[str]]:
    pg = os.environ.get("PG_CONTAINER", "rent_pg")
    v = {"rooms": "{" + ",".join(map(str, args.get("rooms", ROOMS_DEFAULT))) + "}",
         "district": args.get("district", ""), "days": str(args.get("days", 7)),
         "order": args.get("order", "cheap"), "limit": str(args.get("limit", 5))}
    cmd = ["timeout", "30", "docker", "exec", "-i", pg, "psql", "-qtA", "-F", "\t",
           "-v", "ON_ERROR_STOP=1", "-U", "rent", "-d", "rent"]
    for k, val in v.items():
        cmd += ["-v", f"{k}={val}"]
    out = subprocess.run(cmd, input=SQL[fn], capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError(out.stderr.strip()[:200])
    return [line.split("\t") for line in out.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------
# Ответ человеку
# ---------------------------------------------------------------------

def thousands(n) -> str:
    return f"{int(float(n)):,}".replace(",", " ")


def ddmm(iso: str) -> str:
    return f"{iso[8:10]}.{iso[5:7]}"


def scope(args: dict) -> str:
    r = args.get("rooms", ROOMS_DEFAULT)
    rooms = {(1,): "однушки", (2,): "двушки", (3,): "трешки", (1, 2): "однушки и двушки"}.get(
        tuple(r), f"{','.join(map(str, r))}-комнатные")
    return rooms + (f", {args['district']}" if args.get("district") else ", вся Москва")


def days_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} дня"
    return f"{n} дней"


def render(fn: str, args: dict, rows: list[list[str]]) -> str:
    if not rows:
        return "База не вернула ни одной строки."
    r = rows[0]
    if fn == "market_price":
        d, n, med, p25, p75, ppm = r
        if int(n) == 0:
            return f"Активных объявлений нет ({scope(args)})."
        return (f"Цена аренды на {ddmm(d)}, {scope(args)}\n\n"
                f"Медиана {thousands(med)} руб./мес, половина объявлений от {thousands(p25)} "
                f"до {thousands(p75)}\nЗа метр {thousands(ppm)} руб./мес\n"
                f"Активных объявлений {thousands(n)}"
                + ("\n\nМеньше 10 объявлений, медиана неустойчива." if int(n) < 10 else ""))
    if fn == "price_drops":
        d, events, offers, pct = r
        head = f"Снижения цены за {days_word(args['days'])} до {ddmm(d)}, {scope(args)}\n\n"
        if int(events) == 0:
            return head + "Снижений не было."
        return (head + f"Подешевело объявлений {thousands(offers)}, снижений {thousands(events)}\n"
                f"Медианное снижение {pct.replace('.', ',').lstrip('-')}%")
    if fn == "exits":
        d, n, med = r
        return (f"Ушли с площадки за {days_word(args['days'])} до {ddmm(d)}, {scope(args)}\n\n"
                f"Объявлений {thousands(n)}" + (f", медиана цены {thousands(med)} руб./мес" if med else "")
                + "\n\nУход это снятие с публикации, а не обязательно сдача. Последние два-три "
                  "дня недосчитаны: снятие подтверждает следующий обход.")
    if fn == "new_offers":
        d, n, med, start = r
        note = ""
        if start and args["days"] >= 1 and _days_between(start, d) < args["days"]:
            note = f"\n\nПанель собирается с {ddmm(start)}, раньше новых не отличить от старых."
        return (f"Новые объявления за {days_word(args['days'])} до {ddmm(d)}, {scope(args)}\n\n"
                f"Появилось {thousands(n)}" + (f", медиана цены {thousands(med)} руб./мес" if med else "")
                + note)
    if fn == "district_rank":
        word = "дешевые" if args["order"] == "cheap" else "дорогие"
        rooms = scope(args).split(",")[0]
        lines = [f"{i}. {x[1]}: {thousands(x[3])} руб./м², объявлений {x[2]}"
                 for i, x in enumerate(rows, 1)]
        return (f"Самые {word} районы по цене за метр на {ddmm(r[0])}, {rooms}\n\n"
                + "\n".join(lines) + "\n\nРайон в списке, если в нем от 10 активных объявлений.")
    if fn == "pets_share":
        d, n, ok, neg, no = r
        n = int(n)
        if n == 0:
            return f"Активных объявлений нет ({scope(args)})."
        p = lambda x: f"{100 * int(x) / n:.0f}%"
        rest = n - int(ok) - int(neg) - int(no)
        return (f"Животные на {ddmm(d)}, {scope(args)}, объявлений {thousands(n)}\n\n"
                f"Можно {p(ok)}\nПо договоренности {p(neg)}\nНельзя {p(no)}\n"
                f"Не указано {p(rest)}")
    if fn == "data_status":
        mart, label, qd, covered, comp, offers, failed = r
        return (f"Данные витрин {label}\n\nПоследний день сбора {ddmm(qd)}: "
                + ("закрыт успешным прогоном" if covered == "t" else "успешного прогона нет")
                + (f", забрано {comp.replace('.', ',')}% заявленного" if comp else "")
                + (f", однушек и двушек {thousands(offers)}" if offers else "")
                + (f"\nУпавших прогонов за день {failed}" if failed and failed != "0" else ""))
    return ""


def _days_between(a: str, b: str) -> int:
    from datetime import date
    return (date.fromisoformat(b) - date.fromisoformat(a)).days + 1


def bot_model() -> str:
    """Модель бота. Пишет src/ask_eval.py по правилу MODEL_RULE."""
    try:
        return json.loads((REPO / "results" / "ask_eval.json").read_text())["model"]
    except (OSError, ValueError, KeyError):
        return MODELS[0]


HELP = """Умею отвечать на такие вопросы:
сколько стоит двушка в Хамовниках
сколько однушек подешевело за неделю
сколько объявлений ушло за три дня
сколько новых двушек за неделю
какие районы самые дешевые для однушки
какая доля разрешает животных
свежие ли данные"""


def cmd_answer(chat: str, role: str, text: str) -> int:
    """Печатает готовый ответ. Первая строка служебная для журнала бота:
    ok <функция>, none, limit, error."""
    if not box_nl.take_quota(chat, role):
        print(f"limit\nВопросов на сегодня больше не разбираю, лимит {box_nl.LIMITS.get(role, 0)} в сутки.")
        return 0
    try:
        res = call(text[:300], box_nl.districts_from_db(), bot_model())
    except box_nl.KeyRejected as e:
        box_nl.alarm(str(e))
        print("error\nМодель сейчас недоступна, попробуйте позже.")
        return 0
    except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
        print(f"error {type(e).__name__}\nМодель не ответила вовремя, попробуйте еще раз.")
        return 0
    if res["fn"] == "none":
        print("none\nНе нашел, каким запросом к данным на это ответить.\n\n" + HELP)
        return 0
    try:
        body = render(res["fn"], res["args"], run_sql(res["fn"], res["args"]))
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        print(f"error sql\nБаза не ответила: {str(e)[:100]}")
        return 0
    rej = f"\n\nНе учел: {', '.join(res['rejected'])}" if res["rejected"] else ""
    print(f"ok {res['fn']}\n{body}{rej}")
    return 0


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "call":
        model = os.environ.get("ASK_MODEL", bot_model())
        print(json.dumps(call(sys.argv[2], box_nl.districts_from_db(), model), ensure_ascii=False))
        return 0
    if len(sys.argv) == 5 and sys.argv[1] == "answer":
        return cmd_answer(*sys.argv[2:5])
    print(__doc__.split("\n\n")[1])
    return 1


if __name__ == "__main__":
    sys.exit(main())
