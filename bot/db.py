"""Одно соединение с базой на весь процесс, с переподключением.

Старый бот на каждый запрос поднимал docker exec psql, это и были
полсекунды из каждого ответа. Здесь соединение держится открытым, а
упавшее (база перезапущена, макбук проснулся) поднимается при следующем
запросе.
"""
from __future__ import annotations

import logging
import threading

import psycopg

from etl.config import DB

log = logging.getLogger("bot")

# При голоде по памяти Docker перестает отвечать вовсе, и бот не имеет
# права повиснуть вместе с ним. Срок тот же, что у старого q().
STATEMENT_TIMEOUT_MS = 30_000
CONNECT_TIMEOUT_S = 5

_conn: psycopg.Connection | None = None
_lock = threading.Lock()


def _connect() -> psycopg.Connection:
    return psycopg.connect(**DB, autocommit=True, connect_timeout=CONNECT_TIMEOUT_S,
                           options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")


def query(sql: str, params: list | tuple | None = None) -> list[tuple] | None:
    """Строки запроса или None, если база не ответила.

    None, а не исключение: ответ бота собирается из нескольких запросов,
    и упавший один не должен отнимать у человека остальные строки. Так же
    вел себя старый q(), отдававший пустоту.
    """
    global _conn
    with _lock:
        for attempt in (1, 2):
            try:
                if _conn is None or _conn.closed:
                    _conn = _connect()
                with _conn.cursor() as cur:
                    cur.execute(sql, params)
                    return cur.fetchall() if cur.description else []
            except psycopg.OperationalError as e:
                # Первая неудача часто значит лишь, что соединение умерло,
                # пока процесс спал. Вторая уже ответ базы.
                log.warning(f"база: {e.__class__.__name__}: {str(e).strip()[:200]}")
                if _conn is not None:
                    _conn.close()
                _conn = None
                if attempt == 2:
                    return None
            except psycopg.Error as e:
                log.warning(f"база: {e.__class__.__name__}: {str(e).strip()[:200]}")
                return None
    return None


def scalar(sql: str, params: list | tuple | None = None):
    rows = query(sql, params)
    return rows[0][0] if rows else None
