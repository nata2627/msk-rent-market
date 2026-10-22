"""Тесты бота, который принимает команды с телефона (scripts/box_bot.sh).

Бот существует ради разъездов: беда случается тогда, когда до консоли не
добраться, и лечение должно нажиматься в чате. Отсюда и то, что здесь
проверяется.

Первое и главное — безопасность. Бот выполняет команды на машине сбора,
а текст в него приходит из интернета. Чужой чат не должен получить ни
ответа, ни действия; аргумент команды не должен подставляться в оболочку
как есть; неизвестная команда не должна исполняться вовсе.

Второе — обещания, которые бот даёт сообщениям о беде. Каждое сообщение
называет команду лечения (/clean, /unlock, /catchup, /dbup), и если
команда исчезнет из бота, сообщение станет враньём. Поэтому связь
«сообщение обещает — бот умеет» проверяется явно.

Третье — отказы. Команды лечения обязаны отказываться работать посреди
обхода: второй браузер в том же профиле Chrome ломает сессию площадки
обоим, и это уже стоило проекту дня.

Ни сеть, ни база здесь не трогаются: curl, docker и прочее подменяются
заглушками в PATH.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "scripts" / "box_bot.sh"

# Команда бота -> в каком скрипте сообщение о беде её обещает.
# Обещание без исполнителя хуже отсутствия обещания: человек в дороге
# нажмёт и решит, что починил.
PROMISED = {
    "/clean": "box_watch.sh",
    "/unlock": "box_watch.sh",
    "/catchup": "box_daily.sh",
    "/dbup": "box_daily.sh",
    "/status": "box_watch.sh",
    "/log": "box_daily.sh",
}


@pytest.fixture
def box(tmp_path):
    """Песочница: копия скриптов, заглушки вместо всего опасного.

    Проверка подмены — не формальность. 22.10.2026 проверочная обвязка
    без неё позвала настоящий docker kill и убила живой обход на 330-й
    корзине.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "data" / "logs").mkdir(parents=True)
    (repo / "data" / "bot").mkdir(parents=True)
    for name in ("box_bot.sh", "box_notify.sh"):
        (repo / "scripts" / name).write_text((ROOT / "scripts" / name).read_text())
    (repo / ".env").write_text("NOTIFY_TG_TOKEN=111:test\nNOTIFY_TG_CHAT=42\n")

    binx = tmp_path / "bin"
    binx.mkdir()
    calls = tmp_path / "calls.log"
    for name in ("docker", "curl", "pkill", "setsid", "nohup", "free", "df", "rmdir"):
        (binx / name).write_text(
            "#!/bin/bash\n"
            f'echo "{name} $*" >> {calls}\n'
            'case "$1" in\n'
            '  exec) echo 0 ;;\n'
            'esac\n'
            "exit 0\n")
        (binx / name).chmod(0o755)
    # df и free должны отдавать разбираемые числа, иначе арифметика в
    # скрипте споткнётся раньше проверяемого места.
    (binx / "df").write_text("#!/bin/bash\necho 'h h h h'\necho '/dev/x 100 50 9000 50% /'\nexit 0\n")
    (binx / "df").chmod(0o755)
    (binx / "free").write_text("#!/bin/bash\necho 'h'\necho 'Mem: 3072 1000 500 100 200 1800'\nexit 0\n")
    (binx / "free").chmod(0o755)

    # Окружение вычищается от настоящих секретов намеренно. Без этого
    # песочница подхватывает NOTIFY_TG_TOKEN из окружения того, кто
    # запустил тесты (переменная сильнее .env — так задумано у
    # отправщика), и тест начинает ходить в НАСТОЯЩИЙ телеграм, а
    # настоящий токен попадает в вывод упавшего теста. Поймано на себе
    # 22.10.2026.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("NOTIFY_", "BOX_SSH_"))}
    env["PATH"] = f"{binx}:{os.environ['PATH']}"
    return {"repo": repo, "bot": repo / "scripts" / "box_bot.sh",
            "calls": calls, "env": env, "bin": binx}


def run(box, *args, stdin=""):
    return subprocess.run(["bash", str(box["bot"]), *args], input=stdin,
                          capture_output=True, text=True, env=box["env"], timeout=60)


def calls(box):
    return box["calls"].read_text() if box["calls"].exists() else ""


# ---------------------------------------------------------------------
# Безопасность
# ---------------------------------------------------------------------

def test_чужой_чат_не_получает_ответа(box):
    """Чужому не отвечаем вовсе: ответ подтвердил бы, что бот живой."""
    src = BOT.read_text()
    assert 'if [ "$CHAT" != "$OWNER" ]' in src
    # В ветке чужого чата нет вызова reply — только запись в журнал.
    foreign = src.split('if [ "$CHAT" != "$OWNER" ]')[1].split("fi")[0]
    assert "reply" not in foreign
    assert "note" in foreign


@pytest.mark.parametrize("arg", [
    "; rm -rf /",
    "$(date)",
    "`id`",
    "2026-10-22; echo взломано",
    "../../etc/passwd",
    "' or 1=1 --",
])
def test_аргумент_команды_проверяется_образцом(box, arg):
    """Опасный аргумент отвергается, а не подставляется в оболочку."""
    r = run(box, "--run", f"/catchup {arg}")
    assert r.returncode == 0
    # Ни запуска прогона, ни чужих процессов: дошло только до отказа.
    assert "setsid" not in calls(box)
    assert "box_daily.sh" not in calls(box)


def test_дата_правильного_вида_принимается(box):
    r = run(box, "--run", "/catchup 2026-10-22")
    assert r.returncode == 0
    assert "setsid" in calls(box) or "box_daily.sh" in calls(box)


def test_неизвестная_команда_не_исполняется(box):
    r = run(box, "--run", "/exec whoami")
    assert r.returncode == 0
    assert "whoami" not in calls(box)


def test_разбор_белым_списком(box):
    """Диспетчер — case по известным именам, а не eval чего пришло."""
    src = BOT.read_text()
    assert "eval" not in src
    assert re.search(r"case \"\$cmd\" in", src)


# ---------------------------------------------------------------------
# Отказы, которые берегут сбор
# ---------------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["/clean", "/catchup"])
def test_лечение_отказывает_посреди_обхода(box, cmd):
    """Второй браузер в том же профиле ломает сессию площадки обоим."""
    (box["repo"] / "data" / "box.lock").mkdir()
    r = run(box, "--run", cmd)
    assert r.returncode == 0
    assert "rm -rf" not in calls(box)
    assert "setsid" not in calls(box)


def test_unlock_не_трогает_живой_обход(box):
    """Журнал писался только что — значит корзины читаются."""
    (box["repo"] / "data" / "box.lock").mkdir()
    log = box["repo"] / "data" / "logs" / "box_20261022_1200.log"
    log.write_text("12:00:01 INFO [1] корзина\n")
    r = run(box, "--run", "/unlock")
    assert r.returncode == 0
    assert "docker kill" not in calls(box)
    assert "pkill" not in calls(box)
    assert (box["repo"] / "data" / "box.lock").exists()


# ---------------------------------------------------------------------
# Обещания сообщений о беде
# ---------------------------------------------------------------------

@pytest.mark.parametrize("cmd,caller", sorted(PROMISED.items()))
def test_обещанная_в_сообщении_команда_у_бота_есть(cmd, caller):
    """Сообщение о беде называет команду — бот обязан её понимать."""
    text = (ROOT / "scripts" / caller).read_text()
    assert cmd in text, f"{caller} больше не обещает {cmd}"
    assert cmd in BOT.read_text(), f"бот не понимает обещанную {cmd}"


def test_в_сообщениях_о_беде_нет_команд_для_терминала():
    """Лечение в дороге нажимается, а не набирается в ssh.

    Команда вида `rm -rf ~/rent_msk/...` в уведомлении бесполезна ровно
    тогда, когда уведомление и нужно: в поезде консоли нет.
    """
    for name in ("box_watch.sh", "box_daily.sh", "box_report.sh"):
        src = (ROOT / "scripts" / name).read_text()
        # Берём только текст уведомлений: строки внутри вызовов notify.
        for block in re.findall(r"notify (?:--always )?[a-z-]+ \"(.*?)\"", src, re.S):
            assert "ssh " not in block, f"{name}: в сообщении остался ssh"
            assert "rm -rf" not in block, f"{name}: в сообщении остался rm -rf"


# ---------------------------------------------------------------------
# Опрос
# ---------------------------------------------------------------------

def test_первый_запуск_не_выполняет_старое(box):
    """В телеграме лежит всё, что писали боту до его появления."""
    src = BOT.read_text()
    assert "FIRST_RUN" in src
    assert "пропускаю старое" in src


def test_протухшие_команды_не_выполняются():
    """Бокс мог молчать сутки, и вчерашнее «догнать день» значит не то."""
    src = BOT.read_text()
    assert re.search(r"NOW - WHEN \)\) -gt \d+", src)


def test_у_замка_опроса_есть_срок_давности():
    """Зависший опрос не имеет права заткнуть бота навсегда."""
    src = BOT.read_text()
    assert "POLL_LOCK" in src
    assert "LOCK_AGE" in src


def test_без_настроенного_канала_бот_молчит_и_не_падает(box):
    (box["repo"] / ".env").write_text("")
    r = run(box)
    assert r.returncode == 0
    assert calls(box) == "" or "curl" not in calls(box)


# ---------------------------------------------------------------------
# Фильтры подбора
# ---------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "90; drop table raw.offer_snapshot",
    "90 or 1=1",
    "'",
    "$(id)",
    "90'--",
    "сто",
])
def test_числовой_фильтр_не_принимает_мусор(box, value):
    """В SQL уходит только проверенное число.

    Фильтр собирается в строку запроса, поэтому проверка образцом тут не
    опрятность, а единственное, что отделяет чат от базы.
    """
    r = run(box, "--run", f"/filter цена {value}")
    assert r.returncode == 0
    saved = box["repo"] / "data" / "bot" / "filters"
    assert not saved.exists() or "цена" not in saved.read_text()
    assert "drop" not in calls(box).lower()


def test_число_в_границах_сохраняется(box):
    run(box, "--run", "/filter цена 90")
    saved = (box["repo"] / "data" / "bot" / "filters").read_text()
    assert "price=90" in saved


def test_число_вне_границ_отвергается(box):
    """Цена в рублях вместо тысяч — обычная опечатка, и она бы молча
    показала пустой список вместо отбора."""
    run(box, "--run", "/filter цена 90000")
    saved = box["repo"] / "data" / "bot" / "filters"
    assert not saved.exists() or "price=" not in saved.read_text()


def test_район_сверяется_со_справочником(box):
    """Название района идёт в запрос только если нашлось в базе."""
    r = run(box, "--run", "/filter район Выдуманный'; drop table x; --")
    assert r.returncode == 0
    saved = box["repo"] / "data" / "bot" / "filters"
    assert not saved.exists() or "district=" not in saved.read_text()


def test_фильтр_переживает_перезапуск(box):
    run(box, "--run", "/filter комнаты 1,2")
    run(box, "--run", "/filter площадь 40")
    saved = (box["repo"] / "data" / "bot" / "filters").read_text()
    assert "rooms=1,2" in saved and "area=40" in saved


def test_сброс_убирает_всё(box):
    run(box, "--run", "/filter цена 90")
    run(box, "--run", "/filter сброс")
    saved = box["repo"] / "data" / "bot" / "filters"
    assert not saved.exists() or saved.read_text().strip() == ""


def test_подбор_не_заводит_своего_определения():
    """Бот и дашборд читают один и тот же marts.offer_pick.

    Два определения подбора в проекте уже расходились: make flats
    отбирал по is_my_segment, дашборд этой разметкой не пользовался, и
    они тихо разъехались. Поэтому базовые условия лежат в схеме, а не
    повторяются в питоне и в bash.
    """
    bot = BOT.read_text()
    dash = (ROOT / "scripts" / "setup_dashboard.py").read_text()
    schema = (ROOT / "sql" / "schema" / "04_alter.sql").read_text()

    assert "marts.offer_pick" in bot
    assert "marts.offer_pick" in dash
    assert "create or replace view marts.offer_pick" in schema
    # Базовые условия — ровно в одном месте, в схеме.
    for gate in ("is_duplicate", "is_room_share", "is_daily", "lease_term_type"):
        assert gate in schema, f"{gate} пропал из определения подбора"
        assert gate not in bot, f"{gate} продублирован в боте"


def test_свежесть_в_подборе_не_отключаема():
    """Умолчание «была в последней выдаче» убирается только явно.

    На 04.10.2026 из 17 935 активных однушек и двушек 2091 не
    встретились в последнем обходе: без этого условия каждое восьмое
    объявление ведёт на снятую публикацию.
    """
    assert "crawls_missed <= ${v:-0}" in BOT.read_text()
