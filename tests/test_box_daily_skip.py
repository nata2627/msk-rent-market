"""Узкий срез не повторяет то, что ночной полный обход уже собрал.

docker подменяется заглушкой: на вопрос о покрытии она отвечает числом
покрытых комнатностей, а запуск контейнера сбора записывает в журнал.
Скрипт копируется во временный каталог, чтобы журналы и замок прогона не
ложились в настоящий data/."""
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_daily(tmp_path, covered: str, mode="narrow"):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "box_daily.sh", repo / "scripts")
    (repo / "scripts" / "box_notify.sh").write_text("#!/bin/bash\nexit 0\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls"
    (bindir / "docker").write_text(f"""#!/bin/bash
echo "$*" >> {calls}
case "$*" in
  *pg_isready*) exit 0 ;;
  *"count(distinct b.rooms)"*) echo {covered} ;;
esac
exit 0
""")
    (bindir / "docker").chmod(0o755)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    r = subprocess.run(["bash", str(repo / "scripts" / "box_daily.sh"), mode, "2026-10-23"],
                       env=env, capture_output=True, text=True, timeout=60)
    log = next((repo / "data" / "logs").glob("box_*.log")).read_text()
    return r, log, calls.read_text() if calls.exists() else ""


def test_narrow_skipped_when_day_covered(tmp_path):
    r, log, calls = run_daily(tmp_path, "2")
    assert r.returncode == 0
    assert "уже покрыты" in log
    assert "rent-collector" not in calls


def test_narrow_runs_when_one_room_missing(tmp_path):
    _, log, calls = run_daily(tmp_path, "1")
    assert "уже покрыты" not in log
    assert "rent-collector" in calls


def test_full_is_never_skipped(tmp_path):
    _, log, calls = run_daily(tmp_path, "9", mode="full")
    assert "уже покрыты" not in log
    assert "rent-collector" in calls
