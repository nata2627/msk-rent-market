#!/bin/bash
# Включить контрольные суммы страниц в базе на Mac mini. Разовая операция.
#
#   scripts/box_checksums.sh
#
# Зачем. До 26.11.2026 база жила без них (data_checksums off). Испорченная
# страница core.offer_photo читалась как обычная, и Postgres заметил порчу
# только на бессмыслице в заголовке строки, через сутки и на другом шаге.
# С суммами любая порча страницы видна при первом же чтении ошибкой
# «invalid page verification», и ее ловят полная копия и db_check.sh.
#
# Включается только на остановленной базе (pg_checksums --enable), около
# 12 ГБ чтения и записи. Бот на это время не отвечает. Если pg_checksums
# прервется, кластер остается без сумм и рабочим, запуск можно повторить
# (так написано в документации Postgres). База запускается в любом случае,
# ловушкой на выходе.
#
# Ждет замок прогона до 02:00: в окно может попасть добор после сбоя.

set -uo pipefail
export TZ=Europe/Moscow
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PG=rent_pg
VOLUME=rent_msk_rent_pgdata
IMAGE="$(docker inspect -f '{{.Config.Image}}' "$PG")"
LOCK="$REPO/data/box.lock"
LOG="$REPO/data/logs/checksums_$(date +%Y%m%d_%H%M).log"
exec >>"$LOG" 2>&1
say() { echo "$(date +%H:%M:%S) === $*"; }
notify() { bash "$REPO/scripts/box_notify.sh" "$@" >/dev/null 2>&1; }

on() { docker exec "$PG" psql -U rent -d rent -qAtX -c "show data_checksums" </dev/null 2>/dev/null; }

if [ "$(on)" = on ]; then
  say "Контрольные суммы уже включены"
  exit 0
fi

DEADLINE=$(date -d 'tomorrow 02:00' +%s)
until mkdir "$LOCK" 2>/dev/null; do
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    say "Замок так и не освободился"
    notify checksums-fail "Контрольные суммы не включены
$(date '+%d.%m, %H:%M')

Замок прогона занят до 02:00. База не трогалась, работает как раньше."
    exit 1
  fi
  sleep 60
done

started=0
finish() {
  if [ "$started" = 1 ]; then
    docker start "$PG" >/dev/null
    for _ in $(seq 24); do
      docker exec "$PG" pg_isready -U rent -d rent -q 2>/dev/null && break
      sleep 5
    done
  fi
  rmdir "$LOCK" 2>/dev/null
}
trap finish EXIT

T0=$(date +%s)
say "Остановка базы, образ $IMAGE"
started=1
# Долгий срок на остановку: чистое завершение нужно pg_checksums, иначе он
# откажется работать с кластером.
docker stop -t 300 "$PG" >/dev/null || { say "база не остановилась"; exit 1; }

say "pg_checksums --enable"
docker run --rm -u 70:70 -v "$VOLUME":/var/lib/postgresql/data "$IMAGE" \
  pg_checksums --enable -D /var/lib/postgresql/data
CODE=$?

finish
started=0
trap - EXIT

STATE="$(on)"
SECS=$(( $(date +%s) - T0 ))
say "Итог: код $CODE, data_checksums ${STATE:-база не ответила}, ${SECS} с"
if [ "$CODE" = 0 ] && [ "$STATE" = on ]; then
  notify checksums-ok "Контрольные суммы страниц включены
$(date '+%d.%m, %H:%M')

База стояла ${SECS} с, работает. Порча любой страницы теперь видна при первом чтении."
else
  notify checksums-fail "Контрольные суммы не включены
$(date '+%d.%m, %H:%M')

pg_checksums код $CODE, data_checksums ${STATE:-база не ответила}. Журнал $(basename "$LOG")."
fi
