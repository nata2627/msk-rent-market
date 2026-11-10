"""Лечение машины сбора: /clean, /unlock, /catchup, /dbup.

Тексты и порядок проверок старого бота. Проверки (идет ли обход, жив ли
он, отвечает ли база) выполняются по-настоящему и в тестовом боте, иначе
отказ посреди обхода нечем проверить. Само действие тестовый бот не
делает: он пишет в ответ и в журнал, что сделал бы. Иначе проверка
команды с телефона снесла бы замок у боевого сбора.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from bot import db, machine, status
from bot.actions import Ctx

# Чистится только кеш. Cookies лежат отдельно и не трогаются: в них
# накопленная сессия площадки, без нее выдача дается заметно хуже.
CACHE_DIRS = ("Cache", "Code Cache")

# Чем добить заклинивший обход: контейнер сбора, сам сборщик, его браузер
# и виртуальный экран бокса. Имена образцов как у старого бота.
KILL_PATTERNS = (r"etl\.collect", r"user-data-dir=.*browser_profile", "Xvfb :99")

# Молчание журнала единственный признак клинча: без него «замок висит»
# значит «обход работает». Пять минут, а не тридцать, как у сторожа: тут
# решает человек, и он уже видел /status.
QUIET_MIN = 5


def dry(ctx: Ctx, cmd: str, what: str) -> bool:
    """В тестовом боте вместо действия описание; True, если так и вышло."""
    if not ctx.settings.dev:
        return False
    ctx.note(f"{cmd}: тестовый бот, сделал бы: {what}")
    ctx.reply(f"Тестовый бот, машину не трогаю\n\nСделал бы: {what}")
    return True


def free_mb(ctx: Ctx) -> int:
    return machine.disk_free_mb(ctx.settings.box_repo)


def cmd_clean(ctx: Ctx, p=None) -> None:
    repo = ctx.settings.box_repo
    if machine.crawl_running(repo):
        ctx.reply("""Сейчас идёт обход — кеш не трогаю.

Браузер держит эти файлы открытыми, и чистка посреди обхода ломает
профиль. Повторите /clean, когда обход кончится, или дождитесь:
прогон сам чистит кеш, когда тот перевалит за 300 МБ.""")
        return
    cache = repo / "data" / "browser_profile" / "Default"
    targets = [cache / d for d in CACHE_DIRS]
    if dry(ctx, "clean", "удалил бы " + ", ".join(str(t) for t in targets)
           + f"; свободно сейчас {free_mb(ctx)} МБ"):
        return
    was = free_mb(ctx)
    for t in targets:
        shutil.rmtree(t, ignore_errors=True)
    now = free_mb(ctx)
    ctx.note(f"clean: освобождено {now - was} МБ")
    ctx.reply(f"""Кеш браузера вычищен

Освободилось: {now - was} МБ
Свободно сейчас: {now} МБ

Куки не тронуты — без накопленной сессии выдача даётся хуже.""")


def _minutes_since(path: Path | None) -> int:
    # Нет файла, значит отсчет от нуля эпохи, как у старого stat || echo 0.
    try:
        mtime = path.stat().st_mtime if path else 0
    except OSError:
        mtime = 0
    return int((time.time() - mtime) // 60)


def cmd_unlock(ctx: Ctx, p=None) -> None:
    repo = ctx.settings.box_repo
    lock = repo / "data" / "box.lock"
    if not machine.crawl_running(repo):
        ctx.reply("""Замка нет, снимать нечего. Обход сейчас не идёт.

Запустить сбор за сегодня: /catchup""")
        return
    age = _minutes_since(lock)
    # Журнал ищется так же, как для /log (решение 7): на макбуке прогон
    # пишет daily_* и mac_tail_*, а не box_*.
    quiet = _minutes_since(machine.latest_run_log(repo))
    if quiet < QUIET_MIN:
        ctx.reply(f"""Обход жив, замок не снимаю

Замок держится {age} мин, но журнал писался {quiet} мин назад — значит
корзины читаются. Снимать замок у живого обхода нельзя: следующий
запуск войдёт вторым браузером в тот же профиль.

Посмотреть, что он делает: /log""")
        return
    if dry(ctx, "unlock", f"docker kill rent_collect, pkill -f {', '.join(KILL_PATTERNS)}, "
           f"rmdir {lock} (замок {age} мин, журнал молчит {quiet} мин)"):
        return
    _run(["docker", "kill", "rent_collect"], 20)
    time.sleep(5)
    for pattern in KILL_PATTERNS:
        _run(["pkill", "-f", pattern], 10)
    time.sleep(2)
    try:
        lock.rmdir()
    except OSError:
        pass
    if machine.crawl_running(repo):
        ctx.reply(f"""Замок снять не удалось

Держится {age} мин, журнал молчит {quiet} мин, но каталог не пуст — а
непустой замок сторож и бот не сносят: это уже не замок.

Тут нужны руки: ls -la ~/rent_msk/data/box.lock""")
        return
    ctx.note(f"unlock: замок снят вручную (возраст {age} мин, тишина {quiet} мин)")
    ctx.reply(f"""Замок снят

Держался {age} мин, журнал молчал {quiet} мин.
Сторож подхватит сам в ближайшие двадцать минут, как только площадка
отдаст выдачу. Не ждать — /catchup.""")


def cmd_catchup(ctx: Ctx, day: str) -> None:
    repo = ctx.settings.box_repo
    if machine.crawl_running(repo):
        ctx.reply("""Обход уже идёт — второй не запускаю.

Посмотреть, как он идёт: /log
Если он заклинил: /unlock""")
        return
    seg = status.of_claimed(status.gather_seg(), status.gather_claimed())
    script = repo / "scripts" / "box_daily.sh"
    if dry(ctx, "catchup", f"запустил бы {script} narrow {day} (по сегменту собрано "
           f"{seg})"):
        return
    _detach([str(script), "narrow", day])
    ctx.note(f"catchup: запущен обход за {day}")
    ctx.reply(f"""Обход за {day} запущен

Сейчас по сегменту собрано {seg}.
Уже собранные корзины пропускаются, так что добор идёт быстрее полного.
Ход: /log, итог придёт сам.""")


def db_alive() -> bool:
    return db.scalar("select 1") == 1


def cmd_dbup(ctx: Ctx, p=None) -> None:
    if db_alive():
        ctx.reply("База и так отвечает, поднимать нечего. /status")
        return
    # Compose-файл лежит в копии, где идет сбор. Прежний ~/rent_data жил
    # только на боксе и исчез вместе с его SSD.
    compose = ctx.settings.box_repo
    if dry(ctx, "dbup", f"docker compose up -d postgres в {compose}"):
        return
    _run(["docker", "compose", "up", "-d", "postgres"], 120, cwd=compose)
    time.sleep(10)
    if db_alive():
        ctx.note("dbup: база поднята")
        ctx.reply("""База поднята и отвечает

Сбор продолжится по расписанию. Не ждать — /catchup.""")
    else:
        ctx.reply("""База не поднялась

docker compose up отработал, но pg_isready по-прежнему молчит. Дальше
только руками: скорее всего кончился диск (/status) или умер демон
Docker.""")


def _run(cmd: list[str], timeout: float, cwd: Path | None = None) -> None:
    # Итог шага не важен: что получилось, показывает проверка после.
    try:
        subprocess.run(cmd, cwd=cwd, timeout=timeout, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _detach(cmd: list[str]) -> None:
    """Обход живет часами и не должен умереть вместе с ботом."""
    proc = subprocess.Popen(cmd, env=dict(os.environ), start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    threading.Thread(target=proc.wait, name="catchup", daemon=True).start()
