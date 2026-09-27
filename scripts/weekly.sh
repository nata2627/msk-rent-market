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

PY="$REPO/.venv/bin/python"
STAMP="$(date +%Y%m%d_%H%M)"
LOG_DIR="$REPO/data/logs"
LOG="$LOG_DIR/weekly_$STAMP.log"
mkdir -p "$LOG_DIR"
exec >>"$LOG" 2>&1

say() { echo "$(date +%H:%M:%S) === $*"; }

# Замок тот же, что у ежедневного прогона: второй браузер в том же
# профиле сломает сессию обоим. Но здесь мы ЖДЁМ, а не выходим: пропущенный
# недельный добор — это неделя без трёхкомнатных, а тихий выход по замку
# заметить нечем.
LOCK="$REPO/data/daily.lock"
for i in $(seq 1 120); do
  if mkdir "$LOCK" 2>/dev/null; then break; fi
  [ "$i" = 1 ] && say "Идёт другой прогон, жду освобождения замка"
  sleep 60
done
if ! mkdir "$LOCK" 2>/dev/null && [ ! -d "$LOCK" ]; then
  say "ОШИБКА: замок не взялся за два часа. Добор отменён."
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
