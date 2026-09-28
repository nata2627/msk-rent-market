#!/bin/bash
# Ежедневный прогон панели. Запускается по расписанию через launchd,
# руками — тоже (./scripts/daily.sh).
#
# Почему именно launchd, а не cron. Сбор идёт через настоящий Chrome с
# видимым окном: в headless площадка отвечает 403. Значит нужен живой
# графический сеанс. launchd как раз запускает задачи пользователя в его
# сеансе, а пропущенный из-за сна компьютера запуск выполняет при
# пробуждении. cron на macOS для этого не годится.
#
# Порядок шагов не случайный. Сбор первый и единственный обязательный:
# несобранный день потерян навсегда, всё остальное пересчитывается из
# raw когда угодно. Поэтому дальше идёт цепочка, где падение любого
# шага записывается в журнал, но не мешает следующему.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

# Не давать компьютеру уснуть, пока идёт обход.
#
# 28.09.2026 плановый прогон за два часа прошёл 43 корзины из 294 вместо
# полутора часов на все. Провалы в журнале — 944, 956, 989, 1011 секунд —
# слово в слово совпали с циклами Maintenance Sleep в `pmset -g log`:
# в настройках стоит сон после минуты простоя, а счёт процессора сам по
# себе простоем быть не перестаёт. Браузер засыпал вместе с системой.
#
# Заметить это по журналу сбора невозможно: строки просто идут с
# получасовыми паузами, будто площадка задумалась.
#
# -i не даёт уснуть по простою, -s держит систему при питании от сети,
# -m не даёт заснуть диску. Закрытая крышка всё равно усыпит — это
# сильнее любых утверждений процесса.
if [ -z "${UNDER_CAFFEINATE:-}" ]; then
  export UNDER_CAFFEINATE=1
  exec caffeinate -i -s -m "$0" "$@"
fi

PY="$REPO/.venv/bin/python"
STAMP="$(date +%Y%m%d_%H%M)"
LOG_DIR="$REPO/data/logs"
LOG="$LOG_DIR/daily_$STAMP.log"
mkdir -p "$LOG_DIR"

exec >>"$LOG" 2>&1

say() { echo "$(date +%H:%M:%S) === $*"; }

# Один прогон за раз. Обход идёт около часа; если предыдущий ещё жив,
# второй браузер в том же профиле сломает сессию обоим.
LOCK="$REPO/data/daily.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  say "Предыдущий прогон ещё идёт ($LOCK). Выхожу."
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

say "Начало, дата снимка $(date +%F)"

# Сам демон Docker. Без него `docker compose up -d` не поднимет ничего:
# именно так потерялся день 26.09.2026 — launchd отработал в срок, но
# Docker Desktop не был запущен, и прогон час ждал базу впустую.
# Запускается через open: Docker Desktop — приложение, а не служба.
if ! docker info >/dev/null 2>&1; then
  say "Демон Docker не отвечает, запускаю Docker Desktop"
  open -a Docker 2>/dev/null || say "не удалось запустить Docker Desktop"
  for i in $(seq 1 60); do
    docker info >/dev/null 2>&1 && { say "Демон Docker готов"; break; }
    sleep 5
  done
fi

# Docker с автозапуском поднимается не мгновенно. Ждём базу, а не падаем.
for i in $(seq 1 30); do
  if docker compose exec -T postgres pg_isready -q 2>/dev/null; then break; fi
  [ "$i" = 1 ] && { say "База не отвечает, поднимаю"; docker compose up -d; }
  sleep 10
done
if ! docker compose exec -T postgres pg_isready -q 2>/dev/null; then
  say "ОШИБКА: база так и не поднялась. Прогон отменён."
  exit 1
fi

FAILED=0

step() {
  local name="$1"; shift
  say "$name"
  if "$@"; then
    say "$name — готово"
  else
    say "$name — СБОЙ (код $?), иду дальше"
    FAILED=$((FAILED + 1))
  fi
}

# Сбор. Единственный шаг, который нельзя нагнать задним числом.
# --no-collect пропускает его: так проверяется обвязка, не беспокоя площадку.
if [ "${1:-}" = "--no-collect" ]; then
  say "Сбор пропущен (--no-collect)"
else
  step "Сбор"          "$PY" -m etl.collect --mode narrow --transport browser
fi

# Всё дальше пересчитывается из raw в любой момент.
step "Справочник"      "$PY" -m etl.districts
step "Парки и МКАД"    "$PY" -m etl.parks
step "Дедупликация"    "$PY" -m src.dedup
step "Витрины"         docker compose exec -T postgres \
                         psql -q -U "${POSTGRES_USER:-rent}" -d "${POSTGRES_DB:-rent}" \
                         -c "select marts.refresh_all();"

say "Конец"

# Журналы старше двух месяцев не нужны: всё существенное лежит в raw.bucket.
find "$LOG_DIR" -name 'daily_*.log' -mtime +60 -delete 2>/dev/null

# Короткая сводка в отдельный файл — чтобы не листать часовой журнал.
SUMMARY="$(grep -E 'Итог:' "$LOG" | tail -1 | sed 's/^[0-9:]* INFO //')"
echo "$(date +%F\ %H:%M)  сбоев: $FAILED  ${SUMMARY:-сбор не выполнялся}" \
  >> "$LOG_DIR/daily_summary.log"

# Код возврата честный: launchd показывает его в `make schedule-status`,
# и он должен означать «что-то не сделалось», а не «grep ничего не нашёл».
exit $((FAILED > 0 ? 1 : 0))
