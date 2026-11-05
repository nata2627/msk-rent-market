"""Новый бот: /market, /log, /pulse и меню команд.

Тексты сверяются со scripts/box_bot.sh и box_tg.py, пока они живы.
Сети и базы нет: база, запуск итога дня и время подменяются.
"""
import importlib.util
import os
import re
import time
from pathlib import Path

import pytest

from bot import access, commands, config, db, info, menu

ROOT = Path(__file__).resolve().parents[1]
OLD = (ROOT / "scripts" / "box_bot.sh").read_text()
NOW = time.strptime("02.11.2026 18:34", "%d.%m.%Y %H:%M")


@pytest.fixture
def settings(tmp_path):
    (tmp_path / ".env").write_text(
        "NOTIFY_TG_TOKEN=prod:secret\nNOTIFY_TG_TOKEN_DEV=dev:secret\n"
        "NOTIFY_TG_CHAT=42\nNOTIFY_TG_GUESTS=7\n")
    (tmp_path / "data" / "logs").mkdir(parents=True)
    return config.load({}, repo=tmp_path)


def ask(settings, text, chat="42"):
    t = time.time()
    return [r.text for r in access.handle_message(settings, chat, text, t, t, lambda s: None)]


def squash(s):
    return " ".join(s.split())


# ---------------------------------------------------------------------
# /market
# ---------------------------------------------------------------------

def test_market_текст_как_у_старого_бота():
    line = "Активных 1-2 комнаты: 9120\nМедиана, руб/мес: 78000\nСнятий вчера: 310\nСнятий за неделю: 2104"
    assert info.market_text(line, NOW) == f"""Рынок на 02.11, 18:34

{line}

Снятие — это уход объявления с площадки, а не обязательно сдача."""
    assert "${line:-база не ответила}" in OLD
    assert "Снятие — это уход объявления с площадки, а не обязательно сдача." in OLD


def test_market_база_не_ответила():
    assert "\nбаза не ответила\n" in info.market_text(None, NOW)


def test_market_запрос_дословно_из_старого_бота():
    old = OLD.split("cmd_market() {")[1].split('q "', 1)[1].split(';")"')[0]
    assert squash(info.MARKET_SQL) == squash(old)


def test_market_гостю_доступен_и_по_русски(settings, monkeypatch):
    monkeypatch.setattr(db, "scalar", lambda sql, params=None: "Активных 1-2 комнаты: 1")
    for cmd, chat in (("/market", "42"), ("/рынок", "42"), ("/market", "7")):
        assert ask(settings, cmd, chat)[0].startswith("Рынок на ")


# ---------------------------------------------------------------------
# /log
# ---------------------------------------------------------------------

def write_log(settings, name, lines, age=0):
    p = settings.box_repo / "data" / "logs" / name
    p.write_text("".join(f"{line}\n" for line in lines))
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def test_log_журналов_нет(settings):
    assert ask(settings, "/log") == ["Журналов прогона нет вовсе."]


def test_log_последний_по_времени_и_15_строк(settings):
    write_log(settings, "box_2026-11-02_1417.log", [f"новый {i}" for i in range(40)], age=10)
    write_log(settings, "box_2026-11-02_1100.log", ["старый"], age=3600)
    write_log(settings, "box_report.log", ["не журнал прогона"])
    text = ask(settings, "/log")[0]
    assert text.startswith("Журнал box_2026-11-02_1417.log, последние 15 строк\n\n")
    assert text.splitlines()[2:] == [f"новый {i}" for i in range(25, 40)]


def test_log_видит_журналы_макбука(settings):
    write_log(settings, "box_2026-10-28_1100.log", ["бокс"], age=86400 * 5)
    write_log(settings, "daily_20261102_1417.log", ["дневной"], age=60)
    write_log(settings, "mac_tail_202611.log", ["хвост"], age=10)
    write_log(settings, "backup_20261102_1704.log", ["копия"], age=1)
    assert ask(settings, "/log")[0].startswith("Журнал mac_tail_202611.log")


@pytest.mark.parametrize("arg,ok", [("1", True), ("9", True), ("10", True), ("49", True),
                                    ("50", True), ("0", False), ("51", False), ("05", False),
                                    ("100", False), ("-1", False), ("$(id)", False)])
def test_log_число_строк_как_в_dispatch(settings, arg, ok):
    write_log(settings, "box_2026-11-02_1417.log", [f"s{i}" for i in range(60)])
    text = ask(settings, f"/журнал {arg}")[0]
    if ok:
        assert text.splitlines()[0].endswith(f"последние {arg} строк")
        assert len(text.splitlines()) == 2 + int(arg)
    else:
        assert text == info.LOG_ARG_TEXT
    assert f"*) reply \"{info.LOG_ARG_TEXT}\"" in OLD


def test_log_длинные_строки_и_предел_сообщения(settings):
    write_log(settings, "box_2026-11-02_1417.log", ["ю" * 500] * 50)
    body = ask(settings, "/log 50")[0].split("\n\n", 1)[1]
    assert all(len(line) <= info.LOG_LINE_CHARS for line in body.splitlines())
    assert len(body.encode()) <= info.LOG_TAIL_BYTES
    assert "�" not in body


@pytest.mark.parametrize("cmd", ["/log", "/журнал 5", "/pulse", "/итог"])
def test_гостю_журнал_и_итог_недоступны(settings, cmd):
    assert ask(settings, cmd, chat="7") == [access.GUEST_REFUSAL]


# ---------------------------------------------------------------------
# /pulse
# ---------------------------------------------------------------------

class FakePopen:
    calls = []

    def __init__(self, args, env, **kw):
        FakePopen.calls.append((args, env, kw))

    def wait(self):
        return 0


@pytest.fixture
def popen(monkeypatch):
    FakePopen.calls = []
    monkeypatch.setattr(info.subprocess, "Popen", FakePopen)
    return FakePopen.calls


def test_pulse_запускает_итог_отдельно_и_сразу_отвечает(settings, popen):
    assert ask(settings, "/итог") == [info.PULSE_TEXT]
    args, env, kw = popen[0]
    assert args == ["bash", str(settings.repo / "scripts" / "box_report.sh")]
    assert kw["start_new_session"]
    assert f'reply "{info.PULSE_TEXT}"' in OLD


def test_pulse_тестовый_бот_шлет_итог_своим_токеном(settings, popen):
    ask(settings, "/pulse")
    assert popen[0][1]["NOTIFY_TG_TOKEN"] == "dev:secret"


def test_pulse_боевой_бот_токен_не_подменяет(tmp_path, popen, monkeypatch):
    (tmp_path / ".env").write_text("NOTIFY_TG_TOKEN=prod:secret\nNOTIFY_TG_CHAT=42\n")
    monkeypatch.delenv("NOTIFY_TG_TOKEN", raising=False)
    s = config.load({"RENT_BOT_ENV": "prod"}, repo=tmp_path)
    ask(s, "/pulse")
    assert "NOTIFY_TG_TOKEN" not in popen[0][1]


# ---------------------------------------------------------------------
# Меню и перенос
# ---------------------------------------------------------------------

def test_меню_как_у_box_tg_плюс_price_и_ask():
    spec = importlib.util.spec_from_file_location("box_tg", ROOT / "scripts" / "box_tg.py")
    box_tg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(box_tg)
    assert [c for c in menu.COMMANDS if c[0] not in ("price", "ask")] == box_tg.COMMANDS
    names = [c for c, _ in menu.COMMANDS]
    assert names.index("price") == names.index("market") + 1
    # Предел телеграма на описание команды 256 знаков, на имя 32.
    assert all(len(d) <= 256 and len(c) <= 32 for c, d in menu.COMMANDS)


def test_меню_раз_в_сутки(tmp_path):
    stamp = tmp_path / "menu"
    assert menu.due(stamp)
    stamp.touch()
    assert not menu.due(stamp)
    assert menu.due(stamp, now=time.time() + menu.REFRESH_S + 1)


def test_перенесенные_команды_не_числятся_в_переносе():
    done = {"/market", "/рынок", "/log", "/журнал", "/pulse", "/итог"}
    assert done <= commands.HANDLERS.keys()
    for cmd in done:
        assert re.search(rf"(^|[|( ]){re.escape(cmd)}[|)]", OLD, re.M)
