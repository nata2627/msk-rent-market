"""/status: собрано ли сегодня, свежи ли витрины, что с машиной."""
from __future__ import annotations

import time
from dataclasses import dataclass

from bot import db, machine


# Вердикт свежести считается представлением marts.data_freshness за
# полсекунды (максимум по marts.offer_scored), а меняется он раз в сутки,
# при пересборке витрин. Поэтому его держит кеш, который фоновый пульс
# обновляет каждые 30 с, и /status не платит эти полсекунды.
FRESH_TTL_S = 120
STALE_OK_S = 3600
_fresh: tuple[float, str | None] = (0.0, None)


def refresh_fresh() -> str | None:
    global _fresh
    value = db.scalar("select verdict from marts.data_freshness")
    if value is not None:
        _fresh = (time.monotonic(), value)
        return value
    # Пока витрины пересчитываются, последний известный вердикт вернее
    # пустоты: /status в эти минуты не должен говорить «база не ответила».
    # Но не дольше часа, иначе лежащая база выдавала бы вчерашнее «сегодня».
    at, last = _fresh
    return last if time.monotonic() - at < STALE_OK_S else None


def fresh() -> str | None:
    at, value = _fresh
    if value is not None and time.monotonic() - at < FRESH_TTL_S:
        return value
    return refresh_fresh()


@dataclass
class Facts:
    seg: int
    claimed: int | None
    covered: str | None
    run: str | None
    fresh: str | None
    disk: int
    mem: int | None
    crawling: bool
    last_line: str


SEG_SQL = """
    select count(distinct s.offer_id)
      from raw.offer_snapshot s
      join core.offer o using (source, offer_id)
     where s.snapshot_date = current_date and o.rooms_count in (1, 2)"""


# Заявленное ЦИАНом по верхним корзинам однушек и двушек, как в
# scripts/box_seg.sh. До 10.11.2026 здесь стояла норма 16 500, замер
# 13-21.10, и за месяц она устарела. Пока обход не дошел до двушек,
# заявлено только по однушкам, и собрано тоже: доля остается честной.
CLAIMED_SQL = """
    select sum(c) from (
      select max(b.claimed_count) c
        from raw.collection_run r join raw.bucket b using (run_id)
       where r.source = 'cian' and r.snapshot_date = current_date
         and b.depth = 0 and b.rooms in ('1', '2') and not b.pets_filter
         and b.district is null and b.okrug is null
         and b.price_min is null and b.price_max is null
       group by b.rooms) t"""


def gather_seg() -> int:
    """Собрано за сегодня по сегменту, однушки и двушки."""
    return db.scalar(SEG_SQL) or 0


def gather_claimed() -> int | None:
    """Сколько ЦИАН заявил сегодня по сегменту, None до начала обхода."""
    value = db.scalar(CLAIMED_SQL)
    return int(value) if value else None


def of_claimed(seg: int, claimed: int | None) -> str:
    if not claimed:
        return f"{seg}, заявленного ЦИАНом за день еще нет"
    return f"{seg} из {claimed} заявленных ЦИАНом ({seg * 100 // claimed}%)"


def gather(settings) -> Facts:
    return Facts(
        seg=gather_seg(),
        claimed=gather_claimed(),
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

Собрано за сегодня объявлений 1-2 комнаты: {of_claimed(f.seg, f.claimed)}
Покрытые комнатности: {f.covered or ''}
Свежесть витрин: {f.fresh or 'неизвестна'}

{lock}"""
    return f"""Состояние на {stamp}

Сегмент за сегодня: {of_claimed(f.seg, f.claimed)}
Покрытые комнатности: {f.covered or ''}
Витрины: {f.fresh or 'не ответили'}

{lock}
Последний прогон: {f.run or 'неизвестен'}

Диск: {f.disk} МБ свободно
Память: {'' if f.mem is None else f.mem} МБ доступно"""
