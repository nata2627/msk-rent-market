"""Тесты подстановки чисел в README.

Проверяется ровно то, ради чего команда заведена: что числа нельзя
потерять молча. Молчаливая потеря — это и есть исходная болезнь, от
которой лечимся, и повторить её внутри лекарства было бы обидно.
"""
from src.readme_numbers import apply, block


def test_value_between_marks_is_replaced():
    text = "В базе <!--n:offers-->1<!--/n--> объявлений."
    out, changed, missing = apply(text, {"offers": "38 990"})
    assert out == "В базе <!--n:offers-->38 990<!--/n--> объявлений."
    assert changed == 1 and missing == []


def test_unknown_mark_is_reported_not_skipped():
    """Метка без факта должна называться, а не молча остаться старой."""
    text = "<!--n:nope-->7<!--/n-->"
    out, changed, missing = apply(text, {"offers": "1"})
    assert missing == ["nope"]
    assert out == text and changed == 0


def test_text_outside_marks_is_untouched():
    text = "До. <!--n:a-->1<!--/n--> После."
    out, _, _ = apply(text, {"a": "2"})
    assert out.startswith("До. ") and out.endswith(" После.")


def test_same_value_is_not_counted_as_change():
    """Иначе «обновлено значений» всегда равнялось бы числу меток и
    ничего не сообщало бы."""
    text = "<!--n:a-->5<!--/n-->"
    _, changed, _ = apply(text, {"a": "5"})
    assert changed == 0


def test_multiline_block_keeps_table_on_its_own_line():
    """Таблица обязана начинаться с начала строки, иначе GitHub не видит
    в ней таблицу. Ошибка заметна только на отрисованной странице."""
    text = "<!--n:t-->старое<!--/n-->"
    out, _, _ = apply(text, {"t": block(["| a | b |", "|---|---|"])})
    assert "\n| a | b |\n" in out
    assert not out.startswith("<!--n:t-->| a |")


def test_one_mark_appearing_twice_is_updated_in_both_places():
    """Медиана однокомнатной стоит и в сверке с Росстатом, и в разделе
    про дубли. Разъехаться они не должны."""
    text = "<!--n:m-->1<!--/n--> ... <!--n:m-->2<!--/n-->"
    out, changed, _ = apply(text, {"m": "65 000"})
    assert out.count("65 000") == 2 and changed == 2
