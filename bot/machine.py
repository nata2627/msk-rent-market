"""Сведения о машине сбора: диск, память, идет ли обход."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def disk_free_mb(path: Path) -> int:
    # То же, что столбец Available у df -Pm: место, доступное без root.
    return shutil.disk_usage(path).free // 2**20


def avail_mb() -> int | None:
    """Доступная память в МБ.

    На боксе (Linux) это MemAvailable. На макбуке, где сбор идет с
    29.10.2026, доступным считаются свободные, неактивные и спекулятивные
    страницы vm_stat: их система отдаст без выгрузки на диск.
    """
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
        return None
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    page = pages = 0
    for line in out.splitlines():
        if "page size of" in line:
            page = int(line.split("page size of")[1].split()[0])
        elif line.startswith(("Pages free:", "Pages inactive:", "Pages speculative:")):
            pages += int(line.split(":")[1].strip().rstrip("."))
    return pages * page // 2**20 if page else None


def crawl_running(box_repo: Path) -> bool:
    return (box_repo / "data" / "box.lock").is_dir()


# Журналы прогона: box_* на боксе, daily_* и mac_tail_* на макбуке, где
# сбор идет с 29.10.2026 (решение 7 от 02.11). Старый бот искал только
# box_*, и на макбуке /log отвечал, что журналов нет.
RUN_LOGS = ("box_2*.log", "daily_2*.log", "mac_tail_2*.log")


def latest_run_log(box_repo: Path) -> Path | None:
    folder = box_repo / "data" / "logs"
    logs = [p for pattern in RUN_LOGS for p in folder.glob(pattern)]
    return max(logs, key=lambda p: p.stat().st_mtime) if logs else None


def last_crawl_line(box_repo: Path) -> str:
    last = latest_run_log(box_repo)
    if last is None:
        return ""
    # Журнал обхода растет до мегабайт, а нужна одна строка: читаем хвост.
    try:
        with last.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4096))
            lines = f.read().decode(errors="replace").splitlines()
    except OSError:
        return ""
    return lines[-1][:80] if lines else ""
