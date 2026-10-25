#!/usr/bin/env python3
"""Фраза вместо кнопок: «двушка до 90 у метро с котом» превращается в
фильтры подбора.

    box_nl.py apply <файл фильтров> <чат> <owner|guest> "<фраза>"
    box_nl.py parse "<фраза>"            # разбор без записи, для проверки

Разбирает GigaChat, а проверяет код. Модель получает список полей и
обязана вернуть один JSON-объект только с ними, но ее ответу все равно
не верят на слово: каждое поле сверяется с типом и границами, район
сверяется со справочником районов из базы, все лишнее отбрасывается. В
файл фильтров попадает только прошедшее проверку, в том же виде, в каком
его пишут кнопки, и SQL из него собирает тот же filter_where.

Если GigaChat не ответил за 10 секунд, отказал в доступе или вернул
негодное, фраза разбирается правилами из этого же файла. Не разобралось
и правилами, бот показывает экран фильтров с кнопками.

Какой разбор главный, решено до замера (правило ниже, в PRIMARY_RULE), и
замер на 80 замороженных фразах делает src/nl_eval.py.

Лимиты. Владелице 50 фраз в сутки, гостю 20: бесплатный пакет токенов
GigaChat общий на всех, и один гость не должен выбрать его за месяц.

Только стандартная библиотека: на Mac mini бот работает системным
python3 без пакетов. Фраза уходит в Сбер, больше ничего, ни id чата, ни
фильтров. Ключ берется из .env, токен доступа живет 30 минут и
кешируется в data/bot/gigachat.token с правами 600.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATE = REPO / "data" / "bot"
CERT = REPO / "data" / "certs" / "russian_trusted_root_ca.pem"
OAUTH = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
CHAT = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
MODEL = "GigaChat-2"
DEADLINE = 10.0
LIMITS = {"owner": 50, "guest": 20}

# Правило выбора, записанное до первого замера на тестовых фразах.
# Подсказка модели настраивается только на tests/fixtures/nl/dev.json.
# Тестовый набор tests/fixtures/nl/test.json прогоняется после этого один
# раз, и числа публикуются любыми. Главным разбором в боте становится
# GigaChat, если его доля точных разборов на тесте не ниже, чем у правил;
# иначе главными становятся правила, а GigaChat остается запасным для
# фраз, где правила ничего не нашли.
PRIMARY_RULE = "gigachat if exact(gigachat) >= exact(rules) else rules"

# Поля фильтров и их границы. Те же, что у текстовых команд /filter в
# box_bot.sh: другого пути в файл фильтров нет.
BOUNDS = {"price": (10, 999), "area": (10, 300), "metro": (1, 60), "entry": (10, 9999)}
PETS = ("allowed", "negotiable", "forbidden")
NEAR_METRO = 10   # «у метро» без числа минут


def norm(s: str) -> str:
    return s.lower().replace("ё", "е")


def from_env(name: str) -> str:
    v = os.environ.get(name)
    if v:
        return v
    try:
        for line in (REPO / ".env").read_text().splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


# ---------------------------------------------------------------------
# Районы
# ---------------------------------------------------------------------
# Район в разговоре склоняется: «в Хамовниках», «на Соколе», «в Марьиной
# роще». Поэтому каждое слово названия сравнивается основой, без двух
# последних букв, с начала слова фразы. Из нескольких совпадений берется
# самое длинное: «в Сокольниках» задевает и Сокол, и Сокольники, а
# «в Северном Бутово» и Северный, и Северное Бутово.

def district_pattern(name: str) -> re.Pattern:
    words = re.split(r"[\s-]+", norm(name))
    stems = [re.escape(w[:max(3, len(w) - 2)]) + r"\w*" for w in words]
    return re.compile(r"(?<!\w)" + r"[\s-]+".join(stems))


def find_district(text: str, districts: list[str]) -> tuple[str | None, tuple[int, int] | None]:
    t = norm(text)
    best, span = None, None
    for d in districts:
        m = district_pattern(d).search(t)
        if m and (best is None or len(norm(d)) > len(norm(best))):
            best, span = d, m.span()
    return best, span


def match_district(value: str, districts: list[str]) -> str | None:
    """Район из ответа модели: точное совпадение или та же основа."""
    v = norm(value).replace("район", "").strip()
    for d in districts:
        if norm(d) == v:
            return d
    return find_district(v, districts)[0] if v else None


# ---------------------------------------------------------------------
# Разбор правилами
# ---------------------------------------------------------------------

WORD_NUM = {"шестьдесят": 60, "шестидесяти": 60, "семьдесят": 70, "семидесяти": 70,
            "восемьдесят": 80, "восьмидесяти": 80, "девяносто": 90, "девяноста": 90,
            "сто": 100, "ста": 100, "двести": 200, "двухсот": 200}
ROOM_WORDS = (("однуш", 1), ("однух", 1), ("однокомн", 1), ("двуш", 2), ("двух", 2),
              ("треш", 3), ("трех", 3))
UNSUPPORTED = ("студи", "ремонт", "балкон", "без комисс", "посудомо", "новострой",
               "мебел", "лифт", "парковк", "этаж", "посуточ")
PET_WORDS = ("кот", "кош", "собак", "пес", "псом", "животн", "питом", "щен")
ENTRY_CTX = re.compile(r"(заселен|сразу|въезд|залог|первым месяц)")
NUM = re.compile(r"(?<!\w)(\d{1,3}(?:[  ]\d{3})+|\d+)(?:[.,]\d+)?")


def rooms_of(t: str) -> list[int] | None:
    if re.search(r"от\s+(2|двух)\s*(-?х\s*)?комн", t):
        return [2, 3, 4, 5]
    found = set()
    for m in re.finditer(r"(?<!\d)([1-5])\s*(?:-|\s)?\s*(?:к\b|к(?=\s|$|[.,])|комн)", t):
        found.add(int(m.group(1)))
    m = re.search(r"(?<!\d)([1-5])\s*(?:-|или)\s*([1-5])\s*комн", t)
    if m:
        found |= set(range(int(m.group(1)), int(m.group(2)) + 1))
    if re.search(r"\bдве\s+комнат", t):
        found.add(2)
    for w in re.findall(r"\w+", t):
        for stem, k in ROOM_WORDS:
            if w.startswith(stem):
                found.add(k)
    return sorted(found) or None


def rules(text: str, districts: list[str]) -> dict:
    """Разбор без модели. Возвращает {"filters": {...}, "unknown": [...]}."""
    t = norm(text)
    out: dict = {}
    unknown: list[str] = []

    d, span = find_district(t, districts)
    if d:
        out["district"] = d
        t = t[:span[0]] + " " + t[span[1]:]   # «Филевский парк» не про парк

    for w, n in WORD_NUM.items():
        t = re.sub(rf"\b{w}\b", str(n), t)
    r = rooms_of(t)
    if r:
        out["rooms"] = r

    for m in NUM.finditer(t):
        raw = m.group(1)
        n = int(re.sub(r"\D", "", raw))
        before, after = t[max(0, m.start() - 30):m.start()], t[m.end():m.end() + 14]
        if re.match(r"\s*(-|или)?\s*(к\b|к(?=\s|$|[.,])|комн|х\s*комн)", after) and n <= 5:
            continue
        if re.match(r"\s*-\s*\d", after):        # «80-90 тысяч»: потолок второй
            continue
        # «минут», «мин.», но не «минимум»: на нем замер 25.10.2026 поймал
        # площадь, прочитанную как минуты до метро.
        if re.match(r"\s*мин(ут|\.|\b)", after) or re.search(r"\bмин(ут\w*|\.)?\s*$", before):
            out["metro"] = n
            continue
        if re.match(r"\s*\+?\s*(м2|м²|м\b|кв|метр|квадрат)", after) or re.search(r"площад\w*\s*(от\s*)?$", before):
            out["area"] = n
            continue
        if n >= 10000:
            n //= 1000
        key = "entry" if ENTRY_CTX.search(before) else "price"
        lo, hi = BOUNDS[key]
        if lo <= n <= hi:
            out[key] = n
        else:
            unknown.append(raw)

    if "metro" not in out and re.search(r"(у|рядом с|возле|около)\s+метро|шагов", t):
        out["metro"] = NEAR_METRO
    if re.search(r"животн\w* по договор", t):
        out["pets"] = "negotiable"
    elif re.search(r"животн\w* нельзя|нельзя с живот|без животн", t):
        out["pets"] = "forbidden"
    elif any(re.search(rf"\b{p}", t) for p in PET_WORDS):
        out["pets"] = "allowed"
    if re.search(r"\bпарк", t):
        out["park"] = True
    if re.search(r"(внутри|в пределах|не за)\s+мкад", t):
        out["mkad"] = True
    unknown += [u for u in UNSUPPORTED if u in t]
    return {"filters": out, "unknown": unknown}


# ---------------------------------------------------------------------
# Разбор GigaChat
# ---------------------------------------------------------------------

SYSTEM = """Ты разбираешь запрос человека, который ищет квартиру в долгосрочную аренду в Москве, и отвечаешь одним JSON-объектом. Заполняй только то, что в запросе сказано явно. Чего нет в запросе, не заполняй.

Правила.
Цена всегда в тысячах рублей в месяц: «до 90», «90к», «90 тыс», «90 000» дают price_max 90. Диапазон «80-90» дает 90.
«однушка», «однокомнатная», «1к» дают rooms [1]; «двушка», «2к» дают [2]; «трешка» дает [3]; «однушка или двушка», «1-2 комнаты» дают [1, 2]; «от двух комнат» дает [2, 3, 4, 5]. Студия не комната, ее пиши в unsupported.
«у метро», «рядом с метро», «в шаговой доступности» без числа минут дают metro_max 10. Если названо число минут, бери его.
Кот, кошка, собака, питомец, «с животными» дают pets allowed. «по договоренности» дает negotiable. «животных нельзя» дает forbidden.
«рядом с парком», «у парка» дают park true. Название района со словом «парк» (Филевский парк) это район, а не парк.
«внутри МКАД», «в пределах МКАД», «не за МКАДом» дают inside_mkad true.
«на заселение», «сразу», «при въезде», «залог с комиссией и первым месяцем» это сумма при заселении entry_max в тысячах. Ее не дублируй в price_max: месячная цена это другое поле.
Площадь «от 40 м2», «от 40 метров», «45+ квадратов» дает area_min.
Район пиши в именительном падеже, как он называется: «в Хамовниках» дает Хамовники, «на Соколе» дает Сокол.
В unsupported пиши только пожелания, для которых нет поля (ремонт, балкон, без комиссии, новостройка, мебель, этаж, студия). То, что легло в поле, туда не пиши. Чаще всего unsupported пустой.

Примеры.
«двушка до 90 у метро с котом» дает {"rooms": [2], "price_max": 90, "metro_max": 10, "pets": "allowed"}
«однушка до 70 тысяч» дает {"rooms": [1], "price_max": 70}
«трешка в Хамовниках рядом с парком» дает {"rooms": [3], "district": "Хамовники", "park": true}
«до 100 тысяч, внутри мкад, на заселение не больше 250» дает {"price_max": 100, "inside_mkad": true, "entry_max": 250}
«двушка до 90 с балконом» дает {"rooms": [2], "price_max": 90, "unsupported": ["балкон"]}
«квартира можно с собакой» дает {"pets": "allowed"}
«при въезде могу отдать 300 тысяч» дает {"entry_max": 300}"""

# Поля ответа. Через вызов функции (functions, function_call) GigaChat-2
# на настроечных фразах разобрал верно 11 из 30: числа уходили в
# unsupported, двушка становилась трешкой. Тот же список полей в подсказке
# с ответом обычным JSON дал 29 из 30, поэтому так.
SYSTEM += """

Поля JSON. price_max целое, area_min целое, rooms список целых, metro_max целое, entry_max целое, pets одно из allowed, negotiable, forbidden, park true, inside_mkad true, district строка, unsupported список строк. Пустые поля не пиши. Ответ только JSON, без пояснений."""
FIELD = {"price_max": "price", "area_min": "area", "metro_max": "metro", "entry_max": "entry"}


class KeyRejected(Exception):
    """Сбер не принял ключ. Об этом стоит знать сразу, а не через месяц."""


def _ctx() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(CERT))


def _token(left: float) -> str:
    cache = STATE / "gigachat.token"
    try:
        c = json.loads(cache.read_text())
        if c["expires_at"] / 1000 - time.time() > 60:
            return c["access_token"]
    except (OSError, ValueError, KeyError):
        pass
    key = from_env("GIGACHAT_AUTH_KEY")
    if not key:
        raise KeyRejected("в .env нет GIGACHAT_AUTH_KEY")
    req = urllib.request.Request(OAUTH, data=b"scope=GIGACHAT_API_PERS", headers={
        "Authorization": "Basic " + key, "RqUID": str(uuid.uuid4()),
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, context=_ctx(), timeout=left) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            raise KeyRejected(f"OAuth ответил {e.code}") from e
        raise
    tok = json.loads(body)
    STATE.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"access_token": tok["access_token"], "expires_at": tok["expires_at"]}))
    cache.chmod(0o600)
    return tok["access_token"]


def gigachat_raw(text: str, model: str = MODEL) -> dict:
    """JSON из ответа модели как есть, без проверки."""
    start = time.monotonic()
    token = _token(DEADLINE)
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
        "temperature": 0.01,
    }).encode()
    req = urllib.request.Request(CHAT, data=body, headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json"})
    left = max(1.0, DEADLINE - (time.monotonic() - start))
    try:
        with urllib.request.urlopen(req, context=_ctx(), timeout=left) as r:
            ans = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            (STATE / "gigachat.token").unlink(missing_ok=True)
        raise
    m = re.search(r"\{.*\}", ans["choices"][0]["message"]["content"], re.S)
    raw = json.loads(m.group(0)) if m else {}
    return raw if isinstance(raw, dict) else {}


def validate(raw: dict, districts: list[str]) -> dict:
    """Ответ модели через сито. Все, что не прошло, уходит в unknown."""
    out: dict = {}
    unknown = [str(u) for u in raw.get("unsupported") or [] if str(u).strip()][:5]
    for src, key in FIELD.items():
        v = raw.get(src)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v):
            unknown.append(f"{key}={v}")
            continue
        lo, hi = BOUNDS[key]
        if lo <= int(v) <= hi:
            out[key] = int(v)
        else:
            unknown.append(f"{key}={v}")
    r = raw.get("rooms")
    if isinstance(r, list) and r:
        if all(isinstance(x, int) and not isinstance(x, bool) and 1 <= x <= 5 for x in r):
            out["rooms"] = sorted(set(r))
        else:
            unknown.append(f"rooms={r}")
    if raw.get("pets") in PETS:
        out["pets"] = raw["pets"]
    if raw.get("park") is True:
        out["park"] = True
    if raw.get("inside_mkad") is True:
        out["mkad"] = True
    if isinstance(raw.get("district"), str) and raw["district"].strip():
        d = match_district(raw["district"], districts)
        if d:
            out["district"] = d
        else:
            unknown.append(raw["district"][:40])
    return {"filters": out, "unknown": unknown}


def gigachat(text: str, districts: list[str], model: str = MODEL) -> dict:
    return validate(gigachat_raw(text, model), districts)


# ---------------------------------------------------------------------
# Применение
# ---------------------------------------------------------------------

def primary() -> str:
    """Какой разбор главный. Пишет src/nl_eval.py по правилу PRIMARY_RULE."""
    try:
        return json.loads((REPO / "results" / "nl_eval.json").read_text())["primary"]
    except (OSError, ValueError, KeyError):
        return "gigachat"


def parse(text: str, districts: list[str]) -> dict:
    """Разбор с откатом. source говорит, кто разобрал, или none."""
    first = primary()
    res, err = None, None
    if first == "rules":
        res = rules(text, districts)
        if res["filters"]:
            return {**res, "source": "rules"}
    try:
        res = gigachat(text, districts)
        if res["filters"]:
            return {**res, "source": "gigachat"}
    except KeyRejected as e:
        err = f"key: {e}"
    except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
        err = f"{type(e).__name__}: {e}"
    if first != "rules":
        res = rules(text, districts)
        if res["filters"]:
            return {**res, "source": "rules", "error": err}
    return {"filters": {}, "unknown": (res or {}).get("unknown", []), "source": "none", "error": err}


def districts_from_db() -> list[str]:
    pg = os.environ.get("PG_CONTAINER", "rent_pg")
    out = subprocess.run(
        ["timeout", "30", "docker", "exec", "-i", pg, "psql", "-qtA", "-U", "rent", "-d", "rent", "-c",
         "select district from core.offer where source='cian' and district is not null "
         "group by 1 having count(*) >= 3"], capture_output=True, text=True)
    return [x for x in out.stdout.splitlines() if x.strip()]


def file_lines(filters: dict, keep: dict) -> list[str]:
    """Фильтры в виде файла, который читает box_bot.sh."""
    lines = []
    for k in ("price", "area", "rooms", "metro", "entry", "pets", "park", "mkad", "district"):
        v = filters.get(k)
        if v is None:
            continue
        if k == "rooms":
            v = ",".join(map(str, v))
        elif v is True:
            v = 1
        lines.append(f"{k}={v}")
    # Свежесть выдачи фраза не задает, она остается, какой была.
    if "seen" in keep:
        lines.append(f"seen={keep['seen']}")
    return lines


def take_quota(chat: str, role: str) -> bool:
    f = STATE / f"nl.{chat}"
    today = date.today().isoformat()
    try:
        day, n = f.read_text().split()
        n = int(n) if day == today else 0
    except (OSError, ValueError):
        n = 0
    if n >= LIMITS.get(role, 0):
        return False
    STATE.mkdir(parents=True, exist_ok=True)
    f.write_text(f"{today} {n + 1}\n")
    return True


def alarm(err: str) -> None:
    subprocess.run(["bash", str(REPO / "scripts" / "box_notify.sh"), "gigachat-key",
                    f"GigaChat не принял ключ\n{time.strftime('%d.%m, %H:%M')}\n\n{err}. "
                    "Фразы бот разбирает правилами, кнопки работают. Ключ перевыпускается "
                    "в кабинете developers.sber.ru и кладется в .env как GIGACHAT_AUTH_KEY."],
                   capture_output=True, timeout=30)


def cmd_apply(path: str, chat: str, role: str, text: str) -> int:
    """Пишет фильтры и печатает одну строку состояния для box_bot.sh.

    ok <кто разобрал>    фильтры записаны, прежние в <файл>.prev
    limit <число>         лимит фраз на сегодня выбран
    none                  ничего не разобрано, фильтры не тронуты
    Следом строками «? ...» идет то, что понять не удалось.
    """
    if not take_quota(chat, role):
        print(f"limit {LIMITS.get(role, 0)}")
        return 0
    res = parse(text[:300], districts_from_db())
    if (res.get("error") or "").startswith("key:"):
        alarm(res["error"][5:])
    if res["filters"]:
        f = Path(path)
        keep = {}
        if f.exists():
            old = f.read_text()
            Path(path + ".prev").write_text(old)
            keep = dict(x.split("=", 1) for x in old.splitlines() if "=" in x)
        f.write_text("\n".join(file_lines(res["filters"], keep)) + "\n")
        print(f"ok {res['source']}")
    else:
        print("none")
    for u in res["unknown"]:
        print("? " + " ".join(str(u).split()))
    return 0


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "parse":
        print(json.dumps(parse(sys.argv[2], districts_from_db()), ensure_ascii=False))
        return 0
    if len(sys.argv) == 6 and sys.argv[1] == "apply":
        return cmd_apply(*sys.argv[2:6])
    print(__doc__.split("\n\n")[1])
    return 1


if __name__ == "__main__":
    sys.exit(main())
