#!/bin/bash
# Резервная копия базы в Google Drive, зашифрованная, без временных файлов.
#
# Зачем. До 24.10.2026 копия базы была только на макбуке и снималась
# руками. Выйди Mac mini из строя, пропало бы все собранное после
# последнего ручного дампа, а пропущенный день панели не восстановить
# ничем.
#
# Что копируется.
#   full   раз в неделю полный pg_dump, хранятся два последних;
#   daily  каждую ночь сырые строки прогонов за последние три даты снимка
#          (raw.collection_run, bucket, offer_snapshot, pets_hit). Окно в
#          три дня, а не один: догон пропущенного дня пишет в прошлую дату.
# Витрины и core пересобираются из raw (etl/rebuild_core.py), поэтому
# между полными копиями хватает сырых строк. Восстановление описано в
# scripts/backup_restore.sh.
#
# Шифрование. age открытым ключом из .env (BACKUP_AGE_RECIPIENT). Закрытый
# ключ лежит только на макбуке, так что ни Mac mini, ни Google копию не
# прочтут.
#
# Диск и память. Поток идет pg_dump | age | rclone rcat прямо в облако, на
# диск Mac mini не пишется ничего: свободно около 11 ГБ, а полный дамп
# 2,4 ГБ. rclone держит в памяти один кусок загрузки, 8 МБ. pg_dump
# работает внутри контейнера базы. Берется замок обхода, вместе с обходом
# копия не идет. Сроки под timeout: полная 3 часа, ночная 30 минут.
#
# Проверка. После загрузки md5 потока сверяется с md5, который считает
# Google. Расхождение или сбой уходят тревогой через box_notify.sh.
#
#   scripts/box_backup.sh          # ночная копия, полная, если на неделе ее нет
#   scripts/box_backup.sh full     # полная прямо сейчас

set -uo pipefail
export TZ=Europe/Moscow
export PATH="$HOME/bin:$PATH"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

LOG_DIR="$REPO/data/logs"
STATE="$REPO/data/backup_state"
mkdir -p "$LOG_DIR" "$STATE"
LOG="$LOG_DIR/backup_$(date +%Y%m%d_%H%M).log"
exec >>"$LOG" 2>&1
say() { echo "$(date +%H:%M:%S) === $*"; }
notify() { bash "$REPO/scripts/box_notify.sh" "$@" >/dev/null 2>&1; }
find "$LOG_DIR" -name 'backup_*.log' -mtime +60 -delete 2>/dev/null

RECIPIENT="$(grep -E '^BACKUP_AGE_RECIPIENT=' "$REPO/.env" | cut -d= -f2-)"
REMOTE="${BACKUP_REMOTE:-gdrive:rent_msk_backup}"
RC=(rclone --drive-chunk-size 8M --stats 0 --log-level NOTICE)

fail() {
  say "СБОЙ: $1"
  notify backup-fail "Резервная копия не сделана
$(date '+%d.%m, %H:%M')

$1. База на Mac mini цела, копия в облаке на прошлую дату."
  exit 1
}

[ -n "$RECIPIENT" ] || fail "в .env нет BACKUP_AGE_RECIPIENT"
command -v rclone >/dev/null && command -v age >/dev/null || fail "нет rclone или age в ~/bin"

TODAY="$(date +%F)"
WEEK="$(date +%G-W%V)"
MODE="${1:-auto}"
[ "$MODE" = "auto" ] && [ "$(cat "$STATE/daily" 2>/dev/null)" = "$TODAY" ] && exit 0

LOCK="$REPO/data/box.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  say "Идет обход, копия подождет следующей попытки"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# Поток в облако со сверкой md5. Первый аргумент срок в секундах, он
# ставится на источник и на загрузку. pipefail ловит упавший pg_dump, а не
# только упавший rclone.
upload() {
  local tmo="$1" dest="$2"; shift 2
  local sum="$STATE/upload.md5"
  timeout "$tmo" "$@" | age -r "$RECIPIENT" | tee >(md5sum | cut -d' ' -f1 >"$sum") \
    | timeout "$tmo" "${RC[@]}" rcat "$dest" || return 1
  sleep 2
  local theirs
  theirs="$("${RC[@]}" md5sum "$dest" | cut -d' ' -f1)"
  [ -n "$theirs" ] && [ "$theirs" = "$(cat "$sum")" ] || {
    say "md5 не сошелся: у нас $(cat "$sum"), в облаке ${theirs:-пусто}"; return 1; }
  say "$dest: $("${RC[@]}" size --json "$dest" | sed 's/.*"bytes":\([0-9]*\).*/\1/') байт, md5 сошелся"
}

# Сырые строки одной таблицы за окно прогонов, сжатые. Отдельным bash,
# потому что timeout запускает программу, а не функцию.
RAW_ROWS='docker exec -i rent_pg psql -qtA -v ON_ERROR_STOP=1 -U rent -d rent -c "$0" | gzip -6'

if [ "$MODE" = "full" ] || { [ "$(cat "$STATE/full" 2>/dev/null)" != "$WEEK" ] && [ "$(date +%u)" -ge 3 ]; }; then
  say "Полная копия, неделя $WEEK"
  upload 10800 "$REMOTE/full/rent_${TODAY}.dump.age" \
    nice -n 10 docker exec -i rent_pg pg_dump -U rent -d rent -Fc -Z 6 \
    || fail "полная копия не загрузилась, подробности в $(basename "$LOG")"
  echo "$WEEK" >"$STATE/full"
  # Хранятся две последние полные. Ночные старше более старой из них не
  # нужны: восстановление начинается с полной копии.
  "${RC[@]}" lsf "$REMOTE/full" | sort | head -n -2 | while read -r old; do
    "${RC[@]}" deletefile "$REMOTE/full/$old" && say "удалена старая полная $old"
  done
  OLDEST="$("${RC[@]}" lsf "$REMOTE/full" | sort | head -1 | sed 's/rent_\(.*\)\.dump\.age/\1/')"
  "${RC[@]}" lsf --dirs-only "$REMOTE/daily" 2>/dev/null | tr -d / | while read -r d; do
    [[ "$d" < "$OLDEST" ]] && "${RC[@]}" purge "$REMOTE/daily/$d" && say "удалена старая ночная $d"
  done
fi

say "Ночная копия сырых строк за три даты снимка до $TODAY"
RUNS="select run_id from raw.collection_run where snapshot_date >= date '$TODAY' - 2"
for t in collection_run bucket offer_snapshot pets_hit; do
  upload 1800 "$REMOTE/daily/$TODAY/$t.csv.gz.age" bash -o pipefail -c "$RAW_ROWS" \
    "copy (select * from raw.$t where run_id in ($RUNS)) to stdout with (format csv, header)" \
    || fail "ночная копия таблицы raw.$t не загрузилась"
done
echo "$TODAY" >"$STATE/daily"
say "Готово"
exit 0
