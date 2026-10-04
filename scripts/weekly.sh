#!/bin/bash
# Недельный добор комнатностей, которых нет в ежедневном срезе.
#
# Ежедневный прогон собирает одно- и двухкомнатные — искомый сегмент.
# Три, четыре, пять комнат, студии и свободная планировка нужны для
# картины рынка целиком, но каждый день их обходить незачем: это лишний
# час у площадки при том, что ищем мы не их.
#
# После добора отмечаются снятые с публикации — но только среди
# собранных комнатностей, см. db.mark_disappeared.
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
LOG="$LOG_DIR/weekly_$STAMP.log"
mkdir -p "$LOG_DIR"
exec >>"$LOG" 2>&1

say() { echo "$(date +%H:%M:%S) === $*"; }

# Не начинать обход от батареи. Почему caffeinate от этого не спасает и
# как выглядит такой сбой в журнале — в daily.sh, там разобран случай
# 04.10.2026. Добор терпимее к опозданию, чем ежедневный срез, но
# обойтись без шнура он не может ровно так же.
AC_WAIT_MIN=180

on_ac() {
  # Нет pmset — значит не macOS, и проверять нечего.
  command -v pmset >/dev/null 2>&1 || return 0
  [[ "$(pmset -g batt)" == *"'AC Power'"* ]]
}

if ! on_ac; then
  say "Питание от батареи, обход не начинаю. Жду шнур до $AC_WAIT_MIN мин."
  for _ in $(seq 1 "$AC_WAIT_MIN"); do
    sleep 60
    if on_ac; then say "Питание от сети появилось, продолжаю"; break; fi
  done
  if ! on_ac; then
    say "ОШИБКА: за $AC_WAIT_MIN мин питание от сети не появилось. Добор отменён."
    echo "$(date +%F\ %H:%M)  сбоев: 1  добор отменён, питание от батареи  [недельный]" \
      >> "$LOG_DIR/daily_summary.log"
    exit 1
  fi
fi

# Замок тот же, что у ежедневного прогона: второй браузер в том же
# профиле сломает сессию обоим. Но здесь мы ЖДЁМ, а не выходим: пропущенный
# недельный добор — это неделя без трёхкомнатных, а тихий выход по замку
# заметить нечем.
LOCK="$REPO/data/daily.lock"
LOCK_WAIT_MIN=120
GOT_LOCK=0
for i in $(seq 1 "$LOCK_WAIT_MIN"); do
  if mkdir "$LOCK" 2>/dev/null; then GOT_LOCK=1; break; fi
  [ "$i" = 1 ] && say "Идёт другой прогон, жду освобождения замка"
  sleep 60
done
# Флаг, а не повторный mkdir. Прежняя проверка спрашивала «не взялся И
# замка нет» — а когда замок держит живой чужой обход, он как раз есть,
# и добор шёл дальше БЕЗ замка, вторым браузером в том же профиле.
if [ "$GOT_LOCK" = 0 ]; then
  say "ОШИБКА: замок не взялся за $LOCK_WAIT_MIN мин. Добор отменён."
  echo "$(date +%F\ %H:%M)  сбоев: 1  добор отменён, замок занят  [недельный]" \
    >> "$LOG_DIR/daily_summary.log"
  exit 1
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

say "Начало добора, дата снимка $(date +%F)"

if ! docker info >/dev/null 2>&1; then
  say "Демон Docker не отвечает, запускаю Docker Desktop"
  open -a Docker 2>/dev/null || say "не удалось запустить Docker Desktop"
  for i in $(seq 1 60); do
    docker info >/dev/null 2>&1 && { say "Демон Docker готов"; break; }
    sleep 5
  done
fi
for i in $(seq 1 30); do
  docker compose exec -T postgres pg_isready -q 2>/dev/null && break
  [ "$i" = 1 ] && { say "База не отвечает, поднимаю"; docker compose up -d; }
  sleep 10
done
if ! docker compose exec -T postgres pg_isready -q 2>/dev/null; then
  say "ОШИБКА: база так и не поднялась. Добор отменён."
  exit 1
fi

FAILED=0
step() {
  local name="$1"; shift
  say "$name"
  if "$@"; then say "$name — готово"; else say "$name — СБОЙ (код $?), иду дальше"; FAILED=$((FAILED+1)); fi
}

# Комнатности, которых нет в ежедневном срезе: 3-6 комнат, 7 — студия,
# 9 — свободная планировка. Список зеркалит config.NARROW_ROOM_CODES:
# там 1 и 2, здесь всё остальное.
step "Добор комнатностей" "$PY" -m etl.collect --mode full --transport browser \
                             --rooms 3,4,5,6,7,9
step "Дедупликация" "$PY" -m src.dedup
step "Витрины"      docker compose exec -T postgres \
                      psql -q -U "${POSTGRES_USER:-rent}" -d "${POSTGRES_DB:-rent}" \
                      -c "select marts.refresh_all();"

say "Конец"
find "$LOG_DIR" -name 'weekly_*.log' -mtime +90 -delete 2>/dev/null

SUMMARY="$(grep -E 'Итог:' "$LOG" | tail -1 | sed 's/^[0-9:]* INFO //')"
echo "$(date +%F\ %H:%M)  сбоев: $FAILED  ${SUMMARY:-добор не выполнялся}  [недельный]" \
  >> "$LOG_DIR/daily_summary.log"

exit $((FAILED > 0 ? 1 : 0))
