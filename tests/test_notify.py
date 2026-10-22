"""Тесты доклада об ошибках наружу (scripts/box_notify.sh и вызовы из прогона).

Проверяется не красота кода, а два свойства, без которых доклад
бесполезен или вреден.

Первое: вызовы отправщика не вырезаны из скриптов. Дважды за два дня о
беде узнавали только потому, что спрашивали сами (21.10.2026 —
заклинивший обход держал замок девятнадцать часов, 12-22.10.2026 —
мёртвая копия базы в дашборде), и каждый раз всё было правильно записано
в журнал. Журнал без доклада наружу беду не предотвращает, поэтому
исчезновение вызова — это откат к той же потере дня, и он должен ронять
тесты.

Второе: отправщик не падает и не спамит. Уведомление не имеет права
стать новой причиной потерять день, а сторож ходит раз в двадцать минут,
то есть одна беда без ограничения частоты улетела бы семьдесят два раза
за сутки.

Сеть здесь не трогается вовсе: curl подменяется заглушкой в PATH.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOTIFY = ROOT / "scripts" / "box_notify.sh"

# Скрипты прогона и ключи событий, которые каждый из них обязан
# докладывать. Ключ — не украшение: по нему работает ограничение частоты,
# и он же называет беду в журнале отправщика.
CALLERS = {
    "box_daily.sh": ["db-down", "collect-fail", "run-fails"],
    "box_watch.sh": ["disk-stop", "disk-low", "lock-stale", "lock-stuck", "probe-timeout"],
    "box_report.sh": ["pulse"],
}


def fake_curl(tmp_path, code="200", rc=0):
    """Заглушка curl: пишет свои аргументы в файл и отдаёт заданный код.

    Нужна, чтобы проверять ограничение частоты и поведение при отказе
    канала, ни разу не выйдя в сеть.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    calls = tmp_path / "calls.txt"
    (bindir / "curl").write_text(
        "#!/bin/bash\n"
        f'echo "$@" >> "{calls}"\n'
        f"printf '{code}'\n"
        f"exit {rc}\n"
    )
    (bindir / "curl").chmod(0o755)
    return bindir, calls


def run(args, tmp_path, *, bindir=None, env=None):
    """Запуск отправщика в песочнице: свой журнал, свои отметки, без .env."""
    e = dict(os.environ)
    for k in list(e):
        if k.startswith("NOTIFY_"):
            del e[k]
    # Пустой файл вместо настоящего .env: тесты не должны зависеть от
    # того, настроен ли канал на этой машине, и не должны им пользоваться.
    e["NOTIFY_ENV_FILE"] = str(tmp_path / "env.empty")
    (tmp_path / "env.empty").touch()
    e["NOTIFY_LOG_DIR"] = str(tmp_path / "log")
    e["NOTIFY_STATE_DIR"] = str(tmp_path / "state")
    if bindir:
        e["PATH"] = f"{bindir}:{e['PATH']}"
    e.update(env or {})
    return subprocess.run(
        ["bash", str(NOTIFY), *args], capture_output=True, text=True, env=e
    )


def notify_log(tmp_path):
    p = tmp_path / "log" / "box_notify.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ---------------------------------------------------------------------
# Отправщик на месте и синтаксически цел
# ---------------------------------------------------------------------
def test_sender_exists_and_is_executable():
    assert NOTIFY.exists(), "отправщик scripts/box_notify.sh исчез"
    assert os.access(NOTIFY, os.X_OK), "отправщик не исполняемый, cron его не запустит"


@pytest.mark.parametrize("name", ["box_notify.sh", *CALLERS])
def test_scripts_are_syntactically_valid(name):
    """bash читает файл по ходу исполнения, и опечатка в нём рвёт прогон
    посередине, а не на старте."""
    assert subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)]).returncode == 0


# ---------------------------------------------------------------------
# Вызовы не вырезаны из прогона
# ---------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(CALLERS))
def test_caller_invokes_the_sender(name):
    text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
    assert "box_notify.sh" in text, f"{name} перестал докладывать о беде наружу"


@pytest.mark.parametrize(
    "name,key",
    [(n, k) for n, keys in sorted(CALLERS.items()) for k in keys],
)
def test_каждое_событие_докладывается(name, key):
    """Каждая беда, о которой скрипт уже знает, уходит наружу под своим
    ключом. Ключи разные, потому что ограничение частоты считает по
    ключу: слить их в один значило бы заглушить вторую беду за окно."""
    text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
    assert re.search(rf"notify (--always )?{re.escape(key)}\b", text), (
        f"в {name} нет доклада о событии «{key}»"
    )


def test_каждая_тревога_сторожа_имеет_доклад():
    """Сторож кричит строками ВНИМАНИЕ и ОСТАНОВКА, и докладов наружу у
    него должно быть не меньше, чем таких строк.

    Проверка по счёту, а не по соседству строк: доклад о заклинившем
    замке сознательно уходит не в начале разбора, а по его итогу — там
    уже известно, сколько дня осталось несобранным. Но новая тревога,
    добавленная без доклада, этот счёт нарушит.
    """
    text = (ROOT / "scripts" / "box_watch.sh").read_text(encoding="utf-8")
    тревоги = re.findall(r'note "(?:ВНИМАНИЕ|ОСТАНОВКА)', text)
    доклады = re.findall(r"^\s*notify ", text, re.M)
    assert тревоги, "в сторожe не осталось строк ВНИМАНИЕ и ОСТАНОВКА — проверять нечего"
    assert len(доклады) >= len(тревоги), (
        f"тревог {len(тревоги)}, а докладов наружу {len(доклады)}: "
        "какая-то беда кричит только в журнал"
    )


def test_pulse_is_not_rate_limited():
    """Пульс идёт с --always: подавленный вечерний итог читался бы как
    беда с боксом, то есть ложная тревога вместо сигнала."""
    text = (ROOT / "scripts" / "box_report.sh").read_text(encoding="utf-8")
    assert re.search(r"notify --always pulse", text)


# ---------------------------------------------------------------------
# Отправщик молча не падает
# ---------------------------------------------------------------------
def test_без_настроек_возвращает_ноль(tmp_path):
    """Главное свойство: уведомление не должно становиться новой причиной
    потерять день. Канала нет — одна строка в журнал и ноль."""
    r = run(["проба", "беда такая-то"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""
    assert "канал не настроен" in notify_log(tmp_path)
    assert "беда такая-то" in notify_log(tmp_path)


def test_мёртвая_сеть_возвращает_ноль(tmp_path):
    bindir, _ = fake_curl(tmp_path, code="000", rc=6)
    r = run(["проба", "беда"], tmp_path, bindir=bindir,
            env={"NOTIFY_NTFY_URL": "https://example.invalid/topic"})
    assert r.returncode == 0, r.stderr
    assert "НЕ отправлено" in notify_log(tmp_path)


def test_отказ_канала_не_ставит_отметку(tmp_path):
    """Упавшая попытка не имеет права заглушить следующую на целое окно:
    иначе беда уходит в тишину ровно тогда, когда каналу нехорошо."""
    bindir, calls = fake_curl(tmp_path, code="500")
    env = {"NOTIFY_NTFY_URL": "https://example.invalid/topic"}
    run(["проба", "раз"], tmp_path, bindir=bindir, env=env)
    run(["проба", "два"], tmp_path, bindir=bindir, env=env)
    assert len(calls.read_text().strip().splitlines()) == 2


def test_пустое_сообщение_не_отправляется(tmp_path):
    bindir, calls = fake_curl(tmp_path)
    r = run(["проба", "   "], tmp_path, bindir=bindir,
            env={"NOTIFY_NTFY_URL": "https://example.invalid/topic"})
    assert r.returncode == 0
    assert not calls.exists()


# ---------------------------------------------------------------------
# Отправщик не спамит
# ---------------------------------------------------------------------
def test_одна_беда_за_окно_уходит_один_раз(tmp_path):
    """Сторож ходит раз в двадцать минут: без этого одна и та же строка
    улетела бы семьдесят два раза за сутки."""
    bindir, calls = fake_curl(tmp_path)
    env = {"NOTIFY_NTFY_URL": "https://example.invalid/topic"}
    for текст in ("беда раз", "беда два", "беда три"):
        assert run(["lock-stale", текст], tmp_path, bindir=bindir, env=env).returncode == 0
    assert len(calls.read_text().strip().splitlines()) == 1
    assert "подавлено" in notify_log(tmp_path)


def test_разные_события_друг_друга_не_глушат(tmp_path):
    bindir, calls = fake_curl(tmp_path)
    env = {"NOTIFY_NTFY_URL": "https://example.invalid/topic"}
    run(["lock-stale", "замок"], tmp_path, bindir=bindir, env=env)
    run(["disk-stop", "диск"], tmp_path, bindir=bindir, env=env)
    assert len(calls.read_text().strip().splitlines()) == 2


def test_always_обходит_ограничение(tmp_path):
    bindir, calls = fake_curl(tmp_path)
    env = {"NOTIFY_NTFY_URL": "https://example.invalid/topic"}
    run(["--always", "pulse", "итог один"], tmp_path, bindir=bindir, env=env)
    run(["--always", "pulse", "итог два"], tmp_path, bindir=bindir, env=env)
    assert len(calls.read_text().strip().splitlines()) == 2


def test_окно_настраивается(tmp_path):
    """Нулевое окно означает «не подавлять»: так проверяют канал, не
    дожидаясь шести часов."""
    bindir, calls = fake_curl(tmp_path)
    env = {"NOTIFY_NTFY_URL": "https://example.invalid/topic", "NOTIFY_WINDOW_H": "0"}
    run(["lock-stale", "раз"], tmp_path, bindir=bindir, env=env)
    run(["lock-stale", "два"], tmp_path, bindir=bindir, env=env)
    assert len(calls.read_text().strip().splitlines()) == 2


# ---------------------------------------------------------------------
# Секретов в репозитории нет
# ---------------------------------------------------------------------
def test_в_репозитории_нет_токенов():
    """Репозиторий публичный. В .env.example стоят только имена
    переменных с пустыми значениями, а в скриптах — ни одного значения."""
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for имя in ("NOTIFY_NTFY_URL", "NOTIFY_TG_TOKEN", "NOTIFY_TG_CHAT"):
        assert re.search(rf"^{имя}=\s*$", example, re.M), f"{имя} нет в .env.example или он не пуст"

    for name in ["box_notify.sh", *CALLERS]:
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        # Токен бота телеграма выглядит как 123456789:AA... — такого в
        # коде быть не может ни в каком виде, даже в примере.
        assert not re.search(r"\b\d{8,}:[A-Za-z0-9_-]{30,}", text), f"в {name} похоже на токен"
        # Топик ntfy — тоже секрет: кто его знает, тот в него пишет.
        # Заготовки в комментариях всегда в угловых скобках.
        assert not re.search(r"ntfy\.sh/(?!<)", text), f"в {name} похоже на топик ntfy"


def test_состояние_живёт_под_data():
    """Отметки частоты — рабочие данные, а не код: data/ целиком в
    .gitignore, и попасть в репозиторий они не могут."""
    text = NOTIFY.read_text(encoding="utf-8")
    assert "$REPO/data/notify" in text
    assert "data/" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_есть_цель_make_notify_test():
    """Проверка канала должна вызываться одной командой, иначе её не
    вызовут."""
    mk = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^notify-test:", mk, re.M)
    assert "box_notify.sh --test" in mk
