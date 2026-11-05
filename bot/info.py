"""/market, /log, /pulse: посмотреть, ничего не меняя на машине."""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path

from bot import db, machine
from bot.config import Settings

# Запрос дословно из cmd_market старого бота: строка собирается в базе,
# и при пустом ответе честнее сказать про базу, чем показать нули.
MARKET_SQL = r"""
select 'Активных 1-2 комнаты: ' || count(*) filter (where is_active and rooms_count in (1,2)) ||
       E'\nМедиана, руб/мес: ' ||
       coalesce(round(percentile_cont(0.5) within group (
           order by price) filter (where is_active and rooms_count in (1,2)))::text, '?') ||
       E'\nСнятий вчера: ' ||
       (select count(*) from core.offer where disappeared_date = current_date - 1) ||
       E'\nСнятий за неделю: ' ||
       (select count(*) from core.offer where disappeared_date >= current_date - 7)
  from marts.offer_scored"""


def market_text(line: str | None, now: time.struct_time | None = None) -> str:
    stamp = time.strftime("%d.%m, %H:%M", now or time.localtime())
    return f"""Рынок на {stamp}

{line or 'база не ответила'}

Снятие — это уход объявления с площадки, а не обязательно сдача."""


def market() -> str:
    return market_text(db.scalar(MARKET_SQL))


LOG_DEFAULT = 15
LOG_ARG = re.compile(r"[1-9]|[1-4][0-9]|50")
LOG_ARG_TEXT = "Сколько строк показать — число от 1 до 50. Например: /log 30"
# Обрезка по длине: у телеграма предел около 4096 знаков, и сообщение
# сверх него не придет вообще, то есть хвост журнала молча пропадет ровно
# тогда, когда он нужнее всего.
LOG_LINE_CHARS = 120
LOG_TAIL_BYTES = 3000
# Нужно не больше 50 строк, а журнал прогона растет до мегабайт.
LOG_READ_BYTES = 1 << 20


def log_tail(path: Path, n: int) -> str:
    with path.open("rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - LOG_READ_BYTES))
        lines = f.read().decode(errors="replace").splitlines()[-n:]
    text = "".join(line[:LOG_LINE_CHARS] + "\n" for line in lines).encode()
    # Как tail -c у старого бота, только без половины буквы в начале.
    return text[-LOG_TAIL_BYTES:].decode(errors="ignore").rstrip("\n")


def log(settings: Settings, arg: str) -> str:
    if arg and not LOG_ARG.fullmatch(arg):
        return LOG_ARG_TEXT
    n = int(arg) if arg else LOG_DEFAULT
    last = machine.latest_run_log(settings.box_repo)
    if last is None:
        return "Журналов прогона нет вовсе."
    return f"""Журнал {last.name}, последние {n} строк

{log_tail(last, n)}"""


PULSE_TEXT = "Считаю итог дня, он придёт отдельным сообщением."


def pulse(settings: Settings) -> str:
    """Итог дня считает box_report.sh, он же шлет его через box_notify.sh.

    Процесс отсоединен: итог считается секунды, и ответ «считаю» не должен
    их ждать. Тестовый бот подменяет токен отправщика своим, иначе итог
    из проверки пришел бы от боевого бота.
    """
    env = dict(os.environ)
    if settings.dev:
        env["NOTIFY_TG_TOKEN"] = settings.token
    proc = subprocess.Popen(["bash", str(settings.repo / "scripts" / "box_report.sh")],
                     env=env, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Процесс живет сутками, и неподобранный завершившийся потомок висел
    # бы зомби до его конца.
    threading.Thread(target=proc.wait, name="pulse-report", daemon=True).start()
    return PULSE_TEXT
