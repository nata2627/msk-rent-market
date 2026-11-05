"""Новый бот: строки паритета, которые не легли в тесты по командам.

Первый запуск с накопленным, ответ в чат спросившего, бот без канала,
правка экрана фильтров на месте. Телеграм подменяется записью вызовов.
"""
import asyncio
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

from bot import app, config
from bot.actions import Reply
from tests.test_pybot_cards import FakeBot, FakePhotos, settings  # noqa: F401


def update(uid, text=None, data=None):
    msg = SimpleNamespace(text=text) if text is not None else None
    cb = SimpleNamespace(data=data) if data is not None else None
    return SimpleNamespace(update_id=uid, message=msg, callback_query=cb)


class Backlog:
    def __init__(self, updates):
        self.updates, self.calls = updates, []

    async def get_updates(self, **kw):
        self.calls.append(kw)
        return [] if "offset" in kw else self.updates


def skip(settings, bot):  # noqa: F811
    asyncio.run(app.skip_backlog(SimpleNamespace(bot=bot, bot_data={"settings": settings})))


def test_первый_запуск_не_выполняет_старое(settings):  # noqa: F811
    bot = Backlog([update(10, text="/catchup"), update(11, data="x")])
    skip(settings, bot)
    assert app.offset_file(settings).read_text() == "12\n"
    # Телеграму подтверждено: накопленное больше не придет.
    assert bot.calls[-1]["offset"] == 12


def test_второй_запуск_ничего_не_пропускает(settings):  # noqa: F811
    skip(settings, Backlog([]))
    bot = Backlog([update(20, text="/status")])
    skip(settings, bot)
    assert bot.calls == []


class EditBot(FakeBot):
    async def edit_message_text(self, **kw):
        self.calls.append(("edit_text", kw))
        if self.edit_error:
            raise BadRequest(self.edit_error)


def run(bot, actions, chat="7"):
    ctx = SimpleNamespace(bot=bot, bot_data={"photos": FakePhotos({})})
    asyncio.run(app.execute(ctx, chat, actions))


def test_ответ_уходит_тому_кто_спросил():
    bot = EditBot()
    run(bot, [Reply("Ответ"), Reply("Фильтры подбора", [[("+10", "p+10")]], msg_id="55")])
    assert [kw["chat_id"] for _, kw in bot.calls] == ["7", "7"]


def test_экран_правится_на_месте():
    bot = EditBot()
    run(bot, [Reply("Фильтры подбора", [[("+10", "p+10")]], msg_id="55")])
    assert [c for c, _ in bot.calls] == ["edit_text"]
    assert bot.calls[0][1]["message_id"] == "55"


def test_экран_не_изменился_это_не_ошибка():
    bot = EditBot(edit_error="Message is not modified")
    run(bot, [Reply("Фильтры подбора", None, msg_id="55")])
    assert [c for c, _ in bot.calls] == ["edit_text"]


def test_экран_не_поправить_приходит_новый():
    bot = EditBot(edit_error="Message to edit not found")
    run(bot, [Reply("Фильтры подбора", None, msg_id="55")])
    assert [c for c, _ in bot.calls] == ["edit_text", "message"]


@pytest.mark.parametrize("env", ["", "NOTIFY_TG_CHAT=42\n", "NOTIFY_TG_TOKEN_DEV=dev:x\n"])
def test_без_настроенного_канала_бот_молчит(tmp_path, monkeypatch, capsys, env):
    (tmp_path / ".env").write_text(env)
    real = config.load
    monkeypatch.setattr(config, "load", lambda: real({}, repo=tmp_path))
    monkeypatch.setattr(app, "build", lambda s: pytest.fail("без канала опроса быть не должно"))
    assert app.main() == 1
    assert "канал не настроен" in capsys.readouterr().out
