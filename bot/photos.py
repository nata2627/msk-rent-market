"""Фото объявлений для карточек.

Фото качает сам бот: по ссылке телеграм его у площадки не получит, ее
сервер не отвечает его фетчеру. Кеш на диске тот же, что был у прежнего бота:
при быстром листании сервер фото отвечал 429, а листание назад и
отложенное не должны качать картинку второй раз.

Сверху кеш file_id телеграма: однажды загруженное фото дальше
отправляется по нему, без повторной загрузки в телеграм.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

import httpx

log = logging.getLogger("bot")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
KEEP = 300  # около 20 МБ
MAX_BYTES = 10 * 1024 * 1024  # предел телеграма на фотографию
TIMEOUT_S = 10
# Повторять есть смысл только отказ по частоте: 404 значит, что фото
# снято вместе с объявлением.
RETRY_PAUSES = (1.0, 2.5)


class Photos:
    def __init__(self, folder: Path, client: httpx.AsyncClient | None = None) -> None:
        self.folder = folder
        self.client = client or httpx.AsyncClient(
            timeout=TIMEOUT_S, follow_redirects=True,
            headers={"User-Agent": UA, "Referer": "https://www.cian.ru/"})
        self.file_ids: dict[str, str] = {}
        self._inflight: dict[str, asyncio.Task] = {}

    def path(self, url: str) -> Path:
        return self.folder / (hashlib.md5(url.encode()).hexdigest() + ".jpg")

    async def get(self, url: str) -> bytes:
        """Картинка или пусто. Одновременные запросы одного адреса (показ и
        предзагрузка) качают ее один раз."""
        if not url:
            return b""
        key = self.path(url)
        if key.exists():
            key.touch()
            return key.read_bytes()
        task = self._inflight.get(url)
        if task is None:
            task = asyncio.create_task(self._download(url))
            self._inflight[url] = task
            task.add_done_callback(lambda _: self._inflight.pop(url, None))
        return await asyncio.shield(task)

    async def _download(self, url: str) -> bytes:
        for pause in (0, *RETRY_PAUSES):
            await asyncio.sleep(pause)
            try:
                async with self.client.stream("GET", url) as resp:
                    if resp.status_code == 429:
                        continue
                    if resp.status_code != 200:
                        return b""
                    blob = b""
                    async for chunk in resp.aiter_bytes():
                        blob += chunk
                        if len(blob) >= MAX_BYTES:
                            break
            except httpx.HTTPError as e:
                log.warning(f"фото не скачалось: {e.__class__.__name__}")
                return b""
            self._store(url, blob[:MAX_BYTES])
            return blob[:MAX_BYTES]
        return b""

    def _store(self, url: str, blob: bytes) -> None:
        if not blob:
            return
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            self.path(url).write_bytes(blob)
            old = sorted(self.folder.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
            for p in old[:-KEEP]:
                p.unlink(missing_ok=True)
        except OSError as e:
            # Кеш только ускоряет: без него карточка все равно уйдет.
            log.warning(f"кеш фото: {e}")

    def remember(self, url: str, message) -> None:
        """file_id из отправленного сообщения с фото."""
        photo = getattr(message, "photo", None)
        if url and photo:
            self.file_ids[url] = photo[-1].file_id
