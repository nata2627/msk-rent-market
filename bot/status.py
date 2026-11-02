"""/status: собрано ли сегодня, свежи ли витрины, что с машиной."""
from __future__ import annotations

import time
from dataclasses import dataclass

from bot import db, machine

# Норма сегмента за день, та же, что в box_watch.sh.
NORM = 16500


# Вердикт свежести считается представлением marts.data_freshness за
# полсекунды (максимум по marts.offer_scored), а меняется он раз в сутки,
# при пересборке витрин. Поэтому его держит кеш, который фоновый пульс
# обновляет каждые 30 с, и /status не платит эти полсекунды.
FRESH_TTL_S = 120
_fresh: tuple[float, str | None] = (0.0, None)


def refresh_fresh() -> str | None:
    global _fresh
    value = db.scalar("select verdict from marts.data_freshness")
    if value is not None:
        _fresh = (time.monotonic(), value)
    return value


def fresh() -> str | None:
    at, value = _fresh
    if value is not None and time.monotonic() - at < FRESH_TTL_S:
        return value
    return refresh_fresh()


@dataclass
class Facts:
    seg: int
    covered: str | None
    run: str | None
    fresh: str | None
    disk: int
    mem: int | None
    crawling: bool
    last_line: str


def gather(settings) -> Facts:
    return Facts(
        seg=db.scalar("""
            select count(distinct s.offer_id)
              from raw.offer_snapshot s
              join core.offer o using (source, offer_id)
             where s.snapshot_date = current_date and o.rooms_count in (1, 2)""") or 0,
        covered=db.scalar("""
            select coalesce(string_agg(distinct b.rooms, ', '), 'нет')
              from raw.collection_run r join raw.bucket b using (run_id)
             where r.source = 'cian' and r.status = 'ok'
               and r.snapshot_date = current_date and b.error is null"""),
        run=db.scalar("""
            select run_id || ' ' || mode || ' ' || status || ', начат ' ||
                   to_char(started_at at time zone 'Europe/Moscow', 'DD.MM HH24:MI')
              from raw.collection_run order by run_id desc limit 1"""),
        fresh=fresh(),
        disk=machine.disk_free_mb(settings.box_repo),
        mem=machine.avail_mb(),
        crawling=machine.crawl_running(settings.box_repo),
        last_line=machine.last_crawl_line(settings.box_repo),
    )


def text(role: str, f: Facts, now: time.struct_time | None = None) -> str:
    stamp = time.strftime("%d.%m, %H:%M", now or time.localtime())
    pct = f.seg * 100 // NORM
    if not f.crawling:
        lock = "обход не идёт"
    elif role == "owner":
        lock = f"идёт обход, последняя строка журнала: {f.last_line}"
    else:
        lock = "сбор сейчас идёт"
    # Гостю про данные, а не про машину: диск, память и номера прогонов
    # ему ничего не говорят, а показывают устройство чужой машины.
    if role != "owner":
        return f"""Состояние на {stamp}

Собрано за сегодня: {f.seg} объявлений 1-2 комнаты ({pct}% от обычного дня)
Покрытые комнатности: {f.covered or ''}
Свежесть витрин: {f.fresh or 'неизвестна'}

{lock}"""
    return f"""Состояние на {stamp}

Сегмент за сегодня: {f.seg} из {NORM} ({pct}%)
Покрытые комнатности: {f.covered or ''}
Витрины: {f.fresh or 'не ответили'}

{lock}
Последний прогон: {f.run or 'неизвестен'}

Диск: {f.disk} МБ свободно
Память: {'' if f.mem is None else f.mem} МБ доступно"""
