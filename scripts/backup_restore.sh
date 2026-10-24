#!/bin/bash
# Проверка и восстановление облачной копии базы. Запускается на макбуке:
# только здесь лежит закрытый ключ age.
#
#   scripts/backup_restore.sh check            # прочитать последнюю копию целиком
#   scripts/backup_restore.sh restore <база>   # развернуть в отдельную базу
#
# Как устроена копия, написано в scripts/box_backup.sh. Восстановление
# идет в два шага. Сначала последняя полная копия, затем ночные копии
# сырых строк, снятые после нее, по порядку дат. Прогоны из ночной копии
# заменяют одноименные целиком: удаление строки raw.collection_run уносит
# каскадом ее корзины, снимки и метки животных, и вставка кладет их
# заново. После этого core пересобирается из raw (make rebuild).
#
# Восстанавливает только в базу с другим именем, чем рабочая rent:
# затереть живую базу этой командой нельзя. Подменить рабочую базу
# восстановленной это отдельное решение.
#
# check ничего не пишет на диск: поток идет rclone cat | age -d прямо в
# pg_restore -f /dev/null, который читает архив до последнего байта, а
# ночные таблицы разжимаются и считаются по строкам.

set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
KEY="${BACKUP_AGE_KEY:-$HOME/.config/rent_backup/age.key}"
CONF="${RCLONE_CONFIG:-$HOME/.config/rent_backup/rclone.conf}"
REMOTE="${BACKUP_REMOTE:-gdrive:rent_msk_backup}"
RC=(rclone --config "$CONF" --log-level ERROR)
PGX=(docker compose exec -T postgres)
say() { echo "$(date +%H:%M:%S) === $*"; }
dec() { "${RC[@]}" cat "$1" | age -d -i "$KEY"; }

[ -f "$KEY" ] || { echo "нет закрытого ключа $KEY"; exit 1; }
FULL="$("${RC[@]}" lsf "$REMOTE/full" | sort | tail -1)"
[ -n "$FULL" ] || { echo "в $REMOTE/full нет ни одной копии"; exit 1; }
FULL_DATE="$(echo "$FULL" | sed 's/rent_\(.*\)\.dump\.age/\1/')"
DAYS="$("${RC[@]}" lsf --dirs-only "$REMOTE/daily" 2>/dev/null | tr -d / | sort)"

case "${1:-}" in
check)
  say "Полная копия $FULL"
  # Оглавление читается с начала архива, и pg_restore -l закрывает поток
  # раньше конца. Обрыв канала здесь ожидаемый, поэтому без pipefail.
  N="$(set +o pipefail; dec "$REMOTE/full/$FULL" | "${PGX[@]}" pg_restore -l | grep -c '^[0-9]')"
  dec "$REMOTE/full/$FULL" | "${PGX[@]}" pg_restore -f /dev/null
  say "архив читается целиком, объектов в оглавлении $N"
  LAST="$(echo "$DAYS" | tail -1)"
  [ -n "$LAST" ] || { say "ночных копий еще нет"; exit 0; }
  for t in collection_run bucket offer_snapshot pets_hit; do
    rows=$(( $(dec "$REMOTE/daily/$LAST/$t.csv.gz.age" | gunzip | wc -l) - 1 ))
    say "ночная $LAST raw.$t: $rows строк"
  done
  ;;
restore)
  DB="${2:?укажите имя базы, например rent_restore}"
  [ "$DB" != "rent" ] || { echo "в рабочую базу rent не восстанавливаю"; exit 1; }
  PSQL=("${PGX[@]}" psql -qtA -v ON_ERROR_STOP=1 -U rent)
  "${PSQL[@]}" -d postgres -c "drop database if exists $DB" -c "create database $DB"
  say "Полная копия $FULL в базу $DB"
  dec "$REMOTE/full/$FULL" | "${PGX[@]}" pg_restore --no-owner -U rent -d "$DB"
  for d in $DAYS; do
    [[ "$d" > "$FULL_DATE" ]] || continue
    say "Ночная копия $d"
    for t in collection_run bucket offer_snapshot pets_hit; do
      "${PSQL[@]}" -d "$DB" -c "drop table if exists tmp_$t; create unlogged table tmp_$t (like raw.$t)"
      dec "$REMOTE/daily/$d/$t.csv.gz.age" | gunzip \
        | "${PSQL[@]}" -d "$DB" -c "copy tmp_$t from stdin with (format csv, header)"
    done
    # Порядок вставки по ключу: корзина ссылается на родителя, снимок
    # на прежнюю полную карточку, и оба всегда старше ссылающегося.
    "${PSQL[@]}" -d "$DB" <<SQL
begin;
-- На прогон ссылается и панель, без каскада. core все равно
-- пересобирается из raw следующим шагом (make rebuild).
delete from core.offer_observation where run_id in (select run_id from tmp_collection_run);
delete from raw.collection_run where run_id in (select run_id from tmp_collection_run);
insert into raw.collection_run select * from tmp_collection_run order by run_id;
insert into raw.bucket select * from tmp_bucket order by bucket_id;
insert into raw.offer_snapshot select * from tmp_offer_snapshot order by snapshot_id;
insert into raw.pets_hit select * from tmp_pets_hit;
select setval(pg_get_serial_sequence('raw.collection_run', 'run_id'), (select max(run_id) from raw.collection_run));
select setval(pg_get_serial_sequence('raw.bucket', 'bucket_id'), (select max(bucket_id) from raw.bucket));
select setval(pg_get_serial_sequence('raw.offer_snapshot', 'snapshot_id'), (select max(snapshot_id) from raw.offer_snapshot));
drop table tmp_collection_run, tmp_bucket, tmp_offer_snapshot, tmp_pets_hit;
commit;
SQL
  done
  "${PSQL[@]}" -d "$DB" -c "select max(snapshot_date), count(*) from raw.offer_snapshot"
  say "raw восстановлен. Дальше core из raw: POSTGRES_DB=$DB make rebuild"
  ;;
*)
  sed -n 2,10p "$0"; exit 1 ;;
esac
