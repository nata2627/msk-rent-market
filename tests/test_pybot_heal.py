"""Новый бот: лечение машины сбора и отказы, которые берегут сбор.

Повторяет проверки test_bot.py (отказ посреди обхода, живой обход не
трогается, обещанные в сообщениях о беде команды есть) и добавляет режим
тестового бота: проверки настоящие, действия только описанием. Команды
наружу подменяются записью, в сеть и в docker тесты не ходят.
"""
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import access, commands, config, db, heal, status
from bot.actions import Reply
from tests.test_bot import PROMISED, ROOT
from tests.test_pybot_cards import OLD, settings  # noqa: F401


@pytest.fixture
def prod(tmp_path):
    (tmp_path / ".env").write_text("NOTIFY_TG_TOKEN=prod:secret\nNOTIFY_TG_CHAT=42\n"
                                   "NOTIFY_TG_GUESTS=7\n")
    return config.load({"RENT_BOT_ENV": "prod"}, repo=tmp_path)


@pytest.fixture
def machine(monkeypatch):
    """Все, что уходит на машину, записывается, а не выполняется."""
    ran, detached = [], []
    monkeypatch.setattr(heal, "_run", lambda cmd, timeout, cwd=None: ran.append(cmd))
    monkeypatch.setattr(heal, "_detach", lambda cmd: detached.append(cmd))
    monkeypatch.setattr(heal.time, "sleep", lambda s: None)
    monkeypatch.setattr(status, "gather_seg", lambda: 9000)
    alive = {"db": True}
    monkeypatch.setattr(db, "scalar", lambda sql, p=None: 1 if alive["db"] else None)
    return SimpleNamespace(ran=ran, detached=detached, alive=alive)


def ask(settings, text, chat="42"):  # noqa: F811
    t = time.time()
    return access.handle_message(settings, chat, text, t, t, lambda s: None)


def text_of(actions):
    return "\n".join(a.text for a in actions if isinstance(a, Reply))


def lock(s):
    p = s.box_repo / "data" / "box.lock"
    p.mkdir(parents=True)
    return p


def run_log(s, age_min):
    p = s.box_repo / "data" / "logs" / "daily_20261103_1100.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("11:00:01 INFO корзина\n")
    t = time.time() - age_min * 60
    os.utime(p, (t, t))
    return p


def cache(s):
    d = s.box_repo / "data" / "browser_profile" / "Default"
    for sub in ("Cache", "Code Cache"):
        (d / sub).mkdir(parents=True)
        (d / sub / "f").write_bytes(b"x" * 1000)
    (d / "Cookies").write_text("сессия")
    return d


# ---------------------------------------------------------------------
# Отказы посреди обхода: и в боевом, и в тестовом
# ---------------------------------------------------------------------

@pytest.mark.parametrize("env", ["dev", "prod"])
@pytest.mark.parametrize("cmd,head", [
    ("/clean", "Сейчас идёт обход — кеш не трогаю."),
    ("/catchup", "Обход уже идёт — второй не запускаю."),
    ("/догнать 2026-11-02", "Обход уже идёт — второй не запускаю."),
])
def test_лечение_отказывает_посреди_обхода(request, machine, env, cmd, head):
    s = request.getfixturevalue("settings" if env == "dev" else "prod")
    lock(s)
    d = cache(s)
    out = text_of(ask(s, cmd))
    assert out.startswith(head)
    assert (d / "Cache").exists()
    assert machine.ran == [] and machine.detached == []


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_unlock_не_трогает_живой_обход(request, machine, env):
    s = request.getfixturevalue("settings" if env == "dev" else "prod")
    lk = lock(s)
    run_log(s, age_min=1)
    out = text_of(ask(s, "/unlock"))
    assert out.startswith("Обход жив, замок не снимаю")
    assert "журнал писался 1 мин назад" in out
    assert machine.ran == [] and lk.exists()


def test_unlock_без_замка(prod, machine):
    assert text_of(ask(prod, "/замок")).startswith("Замка нет, снимать нечего.")
    assert machine.ran == []


def test_тексты_отказов_как_у_старого():
    src = Path(heal.__file__).read_text()
    for line in ("Браузер держит эти файлы открытыми, и чистка посреди обхода ломает",
                 "Посмотреть, как он идёт: /log", "Если он заклинил: /unlock",
                 "корзины читаются. Снимать замок у живого обхода нельзя: следующий",
                 "База и так отвечает, поднимать нечего. /status"):
        assert line in OLD and line in src


# ---------------------------------------------------------------------
# Тестовый бот: описание вместо действия
# ---------------------------------------------------------------------

def test_dev_clean_только_описывает(settings, machine):
    d = cache(settings)
    notes = []
    t = time.time()
    out = access.handle_message(settings, "42", "/clean", t, t, notes.append)
    assert text_of(out).startswith("Тестовый бот, машину не трогаю\n\nСделал бы: удалил бы")
    assert (d / "Cache").exists() and (d / "Code Cache").exists()
    assert any(n.startswith("clean: тестовый бот, сделал бы") for n in notes)


def test_dev_unlock_только_описывает(settings, machine):
    lk = lock(settings)
    run_log(settings, age_min=30)
    out = text_of(ask(settings, "/unlock"))
    assert "Сделал бы: docker kill rent_collect" in out and "журнал молчит 30 мин" in out
    assert machine.ran == [] and lk.exists()


def test_dev_catchup_только_описывает(settings, machine):
    out = text_of(ask(settings, "/catchup 2026-11-02"))
    assert "Сделал бы: запустил бы" in out and "narrow 2026-11-02" in out
    assert "собрано 9000 из 16500" in out
    assert machine.detached == []


def test_dev_dbup_только_описывает(settings, machine):
    machine.alive["db"] = False
    out = text_of(ask(settings, "/dbup"))
    assert "Сделал бы: docker compose up -d" in out
    assert machine.ran == []


def test_dbup_живая_база(settings, machine):
    assert text_of(ask(settings, "/база")) == "База и так отвечает, поднимать нечего. /status"


# ---------------------------------------------------------------------
# Боевой режим: действие
# ---------------------------------------------------------------------

def test_clean_чистит_кеш_и_не_трогает_куки(prod, machine):
    d = cache(prod)
    out = text_of(ask(prod, "/clean"))
    assert out.startswith("Кеш браузера вычищен\n\nОсвободилось:")
    assert not (d / "Cache").exists() and not (d / "Code Cache").exists()
    assert (d / "Cookies").read_text() == "сессия"


def test_unlock_снимает_заклинивший(prod, machine):
    lk = lock(prod)
    run_log(prod, age_min=30)
    out = text_of(ask(prod, "/unlock"))
    assert out.startswith("Замок снят\n\nДержался 0 мин, журнал молчал 30 мин.")
    assert machine.ran[0] == ["docker", "kill", "rent_collect"]
    assert [c[2] for c in machine.ran[1:]] == list(heal.KILL_PATTERNS)
    assert not lk.exists()


def test_непустой_замок_не_сносится(prod, machine):
    lk = lock(prod)
    (lk / "pid").write_text("123")
    run_log(prod, age_min=30)
    assert text_of(ask(prod, "/unlock")).startswith("Замок снять не удалось")
    assert lk.exists()


def test_catchup_запускает_обход(prod, machine):
    out = text_of(ask(prod, "/catchup"))
    day = time.strftime("%Y-%m-%d")
    assert out.startswith(f"Обход за {day} запущен")
    assert machine.detached == [[str(prod.box_repo / "scripts" / "box_daily.sh"), "narrow", day]]


@pytest.mark.parametrize("arg", ["вчера", "2026-11-2", "2026-11-02;id", "$(id)", "1999-01-01"])
def test_catchup_дата_только_по_образцу(prod, machine, arg):
    out = text_of(ask(prod, f"/catchup {arg}"))
    assert out == "Дата должна выглядеть как 2026-10-22. Без даты беру сегодня."
    assert machine.detached == []


def test_dbup_поднимает_базу(prod, machine, monkeypatch):
    seen = iter([None, 1])
    monkeypatch.setattr(db, "scalar", lambda sql, p=None: next(seen))
    out = text_of(ask(prod, "/dbup"))
    assert out.startswith("База поднята и отвечает")
    assert machine.ran == [["docker", "compose", "up", "-d"]]


def test_dbup_не_поднялась(prod, machine):
    machine.alive["db"] = False
    assert text_of(ask(prod, "/dbup")).startswith("База не поднялась")


def test_dbup_ждет_дольше_минуты():
    from bot import app
    assert app.timeout_for("/dbup") > 120 + 10
    assert app.timeout_for("/status") == 60


# ---------------------------------------------------------------------
# Обещания и полнота
# ---------------------------------------------------------------------

@pytest.mark.parametrize("cmd,caller", sorted(PROMISED.items()))
def test_обещанная_в_сообщении_команда_у_нового_бота_есть(cmd, caller):
    """Сообщение о беде называет команду, бот обязан ее понимать."""
    assert cmd in (ROOT / "scripts" / caller).read_text()
    assert cmd in commands.HANDLERS


def test_все_команды_старого_бота_перенесены():
    body = OLD.split("dispatch() {")[1].split("\n}")[0]
    old = set()
    for pattern in re.findall(r"^\s+(/[^)]*)\)", body, re.M):
        old |= {c for c in pattern.split("|") if c != "/*"}
    assert old and old <= commands.HANDLERS.keys(), old - commands.HANDLERS.keys()


@pytest.mark.parametrize("cmd", ["/clean", "/unlock", "/catchup", "/dbup",
                                 "/чистка", "/замок", "/догнать", "/база"])
def test_гостю_лечение_недоступно(settings, machine, cmd):
    lock(settings)
    out = text_of(ask(settings, cmd, chat="7"))
    assert out.startswith("Это команда владелицы")
    assert machine.ran == [] and machine.detached == []
