"""Настройки бота из окружения и .env.

Режим выбирает RENT_BOT_ENV, и по умолчанию это dev: два процесса на
одном токене отнимают друг у друга getUpdates (ошибка 409), и боевой бот
замолкает. Поэтому случайный запуск без переменной обязан уйти на
тестовый токен, а не на боевой.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values

REPO = Path(__file__).resolve().parents[1]

TOKEN_KEY = {"dev": "NOTIFY_TG_TOKEN_DEV", "prod": "NOTIFY_TG_TOKEN"}


@dataclass(frozen=True)
class Settings:
    env: str
    token: str
    owner: str
    guests: frozenset[str]
    repo: Path
    box_repo: Path
    state_dir: Path
    log_file: Path

    @property
    def dev(self) -> bool:
        return self.env == "dev"


def box_repo(repo: Path) -> Path:
    """Копия проекта, где идет сбор: в ней замок обхода и журналы прогона.

    В боевом режиме это сам repo. Тестовый бот живет в отдельной рабочей
    копии, и своего сбора у нее нет, а .env в ней ссылка на основную. По
    этой ссылке он смотрит на настоящий обход, только читая.
    """
    return (repo / ".env").resolve().parent if (repo / ".env").exists() else repo


def load(environ: Mapping[str, str] | None = None, repo: Path = REPO) -> Settings:
    # Переменная окружения сильнее .env, как у старого бота и отправщика:
    # так проверка подменяет значение, не трогая файл с секретами.
    values = {k: v or "" for k, v in dotenv_values(repo / ".env").items()}
    values.update(os.environ if environ is None else environ)

    env = values.get("RENT_BOT_ENV", "dev") or "dev"
    if env not in TOKEN_KEY:
        raise ValueError(f"RENT_BOT_ENV={env}: ожидается dev или prod")
    guests = frozenset(g.strip() for g in values.get("NOTIFY_TG_GUESTS", "").split(",")
                       if g.strip())
    # Состояние тестового бота отдельно от боевого: фильтры, отложенные и
    # отметка опроса у них свои, и проверка не должна портить боевые.
    state = "bot_dev" if env == "dev" else "bot"
    log = "bot_dev.log" if env == "dev" else "bot.log"
    return Settings(
        env=env,
        token=values.get(TOKEN_KEY[env], ""),
        owner=values.get("NOTIFY_TG_CHAT", "").strip(),
        guests=guests,
        repo=repo,
        box_repo=box_repo(repo),
        state_dir=repo / "data" / state,
        log_file=repo / "data" / "logs" / log,
    )
