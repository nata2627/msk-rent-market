"""Новый бот: экран фильтров, /filter текстом, /undo.

Повторяет проверки фильтров из test_bot.py и сверяет клавиатуру и тексты
со старым ботом. База подменяется, в сеть тесты не ходят.
"""
import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from bot import access, config, db, filters
from bot.actions import Answer, Reply
from tests.test_pybot_cards import OLD, base, settings  # noqa: F401


@pytest.fixture
def pick(base, monkeypatch):  # noqa: F811
    """Поверх базы подбора: число подходящих и справочник районов."""
    counted = []

    def scalar(sql, params=None):
        if "percentile_cont" in sql:
            return 77
        counted.append((sql, params))
        return 123

    plain = db.query

    def query(sql, params=None):
        if sql == filters.DISTRICTS_SQL:
            return [("Хамовники",), ("Марьина Роща",)]
        return plain(sql, params)

    monkeypatch.setattr(db, "scalar", scalar)
    monkeypatch.setattr(db, "query", query)
    return SimpleNamespace(counted=counted)


def ask(settings, text, chat="42"):  # noqa: F811
    t = time.time()
    return access.handle_message(settings, chat, text, t, t, lambda s: None)


def press(settings, data, chat="42", msg="500"):  # noqa: F811
    return access.handle_button(settings, chat, data, msg, lambda s: None)


def saved(settings, chat="42"):  # noqa: F811
    p = filters.path(settings, chat)
    return p.read_text() if p.exists() else ""


def write(settings, body, prev=False):  # noqa: F811
    p = (filters.prev_path if prev else filters.path)(settings, "42")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def text_of(actions):
    return "\n".join(a.text for a in actions if isinstance(a, Reply))


def screen_of(actions):
    return next(a for a in actions if isinstance(a, Reply) and a.keyboard)


def answer_of(actions):
    return next(a.text for a in actions if isinstance(a, Answer))


# ---------------------------------------------------------------------
# Текстом, те же случаи, что у старого бота
# ---------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "90; drop table x", "'; drop", "9 0", "abc", "", "-5", "1e3", "٩٠", "９０",
])
def test_числовой_фильтр_не_принимает_мусор(settings, pick, value):
    ask(settings, f"/filter цена {value}")
    assert "price=" not in saved(settings)
    assert not any("drop" in str(c).lower() for c in pick.counted)


def test_число_в_границах_сохраняется(settings, pick):
    out = ask(settings, "/filter цена 90")
    assert "price=90" in saved(settings)
    assert text_of(out) == "Принято: price = 90\nСейчас отбор: цена до 90 тыс"


@pytest.mark.parametrize("value", ["90000", "5", "1000"])
def test_число_вне_границ_отвергается(settings, pick, value):
    out = ask(settings, f"/filter цена {value}")
    assert "price=" not in saved(settings)
    assert text_of(out) == "Не похоже на правду: цена в тысячах, от 10 до 999"


def test_район_сверяется_со_справочником(settings, pick):
    out = ask(settings, "/filter район Выдуманный'; drop table x; --")
    assert "district=" not in saved(settings)
    assert "Района «Выдуманный'; drop table x; --» не знаю" in text_of(out)


def test_район_с_пробелом_берется_хвостом(settings, pick):
    ask(settings, "/filter район Марьина Роща")
    assert "district=Марьина Роща" in saved(settings)
    # В запрос район уходит параметром.
    cond, params = filters.where(filters.load(settings, "42"))
    assert "district = %s" in cond and "Марьина Роща" in params


def test_фильтр_переживает_перезапуск(settings, pick):
    ask(settings, "/filter комнаты 1,2")
    ask(settings, "/filter площадь 40")
    assert filters.load(settings, "42") == {"rooms": "1,2", "area": "40"}


def test_сброс_убирает_всё(settings, pick):
    ask(settings, "/filter цена 90")
    out = ask(settings, "/filter сброс")
    assert saved(settings) == ""
    assert text_of(out).startswith("Фильтры сброшены.")


def test_ключ_без_учета_регистра_и_русские_имена(settings, pick):
    ask(settings, "/ФИЛЬТР Метро 15")
    ask(settings, "/отбор животные договор")
    assert filters.load(settings, "42") == {"metro": "15", "pets": "negotiable"}


@pytest.mark.parametrize("value", ["6", "1,2,3,4", "1;2", "0", "1,,2"])
def test_комнаты_только_по_образцу(settings, pick, value):
    out = ask(settings, f"/filter комнаты {value}")
    assert "rooms=" not in saved(settings)
    assert text_of(out).startswith("Комнатность — числа от 1 до 5")


def test_снять_один_фильтр(settings, pick):
    ask(settings, "/filter цена 90")
    ask(settings, "/filter метро 15")
    out = ask(settings, "/filter цена -")
    assert filters.load(settings, "42") == {"metro": "15"}
    assert text_of(out) == "Фильтр «price» убран.\nСейчас отбор: метро до 15 мин"


def test_неизвестный_фильтр(settings, pick):
    assert text_of(ask(settings, "/filter этаж 3")) == "Такого фильтра нет. Список — /filter"


def test_справка_текстом_дословно(settings, pick):
    old = OLD.split('reply "Фильтры текстом\n')[1].split('" ;;')[0]
    assert text_of(ask(settings, "/filter текстом")) == "Фильтры текстом\n" + old


def test_фильтры_у_гостя_свои(settings, pick):
    ask(settings, "/filter цена 90")
    ask(settings, "/filter цена 150", chat="7")
    assert filters.load(settings, "42")["price"] == "90"
    assert filters.load(settings, "7")["price"] == "150"


# ---------------------------------------------------------------------
# /undo
# ---------------------------------------------------------------------

def test_undo_без_прежних(settings, pick):
    for cmd in ("/undo", "/вернуть", "/filter вернуть"):
        assert text_of(ask(settings, cmd)) == "Прежних фильтров нет: фразой фильтры еще не менялись."


def test_undo_возвращает_прежние(settings, pick):
    filters.put(settings, "42", "price", "150")
    write(settings, "price=90\nrooms=1\n", prev=True)
    out = ask(settings, "/undo")
    assert text_of(out) == "Вернул прежние фильтры: цена до 90 тыс, комнаты 1"
    assert filters.load(settings, "42") == {"price": "90", "rooms": "1"}
    assert not filters.prev_path(settings, "42").exists()


# ---------------------------------------------------------------------
# Экран с кнопками
# ---------------------------------------------------------------------

def old_keyboard(price="", rooms="", area="", metro="", pets="", park="", mkad=""):
    body = OLD.split("  python3 -c '")[1].split("' \"$(filter_get price)\"")[0]
    out = subprocess.run(["python3", "-c", body, price, rooms, area, metro, pets, park, mkad],
                         capture_output=True, text=True, check=True)
    return [[(b["text"], b["callback_data"]) for b in row]
            for row in json.loads(out.stdout)["inline_keyboard"]]


@pytest.mark.parametrize("f", [
    {},
    {"price": "90", "rooms": "1,2"},
    {"rooms": "1", "area": "35", "metro": "10", "pets": "allowed", "park": "1"},
    {"rooms": "2", "metro": "20", "pets": "negotiable", "mkad": "1"},
    {"metro": "15", "pets": "forbidden"},
])
def test_клавиатура_как_у_старого(f):
    keys = ("price", "rooms", "area", "metro", "pets", "park", "mkad")
    assert filters.keyboard(f) == old_keyboard(*(f.get(k, "") for k in keys))


def test_экран_ставит_начальный_отбор(settings, pick):
    out = ask(settings, "/filter")
    scr = screen_of(out)
    assert filters.load(settings, "42") == {"rooms": "1,2", "price": "77"}
    assert scr.text.startswith("Фильтры подбора\n\nЦена: до 77 тыс\nКомнаты: 1,2\n")
    assert "Подходит: 123 вариантов" in scr.text
    assert "$ /filter цена 90" in scr.text
    assert scr.msg_id == ""


def test_экран_текстом_как_у_старого(settings, pick):
    filters.put(settings, "42", "pets", "allowed")
    old = OLD.split("filter_screen_text() {")[1].split("printf '%s' \"")[1].split('"\n}')[0]
    old = (old.replace("${lines:-", "").replace("качества.}", "качества.")
           .replace("${found:-?}", "123").replace("\\$", "$"))
    head, _, rest = old.partition("Пока ничего")
    assert filters.screen_text({}) == head + "Пока ничего" + rest
    assert filters.screen_text({"pets": "allowed"}).startswith(
        "Фильтры подбора\n\nЖивотные: можно\n\nПодходит: 123")


def test_кнопка_правит_экран_на_месте(settings, pick):
    filters.put(settings, "42", "price", "90")
    out = press(settings, "p-10", msg="777")
    assert answer_of(out) == "цена до 80 тыс"
    assert screen_of(out).msg_id == "777"
    assert filters.load(settings, "42")["price"] == "80"


@pytest.mark.parametrize("start,data,times,want", [
    ("20", "p-10", 5, "10"), ("990", "p+10", 3, "999"),
    ("15", "a-5", 4, "10"), ("295", "a+5", 3, "300"),
])
def test_шаг_не_уходит_за_границы(settings, pick, start, data, times, want):
    key = "price" if data[0] == "p" else "area"
    filters.put(settings, "42", key, start)
    for _ in range(times):
        press(settings, data)
    assert filters.load(settings, "42")[key] == want


def test_шаг_от_умолчания(settings, pick):
    write(settings, "rooms=1\n")
    press(settings, "p+10")
    press(settings, "a+5")
    f = filters.load(settings, "42")
    assert f["price"] == "110" and f["area"] == "45"


def test_состояние_животных_по_кругу(settings, pick):
    write(settings, "rooms=1\n")
    seen = [answer_of(press(settings, "e")) for _ in range(3)]
    assert seen == ["животные: можно", "животные: по договоренности", "животные: не важно"]
    # «Нельзя» из круга выводит, а не ставится.
    filters.put(settings, "42", "pets", "forbidden")
    press(settings, "e")
    assert "pets" not in filters.load(settings, "42")


@pytest.mark.parametrize("data,key,value,toast", [
    ("r1", "rooms", "1", "однокомнатные"), ("r12", "rooms", "1,2", "одно- и двухкомнатные"),
    ("r0", "rooms", None, "комнатность любая"), ("m15", "metro", "15", "метро до 15 минут"),
    ("m0", "metro", None, "метро не важно"), ("k", "park", "1", "парк в пределах километра"),
    ("d", "mkad", "1", "только внутри МКАД"),
])
def test_переключатели(settings, pick, data, key, value, toast):
    write(settings, "rooms=2\nmetro=10\n")
    out = press(settings, data)
    assert answer_of(out) == toast
    assert filters.load(settings, "42").get(key) == value


def test_парк_и_мкад_снимаются_повторным_нажатием(settings, pick):
    write(settings, "park=1\nmkad=1\n")
    assert answer_of(press(settings, "k")) == "парк не важен"
    assert answer_of(press(settings, "d")) == "МКАД не важен"
    assert filters.load(settings, "42") == {}


def test_сбросить_все_возвращает_начальный_отбор(settings, pick):
    """Как у старого: файл удаляется, экран заново ставит начальный отбор."""
    write(settings, "price=50\nmetro=10\n")
    out = press(settings, "x")
    assert answer_of(out) == "фильтры сброшены"
    assert filters.load(settings, "42") == {"rooms": "1,2", "price": "77"}


def test_еще_фильтры_присылает_новый_экран(settings, pick):
    out = press(settings, "filters", msg="777")
    assert answer_of(out) == "фильтры"
    assert screen_of(out).msg_id == ""


@pytest.mark.parametrize("data", ["rm -rf /", "p-10; id", "x;reset", "' or 1=1", "эксплойт",
                                  "p-100", "r3"])
def test_неизвестная_кнопка_ничего_не_меняет(settings, pick, data):
    write(settings, "price=90\n")
    out = press(settings, data)
    assert answer_of(out) == "не знаю такой кнопки"
    assert saved(settings) == "price=90\n"


def test_кнопки_экрана_есть_в_белом_списке(settings, pick):
    """Каждая кнопка экрана что-то делает, ни одна не отвечает отказом."""
    write(settings, "price=90\n")
    for row in filters.keyboard({}):
        for _, data in row:
            if data in ("noop", "s"):
                continue
            assert answer_of(press(settings, data)) != "не знаю такой кнопки", data


def test_callback_data_в_пределах_64_байт():
    for row in filters.keyboard({"price": "999", "area": "300", "pets": "negotiable"}):
        for text, data in row:
            assert len(data.encode()) <= 64 and text.strip()
