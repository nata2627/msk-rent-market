#!/bin/bash
# Сколько собрано по сегменту 1-2 комнат за день и сколько ЦИАН заявил.
#
# Зачем. До 10.11.2026 в сообщениях стояла норма 16 500, замер 13-21.10.
# Рынок за месяц сдвинулся, и «104% нормы» перестало что-либо значить.
# Заявленное ЦИАН показывает на каждой выдаче, сбор его записывает в
# raw.bucket, поэтому сравнивать надо с ним, тем же днем.
#
# Заявленное берется по верхним корзинам (комнатность без района, цены и
# фильтра животных), максимум за день: прогонов бывает несколько. Пока
# обход не дошел до двушек, заявлено только по однушкам, и собрано тоже.
#
#   box_seg.sh ДАТА          "17271 из 17520 заявленных ЦИАНом (98%)"
#   box_seg.sh ДАТА --nums   "17271 17520", пустое заявленное, если его нет

set -uo pipefail
DAY="${1:?дата снимка}"
PG_CONTAINER="${PG_CONTAINER:-rent_pg}"

read -r SEG CLAIMED < <(timeout 60 docker exec -i "$PG_CONTAINER" psql -qtA -F ' ' -U rent -d rent -c "
  select
    (select count(distinct s.offer_id)
       from raw.offer_snapshot s
       join core.offer o using (source, offer_id)
      where s.snapshot_date = '$DAY' and o.rooms_count in (1, 2)),
    (select sum(c) from (
       select max(b.claimed_count) c
         from raw.collection_run r join raw.bucket b using (run_id)
        where r.source = 'cian' and r.snapshot_date = '$DAY'
          and b.depth = 0 and b.rooms in ('1', '2') and not b.pets_filter
          and b.district is null and b.okrug is null
          and b.price_min is null and b.price_max is null
        group by b.rooms) t);" 2>/dev/null)

if [ "${2:-}" = --nums ]; then
  echo "${SEG:-} ${CLAIMED:-}"
elif [ -z "${SEG:-}" ]; then
  echo "? (база не ответила)"
elif [ -z "${CLAIMED:-}" ] || [ "$CLAIMED" = 0 ]; then
  echo "$SEG, заявленного ЦИАНом за день еще нет"
else
  echo "$SEG из $CLAIMED заявленных ЦИАНом ($(( SEG * 100 / CLAIMED ))%)"
fi
