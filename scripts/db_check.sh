#!/bin/bash
# Проверка целостности базы: каждая таблица raw, core и public по
# страницам (verify_heapam, с TOAST) и каждый индекс btree (bt_index_check).
# Только чтение.
#
#   scripts/db_check.sh            # печать итога, код 1 при повреждении
#   scripts/db_check.sh --notify   # то же, и тревога в телеграм при сбое
#
# Зачем. 25.11.2026 одна страница core.offer_photo оказалась испорчена.
# Контрольных сумм страниц тогда не было, и Postgres заметил порчу только
# когда наткнулся на бессмыслицу в заголовке строки: в 20:45 на ней упала
# полная копия, а в 12:53 следующего дня ежедневный сбор. Ни диск (SMART
# чистый), ни журналы причину не назвали. Проверка раз в сутки перед
# копией ловит такую страницу в тот же вечер, с именем таблицы и номером
# страницы, а не через непонятную ошибку на другом шаге.
#
# marts не проверяются: dbt пересоздает их каждый день.
#
# Порча страницы роняет verify_heapam целиком, а не строкой отчета (так
# было 26.11 с MultiXactId), поэтому каждая таблица проверяется отдельным
# вызовом, и упавший вызов тоже считается находкой.

set -uo pipefail
export TZ=Europe/Moscow
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PG="${PG_CONTAINER:-rent_pg}"
NOTIFY=0
[ "${1:-}" = "--notify" ] && NOTIFY=1

# Без -i и с </dev/null: внутри цикла while read docker exec -i съедал
# весь оставшийся список таблиц, и проверка за секунду смотрела одну.
q() { docker exec "$PG" psql -U rent -d rent -qAtX -v ON_ERROR_STOP=1 -c "$1" </dev/null 2>&1; }

q "create extension if not exists amcheck" >/dev/null || {
  echo "amcheck недоступен"; exit 2; }

SCHEMAS="'raw','core','public'"
BAD=()
NT=0
NI=0
T0=$(date +%s)

while read -r rel; do
  [ -n "$rel" ] || continue
  NT=$((NT + 1))
  out="$(q "select count(*) || ' ' || coalesce(min(format('страница %s строка %s, %s', blkno, offnum, msg)), '')
            from verify_heapam('$rel', check_toast := true)")"
  case "$out" in
    "0 ") ;;
    *) BAD+=("$rel: $(printf '%s' "$out" | head -n 1 | cut -c1-200)") ;;
  esac
done < <(q "select c.oid::regclass from pg_class c join pg_namespace n on n.oid = c.relnamespace
            where c.relkind in ('r', 'm') and n.nspname in ($SCHEMAS) order by 1")

while read -r idx; do
  [ -n "$idx" ] || continue
  NI=$((NI + 1))
  out="$(q "select bt_index_check('$idx')")"
  [ -z "$out" ] || BAD+=("$idx: $(printf '%s' "$out" | head -n 1 | cut -c1-200)")
done < <(q "select c.oid::regclass from pg_class c join pg_namespace n on n.oid = c.relnamespace
            join pg_am a on a.oid = c.relam
            where c.relkind = 'i' and a.amname = 'btree' and n.nspname in ($SCHEMAS) order by 1")

SECS=$(( $(date +%s) - T0 ))
echo "Проверено таблиц $NT, индексов $NI"
if [ "${#BAD[@]}" = 0 ]; then
  echo "База цела, проверка заняла ${SECS} с"
  exit 0
fi

REPORT="$(printf '%s\n' "${BAD[@]}" | head -n 10)"
echo "Найдено повреждений: ${#BAD[@]}"
echo "$REPORT"
if [ "$NOTIFY" = 1 ]; then
  bash "$REPO/scripts/box_notify.sh" db-corrupt "База повреждена
$(date '+%d.%m, %H:%M')

$REPORT

Сбор и копия могут падать на этих местах. Починка в docs/operations.md, раздел «Испорченная страница»." >/dev/null 2>&1
fi
exit 1
