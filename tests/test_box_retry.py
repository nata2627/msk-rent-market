"""Добор после сбоя: отметка ставится сбоем сбора, снимается удачей, и
перезапуск идет только той же датой.

docker и box_daily.sh подменяются заглушками, скрипты копируются во
временный каталог, чтобы журналы, замок и отметки не ложились в настоящий
data/."""
import os
import shutil
import subprocess
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TODAY = date.today().isoformat()


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    (repo / "data" / "retry").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "scripts" / "box_retry.sh", repo / "scripts")
    sent = tmp_path / "sent"
    (repo / "scripts" / "box_notify.sh").write_text(f'#!/bin/bash\necho "$1|$2" >> {sent}\n')
    (repo / "scripts" / "box_seg.sh").write_text("#!/bin/bash\necho '100 из 200 (50%)'\n")
    return repo, sent


def fake_docker(tmp_path, probe_ok=True, collect_fails=False):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    calls = tmp_path / "calls"
    collect = (
        "echo '13:58:09 INFO Итог: корзин 202, пропущено собранных 0, недобрано в 66, "
        "переделено по недобору 0, не прочиталось 0, уникальных объявлений 4998, запросов 516, статус aborted'\n"
        "    echo '13:58:09 ERROR Площадка закрыла доступ (HTTP 200, запрос 516 сеанса за 52 мин, всего 516, «Captcha - база объявлений ЦИАН»). Сбор остановлен.'\n"
        "    echo '13:58:10 === Сбор — СБОЙ (код 1), иду дальше'; exit 1"
        if collect_fails else "echo '=== Сбор — готово'; exit 0")
    (bindir / "docker").write_text(f"""#!/bin/bash
echo "$*" >> {calls}
case "$*" in
  *box_probe.py*) {"echo 'проба: выдача далась'; exit 0" if probe_ok else "echo 'проба: выдача НЕ далась'; exit 1"} ;;
  *pg_isready*) exit 0 ;;
  *"count(distinct b.rooms)"*) echo 0 ;;
  *rent-collector*) {collect} ;;
esac
exit 0
""")
    (bindir / "docker").chmod(0o755)
    return dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
                RETRY_LAST_HOUR="25"), calls


def stub_daily(repo, tmp_path):
    started = tmp_path / "started"
    (repo / "scripts" / "box_daily.sh").write_text(f'#!/bin/bash\necho "$* $BOX_RETRY" >> {started}\n')
    (repo / "scripts" / "box_daily.sh").chmod(0o755)
    return started


def mark(repo, mode, day, minutes_ago, runs=0):
    f = repo / "data" / "retry" / mode
    f.write_text(day + "\n" + "".join(f"run 1{i}:00\n" for i in range(runs)))
    t = time.time() - minutes_ago * 60
    os.utime(f, (t, t))
    return f


def retry(repo, env):
    return subprocess.run(["bash", str(repo / "scripts" / "box_retry.sh")],
                          env=env, capture_output=True, text=True, timeout=60)


def read(p):
    return p.read_text() if p.exists() else ""


def test_без_отметки_площадку_не_трогает(tmp_path):
    repo, _ = make_repo(tmp_path)
    env, calls = fake_docker(tmp_path)
    assert retry(repo, env).returncode == 0
    assert read(calls) == ""


def test_вчерашняя_отметка_снимается_без_обхода(tmp_path):
    repo, sent = make_repo(tmp_path)
    env, calls = fake_docker(tmp_path)
    started = stub_daily(repo, tmp_path)
    f = mark(repo, "weekly", "2026-11-15", 600)
    retry(repo, env)
    assert not f.exists()
    assert read(started) == ""
    assert "box_probe" not in read(calls)
    assert "retry-late-weekly" in read(sent)


def test_пауза_после_сбоя(tmp_path):
    repo, _ = make_repo(tmp_path)
    env, calls = fake_docker(tmp_path)
    mark(repo, "weekly", TODAY, 10)
    retry(repo, env)
    assert "box_probe" not in read(calls)


def test_замок_занят(tmp_path):
    repo, _ = make_repo(tmp_path)
    env, calls = fake_docker(tmp_path)
    mark(repo, "weekly", TODAY, 90)
    (repo / "data" / "box.lock").mkdir()
    retry(repo, env)
    assert "box_probe" not in read(calls)


def test_проба_не_прошла_ждем(tmp_path):
    repo, _ = make_repo(tmp_path)
    env, _ = fake_docker(tmp_path, probe_ok=False)
    started = stub_daily(repo, tmp_path)
    f = mark(repo, "weekly", TODAY, 90)
    retry(repo, env)
    assert f.exists()
    assert read(started) == ""
    assert "НЕ далась" in read(repo / "data" / "logs" / "box_retry.log")


def test_проба_прошла_перезапуск_той_же_датой(tmp_path):
    repo, _ = make_repo(tmp_path)
    env, _ = fake_docker(tmp_path)
    started = stub_daily(repo, tmp_path)
    f = mark(repo, "weekly", TODAY, 90)
    retry(repo, env)
    assert read(started).split() == ["weekly", TODAY, "1"]
    assert "run " in f.read_text()


def test_срез_раньше_добора(tmp_path):
    repo, _ = make_repo(tmp_path)
    env, _ = fake_docker(tmp_path)
    started = stub_daily(repo, tmp_path)
    mark(repo, "weekly", TODAY, 90)
    mark(repo, "narrow", TODAY, 90)
    retry(repo, env)
    assert read(started).split()[0] == "narrow"


def test_предел_перезапусков(tmp_path):
    repo, sent = make_repo(tmp_path)
    env, calls = fake_docker(tmp_path)
    started = stub_daily(repo, tmp_path)
    f = mark(repo, "weekly", TODAY, 90, runs=6)
    retry(repo, env)
    assert not f.exists()
    assert read(started) == ""
    assert "retry-gaveup-weekly" in read(sent)


def test_поздно_не_начинает(tmp_path):
    repo, _ = make_repo(tmp_path)
    env, calls = fake_docker(tmp_path)
    env["RETRY_LAST_HOUR"] = "0"
    f = mark(repo, "weekly", TODAY, 90)
    retry(repo, env)
    assert f.exists()
    assert "box_probe" not in read(calls)


# Сам прогон: ставит и снимает отметку, говорит о сбое добора своими словами.

def run_daily(tmp_path, mode, collect_fails, retry_env=False):
    repo, sent = make_repo(tmp_path)
    shutil.copy(ROOT / "scripts" / "box_daily.sh", repo / "scripts")
    (repo / "scripts" / "box_marts.sh").write_text("#!/bin/bash\necho 'Done.'\n")
    (repo / "scripts" / "box_marts.sh").chmod(0o755)
    (repo / "scripts" / "box_numbers.sh").write_text("#!/bin/bash\n")
    (repo / "scripts" / "box_numbers.sh").chmod(0o755)
    env, _ = fake_docker(tmp_path, collect_fails=collect_fails)
    if retry_env:
        env["BOX_RETRY"] = "1"
    r = subprocess.run(["bash", str(repo / "scripts" / "box_daily.sh"), mode, TODAY],
                       env=env, capture_output=True, text=True, timeout=60)
    return repo, sent, r


def test_сбой_сбора_ставит_отметку(tmp_path):
    repo, sent, _ = run_daily(tmp_path, "weekly", collect_fails=True)
    f = repo / "data" / "retry" / "weekly"
    assert f.read_text().strip() == TODAY
    msg = read(sent)
    assert msg.startswith("weekly-fail|Добор 3+ комнат оборван")
    assert "площадка закрыла доступ, запрос 516 сеанса за 52 мин, всего 516, «Captcha - база объявлений ЦИАН»." in msg
    assert "корзин 202, объявлений 4998" in msg
    assert "под угрозой" not in msg


def test_повторный_сбой_хранит_счет_попыток(tmp_path):
    repo, _ = make_repo(tmp_path)
    mark(repo, "narrow", TODAY, 90, runs=2)
    repo, sent, _ = run_daily(tmp_path, "narrow", collect_fails=True)
    f = repo / "data" / "retry" / "narrow"
    assert f.read_text().count("run ") == 2
    assert "collect-fail|Сбор упал" in read(sent)


def test_удачный_добор_снимает_отметку(tmp_path):
    repo, _ = make_repo(tmp_path)
    mark(repo, "weekly", TODAY, 90, runs=1)
    repo, sent, r = run_daily(tmp_path, "weekly", collect_fails=False, retry_env=True)
    assert not (repo / "data" / "retry" / "weekly").exists()
    assert "retry-ok|Добор после сбоя прошел" in read(sent)
