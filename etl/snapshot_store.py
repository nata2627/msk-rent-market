"""Сырой снимок без повторов: полная карточка пишется, только если она
изменилась.

Зачем. С 13.10.2026 на машине сбора кончался диск: каждый день в
raw.offer_snapshot ложилась полная карточка каждого объявления, около
7,5 КБ, а свободно было 12,9 ГБ при общем с соседом диске. Замер 23.10:
после вычистки служебного шума 75,6% карточек совпадают с карточкой
того же объявления за прошлый день.

Как устроено. Строка снимка пишется каждый день, как и раньше: на ней
держатся все счетчики «сколько собрано за день», покрытие и снятия. Если
карточка без шума совпала с последней полной карточкой этого объявления,
в строке вместо payload лежит ссылка same_as на ту строку и время
подъема added_ts. raw по-прежнему только пополняется, старые строки не
трогаются.

Что считается шумом. Только то, что меняется от показа к показу и не
несет сведений о квартире: метки поиска в ссылках (context=,
mlSearchSessionGuid=), строка поиска qs, через которую объявление
нашлось, «2 часа назад» и «вчера», рекламные параметры, онлайн-статус
агента. Время подъема addedTimestamp сведение (агентство подняло
объявление), поэтому оно хранится отдельной колонкой и восстанавливается
точно.

Чего восстановление не возвращает. Шумовые поля восстановленной карточки
берутся из базового снимка, то есть ссылки несут вчерашние метки показа.
Разбор (etl/parse.py) этих полей не читает, кроме адреса объявления и
фото, а адрес пишется в core при первой встрече, когда карточка всегда
полная. Проверено на 4 днях настоящих снимков: scripts/check_snapshot_store.py.
"""
from __future__ import annotations

import hashlib
import json
import re

NOISE_TOP = ("contextToken", "humanizedTimedelta", "added", "adfoxParams",
             "searchDuplicatesRotationPromo", "addedTimestamp")
URL_RE = re.compile(r"^(https?://[^?]*)\?.*$")


def _clean(obj, top=False):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "qs" or (top and k in NOISE_TOP):
                continue
            out[k] = _clean(v)
        user = out.get("user") if top else None
        if isinstance(user, dict) and "agentAvailability" in user:
            out["user"] = {k: v for k, v in user.items() if k != "agentAvailability"}
        return out
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    if isinstance(obj, str):
        m = URL_RE.match(obj)
        return m.group(1) if m else obj
    return obj


def payload_hash(payload: dict) -> str:
    """Отпечаток карточки без шума. Ключи сортируются, чтобы порядок,
    в котором площадка отдала поля, не менял отпечаток."""
    text = json.dumps(_clean(payload, top=True), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.md5(text.encode()).hexdigest()


def restore(base: dict, added_ts) -> dict:
    """Карточка дня из базового снимка и собственного времени подъема."""
    out = dict(base)
    if added_ts is not None:
        out["addedTimestamp"] = added_ts
    else:
        out.pop("addedTimestamp", None)
    return out
