"""Разметка ответа: тот же договор, что в box_notify.sh и box_tg.py.

Первая строка заголовок (жирным), строка с «$ » команда (моноширинной),
голая ссылка становится словом. Сообщения о беде рисует bash, ответы бот,
и выглядеть они должны одинаково: это проверяет тест.
"""
from __future__ import annotations

import html


def render(text: str) -> str:
    lines = text.rstrip("\n").split("\n")
    out = []
    for i, line in enumerate(lines):
        safe = html.escape(line)
        if i == 0:
            out.append(f"<b>{safe}</b>")
        elif line.startswith("$ "):
            out.append(f"<code>{html.escape(line[2:])}</code>")
        elif line.startswith(("http://", "https://")):
            # Ссылка на объявление с хвостом трекинга занимает четыре
            # строки и ничего не говорит, поэтому показывается словом.
            out.append(f'<a href="{html.escape(line, quote=True)}">Открыть объявление</a>')
        else:
            out.append(safe)
    return "\n".join(out)
