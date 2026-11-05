"""Что обработчик просит сделать в телеграме.

Обработчики работают в потоке и в сеть не ходят: они возвращают список
действий, а исполняет его app. Так логику можно проверять без телеграма.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from bot.config import Settings

# Клавиатура: строки кнопок (надпись, callback_data).
Keyboard = list[list[tuple[str, str]]]


@dataclass
class Reply:
    """Сообщение; с msg_id правка прежнего на месте (экран фильтров)."""
    text: str
    keyboard: Keyboard | None = None
    msg_id: str = ""


@dataclass
class Answer:
    """Ответ на нажатие: гасит индикатор на кнопке, текст всплывает."""
    text: str | None = None


@dataclass
class Card:
    """Карточка подбора: новое сообщение или правка прежнего на месте.

    on_sent получает id сообщения, которое теперь держит карусель (новое,
    если правка не удалась), prefetch это фото следующей карточки.
    """
    caption: str
    keyboard: Keyboard
    photo: str = ""
    msg_id: str = ""
    on_sent: Callable[[str], None] | None = None
    prefetch: str = ""
    label: str = ""


Action = Reply | Answer | Card


@dataclass
class Ctx:
    settings: Settings
    chat: str
    role: str  # owner или guest; чужой сюда не доходит
    replies: list[Action] = field(default_factory=list)
    note: Callable[[str], None] = lambda text: None  # строка в журнал бота

    def reply(self, text: str, keyboard: Keyboard | None = None) -> None:
        self.replies.append(Reply(text, keyboard))

    def answer(self, text: str | None = None) -> None:
        self.replies.append(Answer(text))
