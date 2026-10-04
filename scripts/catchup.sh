#!/bin/bash
# Догон пропущенного обхода: та же дата снимка, только нужные комнатности.
#
# Нужен, когда плановый прогон упал или не состоялся, а день ещё не
# кончился. Несобранный день не вернуть ничем: цена, которую объявление
# показывало в четверг, нигде больше не хранится.
#
# Задваивания не будет. Ключ панели — источник, объявление, дата снимка, —
# и уже собранное просто не запишется второй раз. Поэтому догонять можно
# и после частичного обхода, не разбираясь, где он остановился.
#
#   scripts/catchup.sh 2026-10-04 1,2
#   scripts/catchup.sh            # сегодня, комнатности ежедневного среза
#
# Отличие от daily.sh одно, но важное: этот скрипт ЖДЁТ замок, а не
# выходит по нему. Догон ставят, уже зная, что данных не хватает, и тихий
# выход по занятому замку обессмыслил бы саму затею.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

SNAPSHOT_DATE="${1:-$(date +%F)}"
ROOMS="${2:-1,2}"

# Режим — это ярлык в raw.collection_run, нарезку задаёт --rooms. Но
# ярлык должен быть честным: 'narrow' означает ровно комнатности
# ежедневного среза, всё остальное — 'full'.
MODE="narrow"
case ",$ROOMS," in
  *,3,*|*,4,*|*,5,*|*,6,*|*,7,*|*,9,*) MODE="full" ;;
esac

# Не давать компьютеру уснуть, пока идёт обход. Подробно про грабли со
# сном — в daily.sh, там же про то, почему одного caffeinate мало.
if [ -z "${UNDER_CAFFEINATE:-}" ]; then
  export UNDER_CAFFEINATE=1
  exec caffeinate -i -s -m "$0" "$@"
fi

PY="$REPO/.venv/bin/python"
STAMP="$(date +%Y%m%d_%H%M)"
LOG_DIR="$REPO/data/logs"
LOG="$LOG_DIR/catchup_$STAMP.log"
mkdir -p "$LOG_DIR"
exec >>"$LOG" 2>&1

say() { echo "$(date +%H:%M:%S) === $*"; }

# Разовая задача launchd снимается сразу, не дожидаясь конца работы.
# RunAtLoad уже сработал, файл больше не нужен, а оставленный — выстрелит
# ещё раз при следующем входе в систему и затопчет свежие данные старой
# датой снимка. Удаляем в начале, чтобы это случилось даже если обход
# упадёт: задача, пережившая собственный сбой, опаснее несделанного
# догона.
if [ -n "${CATCHUP_PLIST:-}" ] && [ -f "$CATCHUP_PLIST" ]; then
  rm -f "$CATCHUP_PLIST" && say "Разовая задача launchd снята ($CATCHUP_PLIST)"
fi

say "Начало догона, дата снимка $SNAPSHOT_DATE, комнатности $ROOMS, режим $MODE"

# Питание от сети. Причина проверки — в daily.sh.
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
    say "ОШИБКА: за $AC_WAIT_MIN мин питание от сети не появилось. Догон отменён."
    echo "$(date +%F\ %H:%M)  сбоев: 1  догон отменён, питание от батареи  [догон]" \
      >> "$LOG_DIR/daily_summary.log"
    exit 1
  fi
fi

# Замок тот же, что у планового прогона: второй браузер в том же профиле
# сломает сессию обоим. Здесь мы ждём, а не выходим.
LOCK="$REPO/data/daily.lock"
LOCK_WAIT_MIN=180
GOT_LOCK=0
for i in $(seq 1 "$LOCK_WAIT_MIN"); do
  if mkdir "$LOCK" 2>/dev/null; then GOT_LOCK=1; break; fi
  [ "$i" = 1 ] && say "Идёт другой прогон, жду освобождения замка"
  sleep 60
done
if [ "$GOT_LOCK" = 0 ]; then
  say "ОШИБКА: замок не взялся за $LOCK_WAIT_MIN мин. Догон отменён."
  echo "$(date +%F\ %H:%M)  сбоев: 1  догон отменён, замок занят  [догон]" \
    >> "$LOG_DIR/daily_summary.log"
  exit 1
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT
say "Замок взят"

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
  say "ОШИБКА: база так и не поднялась. Догон отменён."
  echo "$(date +%F\ %H:%M)  сбоев: 1  догон отменён, база не поднялась  [догон]" \
    >> "$LOG_DIR/daily_summary.log"
  exit 1
fi

FAILED=0
step() {
  local name="$1"; shift
  say "$name"
  if "$@"; then say "$name — готово"; else say "$name — СБОЙ (код $?), иду дальше"; FAILED=$((FAILED+1)); fi
}

step "Догон"         "$PY" -m etl.collect --mode "$MODE" --transport browser \
                       --snapshot-date "$SNAPSHOT_DATE" --rooms "$ROOMS"
step "Дедупликация"  "$PY" -m src.dedup
step "Витрины"       docker compose exec -T postgres \
                       psql -q -U "${POSTGRES_USER:-rent}" -d "${POSTGRES_DB:-rent}" \
                       -c "select marts.refresh_all();"

say "Конец"

find "$LOG_DIR" -name 'catchup_*.log' -mtime +60 -delete 2>/dev/null

SUMMARY="$(grep -E 'Итог:' "$LOG" | tail -1 | sed 's/^[0-9:]* INFO //')"
echo "$(date +%F\ %H:%M)  сбоев: $FAILED  ${SUMMARY:-догон не выполнялся}  [догон $SNAPSHOT_DATE комнат $ROOMS]" \
  >> "$LOG_DIR/daily_summary.log"

exit $((FAILED > 0 ? 1 : 0))
