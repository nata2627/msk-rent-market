"""Проверка сторожа снаружи машины сбора, см. .github/workflows/watchdog.yml.

    python3 .github/watchdog.py <время последней отметки, unix>

Пишет в телеграм и падает, если есть беда. Ручной запуск
(workflow_dispatch) пишет всегда, даже когда все в порядке: так
проверяется сам канал.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))
BEAT_LATE = timedelta(hours=1)
# От часа до 2 ч 10 мин: два-три сообщения в начале беды при ходе раз в
# полчаса с опозданиями GitHub, дальше напоминание раз в шесть часов.
FIRST_WINDOW = timedelta(hours=2, minutes=10)
REMIND = timedelta(hours=6)
SITE_LATE = timedelta(hours=36)


def send(text: str) -> None:
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if not token or not chat:
        print("::warning::секретов телеграма нет, сообщение не отправлено")
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=20)


def beat_problem(beat: int, now: datetime) -> tuple[str | None, bool]:
    """Текст беды пульса и надо ли о ней сказать в этот заход."""
    if not beat:
        return "ветки heartbeat нет: пульс бокса ни разу не дошел до GitHub", True
    age = now - datetime.fromtimestamp(beat, timezone.utc)
    if age <= BEAT_LATE:
        return None, False
    last = datetime.fromtimestamp(beat, MSK)
    text = (f"Mac mini молчит {age.total_seconds() / 3600:.1f} ч, последний пульс "
            f"{last:%d.%m %H:%M} МСК. Машина зависла, выключена или без сети. "
            "Бот и сбор не работают.")
    # Напоминание, когда возраст пересекает очередные шесть часов: окно
    # шириной в полчаса, как шаг расписания, плюс запас на опоздание.
    since = age - BEAT_LATE
    loud = age <= FIRST_WINDOW or since % REMIND < timedelta(minutes=40)
    return text, loud


def site_problems(now: datetime) -> list[str]:
    d = json.load(open("site/data/site.json"))
    made = datetime.fromisoformat(d["generated_at"])
    data = date.fromisoformat(d["data_date"])
    today = now.astimezone(MSK).date()
    out = []
    if now - made > SITE_LATE:
        out.append(f"витрина не обновлялась {(now - made).days} дн., выгрузка падает")
    if (today - data).days > 2:
        out.append(f"последний покрытый день {data:%d.%m}, обход не идет")
    return out


def main() -> int:
    now = datetime.now(timezone.utc)
    beat = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 0
    manual = os.environ.get("EVENT") == "workflow_dispatch"
    text, loud = beat_problem(beat, now)
    msgs = [text] if text and (loud or manual) else []
    # Витрина раз в сутки, в заход около 21:00 МСК, иначе одна беда
    # витрины слала бы сообщение каждые полчаса.
    site = site_problems(now)
    msk = now.astimezone(MSK)
    if site and (manual or (msk.hour == 21 and msk.minute < 30)):
        msgs += site
    for m in [text, *site]:
        if m:
            print(f"::error::{m}")
    if manual and not msgs:
        last = datetime.fromtimestamp(beat, MSK) if beat else None
        msgs = [f"Сторож GitHub: проверка канала. Пульс {last:%d.%m %H:%M} МСК, витрина в порядке."]
    if msgs:
        send("\n\n".join(msgs))
    # Падает только заход, который сказал о беде: GitHub пишет на почту о
    # каждом упавшем задании по расписанию, и молчаливые заходы посреди
    # той же беды засыпали бы почту.
    return 1 if msgs and not manual else 0


if __name__ == "__main__":
    sys.exit(main())
