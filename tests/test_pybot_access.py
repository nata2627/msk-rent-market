"""Новый бот (пакет bot/): настройки, входной фильтр, разбор команд.

Строки таблицы «Безопасность» из orchestration-критериев здесь стали
тестами: чужой чат, гость, протухшая команда, аргумент с символами
оболочки, неизвестная команда. Сети нет: проверяется логика, которая
решает, что ответить, без телеграма.
"""
import re
from pathlib import Path

import pytest

from bot import access, commands, config
from bot.render import render

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0


@pytest.fixture
def settings(tmp_path):
    (tmp_path / ".env").write_text(
        "NOTIFY_TG_TOKEN=prod:secret\nNOTIFY_TG_TOKEN_DEV=dev:secret\n"
        "NOTIFY_TG_CHAT=42\nNOTIFY_TG_GUESTS=7, 8\n")
    return config.load({}, repo=tmp_path)


def handle(settings, chat, text, age=1):
    notes = []
    replies = access.handle_message(settings, chat, text, NOW - age, NOW, notes.append)
    return [r.text for r in replies], notes


# ---------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------

def test_по_умолчанию_тестовый_токен(settings, tmp_path):
    # Случайный запуск без RENT_BOT_ENV не имеет права занять боевой токен.
    assert settings.env == "dev"
    assert settings.token == "dev:secret"
    assert settings.state_dir == tmp_path / "data" / "bot_dev"
    assert settings.log_file.name == "bot_dev.log"


def test_боевой_режим_по_переменной(tmp_path, settings):
    s = config.load({"RENT_BOT_ENV": "prod"}, repo=tmp_path)
    assert s.token == "prod:secret"
    assert s.state_dir == tmp_path / "data" / "bot"


def test_неизвестный_режим_не_запускается(tmp_path, settings):
    with pytest.raises(ValueError):
        config.load({"RENT_BOT_ENV": "stage"}, repo=tmp_path)


def test_гости_из_списка_через_запятую(settings):
    assert settings.guests == {"7", "8"}


# ---------------------------------------------------------------------
# Безопасность
# ---------------------------------------------------------------------

def test_чужой_чат_без_ответа_но_в_журнале(settings):
    replies, notes = handle(settings, "999", "/help")
    assert replies == []
    assert notes == ["чужой чат 999, пропускаю: /help"]


@pytest.mark.parametrize("cmd", ["/clean", "/unlock", "/catchup", "/dbup", "/log",
                                 "/чистка", "/журнал", "/CLEAN@rent_bot", "/pulse"])
def test_гостю_лечение_и_журнал_недоступны(settings, cmd):
    replies, notes = handle(settings, "7", cmd)
    assert replies == [access.GUEST_REFUSAL]
    assert "отказано" in notes[0]


@pytest.mark.parametrize("cmd", ["/help", "/status", "/flats", "/подбор",
                                 "двушка до 90 у метро"])
def test_гостю_просмотр_доступен(settings, cmd):
    assert access.guest_allowed(cmd)


def test_гостевой_список_как_у_старого_бота():
    src = (ROOT / "scripts" / "box_bot.sh").read_text()
    line = next(l for l in src.splitlines() if l.startswith("GUEST_COMMANDS="))
    # /new появилась 06.11, после переключения: у старого бота ее нет.
    assert set(line.split('"')[1].split()) == access.GUEST_COMMANDS - {"/new", "/новое"}


def test_протухшая_команда_не_выполняется(settings):
    replies, notes = handle(settings, "42", "/help", age=901)
    assert replies == [access.stale_text("/help")]
    assert "старше пятнадцати минут" in notes[0]


def test_команда_на_границе_срока_выполняется(settings):
    replies, _ = handle(settings, "42", "/help", age=900)
    assert replies == [commands.HELP_OWNER]


@pytest.mark.parametrize("arg", ["; rm -rf /", "$(date)", "`id`", "'\"", "a\nb"])
def test_аргумент_с_оболочкой_остается_текстом(settings, arg):
    p = commands.parse(f"/log {arg}")
    assert p.cmd == "/log"
    assert p.tail == arg
    replies, _ = handle(settings, "42", f"/nosuch {arg}")
    assert replies == ["Команды «/nosuch» нет. Что умею — /help"]


def test_неизвестная_команда_не_выполняется(settings):
    replies, _ = handle(settings, "42", "/rm")
    assert replies == ["Команды «/rm» нет. Что умею — /help"]


# ---------------------------------------------------------------------
# Разбор и ответы
# ---------------------------------------------------------------------

def test_разбор_как_в_dispatch():
    p = commands.parse("/Filter@rent_msk_bot район Марьино Роща")
    assert (p.cmd, p.arg, p.tail) == ("/filter", "район", "район Марьино Роща")
    assert commands.parse("/help").tail == ""


def test_help_владелице_и_гостю_разный(settings):
    assert handle(settings, "42", "/help")[0] == [commands.HELP_OWNER]
    assert handle(settings, "7", "/start")[0] == [commands.HELP_GUEST]


def test_тексты_help_дословно_из_старого_бота():
    # Кроме строк про потолок цены через /price и про /new: их у старого
    # бота нет.
    src = (ROOT / "scripts" / "box_bot.sh").read_text()
    added = ("/price 77 — потолок цены в подборе, в тысячах\n",
             "/new — только новое под отбор за последний день\n")
    for text in (commands.HELP_OWNER, commands.HELP_GUEST):
        for line in added:
            assert line in text
            text = text.replace(line, "")
        assert text in src


def test_отказ_гостю_называет_только_доступное():
    named = set(re.findall(r"/\w+", access.GUEST_REFUSAL.splitlines()[-1]))
    assert named <= access.GUEST_COMMANDS
    assert "/log" not in named


def test_разметка_та_же_что_у_box_tg():
    import importlib.util
    spec = importlib.util.spec_from_file_location("box_tg", ROOT / "scripts" / "box_tg.py")
    box_tg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(box_tg)
    text = "Заголовок <x>\nстрока & ещё\n$ make up\nhttps://cian.ru/rent/flat/1?a=b"
    assert render(text) == box_tg.render(text)


def test_токен_не_попадает_в_журнал(settings, tmp_path):
    # httpx на уровне INFO пишет адрес запроса, а в адресе API телеграма
    # лежит токен.
    import logging
    from bot import app
    app.setup_logging(settings)
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
