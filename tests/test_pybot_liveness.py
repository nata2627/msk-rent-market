"""Живучесть нового бота: срок на обработчик, ошибки, пульс, /status.

Сети и базы нет: зависание изображает sleep, база подменяется.
"""
import asyncio
import time

import pytest

from bot import access, commands, config, liveness, status


def test_зависший_обработчик_не_держит_остальных():
    async def go():
        slow = liveness.guarded(time.sleep, 1, timeout=0.2, label="медленная")
        fast = liveness.guarded(lambda: [commands.Reply("ok")], timeout=1)
        started = time.monotonic()
        res = await asyncio.gather(slow, fast)
        return res, time.monotonic() - started

    (slow, fast), spent = asyncio.run(go())
    assert slow == [commands.Reply(liveness.TIMEOUT_TEXT)]
    assert fast == [commands.Reply("ok")]
    assert spent < 1


def test_ошибка_обработчика_отвечает_коротко():
    def boom():
        raise RuntimeError("сломалось")
    assert asyncio.run(liveness.guarded(boom)) == [commands.Reply(liveness.ERROR_TEXT)]


def test_чаты_не_ждут_друг_друга_а_один_чат_по_порядку():
    locks = liveness.ChatLocks()
    assert locks("1") is locks("1")
    assert locks("1") is not locks("2")


def test_пульс_пишет_файл_и_сторож_выходит(tmp_path):
    exited = []
    p = liveness.Pulse(tmp_path / "pulse", exit_fn=exited.append)
    p.beat()
    assert abs(int((tmp_path / "pulse").read_text()) - time.time()) < 2
    assert not p.dead()
    assert p.dead(now=p.last + liveness.PULSE_DEAD_S + 1)


# ---------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------

FACTS = status.Facts(seg=8250, covered="1, 2", run="207 narrow ok, начат 02.11 14:17",
                     fresh="сегодня, 02.11", disk=51868, mem=1363,
                     crawling=True, last_line="корзина 330 из 600")
NOW = time.strptime("02.11.2026 18:34", "%d.%m.%Y %H:%M")


def test_status_владелице_как_у_старого_бота():
    assert status.text("owner", FACTS, NOW) == """Состояние на 02.11, 18:34

Сегмент за сегодня: 8250 из 16500 (50%)
Покрытые комнатности: 1, 2
Витрины: сегодня, 02.11

идёт обход, последняя строка журнала: корзина 330 из 600
Последний прогон: 207 narrow ok, начат 02.11 14:17

Диск: 51868 МБ свободно
Память: 1363 МБ доступно"""


def test_status_гостю_без_машины():
    text = status.text("guest", FACTS, NOW)
    assert "Собрано за сегодня: 8250 объявлений 1-2 комнаты (50% от обычного дня)" in text
    assert text.endswith("сбор сейчас идёт")
    for leak in ("Диск:", "Память:", "Последний прогон", "корзина 330"):
        assert leak not in text


def test_status_база_не_ответила():
    f = status.Facts(seg=0, covered=None, run=None, fresh=None, disk=1, mem=None,
                     crawling=False, last_line="")
    text = status.text("owner", f, NOW)
    assert "Витрины: не ответили" in text
    assert "Последний прогон: неизвестен" in text
    assert "обход не идёт" in text
    assert "Свежесть витрин: неизвестна" in status.text("guest", f, NOW)


def test_status_строки_те_же_что_в_box_bot():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts" / "box_bot.sh").read_text()
    for line in ("Сегмент за сегодня: $seg из $NORM ($pct%)",
                 "Собрано за сегодня: $seg объявлений 1-2 комнаты ($pct% от обычного дня)",
                 'lock="идёт обход, последняя строка журнала:', 'lock="сбор сейчас идёт"',
                 "Диск: $disk МБ свободно", "Память: $mem МБ доступно", "NORM=16500"):
        assert line in src


def test_status_через_разбор_команды(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("NOTIFY_TG_CHAT=42\n")
    s = config.load({}, repo=tmp_path)
    monkeypatch.setattr(status, "gather", lambda settings: FACTS)
    notes = []
    for cmd in ("/status", "/статус", "/STATUS@rent_msk_asinc_bot"):
        replies = access.handle_message(s, "42", cmd, time.time(), time.time(), notes.append)
        assert replies[0].text.startswith("Состояние на ")


def test_кеш_свежести_не_ходит_в_базу_повторно(monkeypatch):
    calls = []
    monkeypatch.setattr(status.db, "scalar", lambda sql, params=None: calls.append(sql) or "сегодня")
    monkeypatch.setattr(status, "_fresh", (0.0, None))
    assert status.fresh() == "сегодня"
    assert status.fresh() == "сегодня"
    assert len(calls) == 1
