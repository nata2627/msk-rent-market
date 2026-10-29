"""Сторож снаружи машины: когда пульс считается пропавшим и когда о нем говорить."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location("watchdog", Path(__file__).resolve().parents[1] / ".github" / "watchdog.py")
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def beat(ago):
    return int((NOW - ago).timestamp())


def test_свежий_пульс_молчит():
    assert wd.beat_problem(beat(timedelta(minutes=40)), NOW) == (None, False)


def test_нет_ветки_это_беда():
    text, loud = wd.beat_problem(0, NOW)
    assert text and loud


def test_первые_два_часа_громко_потом_раз_в_шесть():
    loud = {m: wd.beat_problem(beat(timedelta(minutes=m)), NOW)[1] for m in range(65, 60 * 20, 30)}
    assert all(loud[m] for m in loud if m <= 130)
    quiet = [m for m in loud if m > 130 and not loud[m]]
    noisy = [m for m in loud if m > 130 and loud[m]]
    assert quiet and noisy
    # Напоминание примерно раз в шесть часов, а не каждые полчаса
    assert len(noisy) <= 6
