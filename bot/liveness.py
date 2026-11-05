"""Живучесть долгого процесса.

Старый бот не мог зависнуть, потому что жил 55 секунд. Новый живет
сутками, и то же свойство держится иначе: каждый обработчик под сроком,
ошибка одного не роняет процесс, а заклинивший процесс сам выходит, чтобы
launchd (KeepAlive) или systemd поднял его заново.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import defaultdict
from pathlib import Path

from bot.commands import Reply

log = logging.getLogger("bot")

# GigaChat в /ask и подборе фразой ждется до 45 с, сверху запас на базу.
HANDLER_TIMEOUT_S = 60
PULSE_EVERY_S = 30
# Пульс старше трех минут значит, что цикл событий не крутится: шесть
# пропущенных ударов подряд случайностью не бывают.
PULSE_DEAD_S = 180

TIMEOUT_TEXT = """Не успела ответить

Команда думала дольше минуты, я ее бросила. Попробуйте позже."""
ERROR_TEXT = """Не получилось

Ошибка записана в журнал бота."""


async def guarded(fn, *args, timeout: float = HANDLER_TIMEOUT_S, label: str = "") -> list[Reply]:
    """Обработчик в отдельном потоке под сроком.

    Поток после срока не убить, он доработает сам (запрос к базе сам
    оборвется по statement_timeout). Главное, что цикл событий его не
    ждет, и остальные чаты и кнопки отвечают.
    """
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout)
    except TimeoutError:
        log.warning(f"не уложилось в {timeout:.0f} с: {label}")
        return [Reply(TIMEOUT_TEXT)]
    except Exception:
        # Ошибка одной команды не повод ронять процесс: человек получает
        # короткий ответ, подробности уходят в журнал.
        log.exception(f"ошибка: {label}")
        return [Reply(ERROR_TEXT)]


class ChatLocks:
    """По одному обработчику на чат за раз.

    Разные чаты не ждут друг друга, а нажатия одного человека идут по
    порядку: две быстрые кнопки листания, обработанные вперемешку,
    показали бы не ту карточку.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def __call__(self, chat: str) -> asyncio.Lock:
        return self._locks[chat]


class Pulse:
    """Удар раз в 30 с: файл для внешней проверки и сторож внутри процесса."""

    def __init__(self, path: Path, on_beat=None, exit_fn=os._exit) -> None:
        self.path = path
        self.on_beat = on_beat
        self.exit_fn = exit_fn
        self.last = time.monotonic()

    def beat(self) -> None:
        self.last = time.monotonic()
        self.path.write_text(f"{int(time.time())}\n")

    async def run(self) -> None:
        while True:
            self.beat()
            if self.on_beat:
                # Фоновая работа идет под тем же сроком, что и команды:
                # зависшая база не должна останавливать сам пульс.
                await guarded(self.on_beat, timeout=PULSE_EVERY_S, label="фон пульса")
            await asyncio.sleep(PULSE_EVERY_S)

    def dead(self, now: float | None = None) -> bool:
        return (now or time.monotonic()) - self.last > PULSE_DEAD_S

    def watch(self) -> None:
        """Сторож в отдельном потоке: заклинивший цикл событий его не держит.

        Выход с кодом 1, а не попытка починиться на месте: процесс, в
        котором встал цикл событий, ничему уже не обязан, а launchd и
        systemd поднимут чистый за секунды.
        """
        def loop() -> None:
            while True:
                time.sleep(PULSE_EVERY_S)
                if self.dead():
                    log.error(f"пульса нет {time.monotonic() - self.last:.0f} с, выхожу")
                    logging.shutdown()
                    self.exit_fn(1)
                    return
        threading.Thread(target=loop, name="pulse-watch", daemon=True).start()
