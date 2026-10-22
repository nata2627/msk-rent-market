#!/bin/bash
# Итог дня одной страницей: что собрано, что упало, что с машиной.
#
# Зачем отдельный скрипт, если есть журналы. Журналы пишутся по прогону,
# а прогонов в плохой день бывает десяток, и картина дня рассыпается по
# файлам. 12.10.2026 день собрался одиннадцатью заходами, и чтобы понять,
# что он всё-таки закрылся на 97%, пришлось складывать числа руками.
#
# Запускается из cron в конце суток, пишет в data/logs/box_report.log и
# туда же дописывает каждый следующий день. Файл маленький: страница в
# сутки.
#
#   scripts/box_report.sh              # за сегодня
#   scripts/box_report.sh 2026-10-13   # за конкретный день

set -uo pipefail

export TZ=Europe/Moscow

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

PG_CONTAINER="rent_pg"
DAY="${1:-$(date +%F)}"

LOG_DIR="$REPO/data/logs"
mkdir -p "$LOG_DIR"
REPORT="$LOG_DIR/box_report.log"

q() {
    docker exec -i "$PG_CONTAINER" psql -qtA -F' ' -U rent -d rent -c "$1" 2>/dev/null
}

{
    echo "==================== $DAY ===================="
    echo "составлено $(date '+%d.%m.%Y %H:%M')"
    echo

    echo "-- собрано объявлений"
    # Искомый сегмент отдельно: именно по нему судят, собран ли день.
    # Полный обход даёт в полтора раза больше за счёт комнатностей 3+,
    # и общий счёт вводит в заблуждение (13.10.2026 именно так и вышло).
    q "select 'всего: ' || count(distinct s.offer_id) ||
              ', из них 1-2 комнаты: ' ||
              count(distinct s.offer_id) filter (where o.rooms_count in (1,2))
         from raw.offer_snapshot s
         left join core.offer o using (source, offer_id)
        where s.snapshot_date = '$DAY';"

    echo
    echo "-- для сравнения, предыдущие дни"
    # Группировка по самой дате, а не по номеру колонки: в первой колонке
    # агрегат, и group by 1 на него ругается, а ошибка глушится в /dev/null.
    q "select '  ' || s.snapshot_date || ': ' ||
              count(distinct s.offer_id) filter (where o.rooms_count in (1,2))
         from raw.offer_snapshot s
         left join core.offer o using (source, offer_id)
        where s.snapshot_date between '$DAY'::date - 4 and '$DAY'::date - 1
        group by s.snapshot_date order by s.snapshot_date;"

    echo
    echo "-- прогоны"
    q "select '  #' || run_id || ' ' || mode || ' ' || status ||
              ' ' || to_char(started_at at time zone 'Europe/Moscow', 'HH24:MI') ||
              coalesce('-' || to_char(finished_at at time zone 'Europe/Moscow', 'HH24:MI'), '-идёт') ||
              ', корзин ' || (select count(*) from raw.bucket b where b.run_id = r.run_id) ||
              ', сбойных ' || (select count(*) from raw.bucket b
                                where b.run_id = r.run_id and b.error is not null)
         from raw.collection_run r
        where r.snapshot_date = '$DAY' order by run_id;"

    echo
    # День считается покрытым только по прогонам со статусом ok и корзинам
    # без ошибок. Если здесь пусто, снятия с публикации за этот день не
    # считаются — и это правильно, а не поломка.
    echo "-- покрыт ли день (от этого зависят снятия с публикации)"
    q "select coalesce('  комнатности: ' || string_agg(distinct b.rooms, ', '),
                       '  НЕ ПОКРЫТ: ни одного успешного прогона')
         from raw.collection_run r join raw.bucket b using (run_id)
        where r.source = 'cian' and r.status = 'ok'
          and r.snapshot_date = '$DAY' and b.error is null;"

    echo
    echo "-- снятия с публикации (это снятие, а не сдача квартиры)"
    q "select '  ' || disappeared_date || ': ' || count(*)
         from core.offer where disappeared_date = '$DAY' group by 1;"
    q "select '  активных сейчас: ' || count(*) from core.offer where is_active;"

    echo
    echo "-- сторож"
    if [ -f "$LOG_DIR/box_watch.log" ]; then
        echo "  запусков обхода за день: $(grep -c "^$(date -d "$DAY" '+%d.%m' 2>/dev/null || echo '00.00').*ЗАПУСКАЮ" "$LOG_DIR/box_watch.log" 2>/dev/null || echo '?')"
        grep "ВНИМАНИЕ\|ОСТАНОВКА" "$LOG_DIR/box_watch.log" | tail -n 3 | sed 's/^/  /'
    fi

    echo
    echo "-- машина"
    echo "  диск: $(df -Pm "$REPO" | awk 'NR==2 {print $4}') МБ свободно"
    echo "  память: $(free -m | awk 'NR==2 {print $7}') МБ доступно"
    q "select '  база: ' || pg_size_pretty(pg_database_size('rent'));"

    echo
} >>"$REPORT"

# В конце печатаем то же самое в stdout: удобно вызывать руками.
tail -n 60 "$REPORT"

# ------------------------------------------------------------------
# Итог дня одной строкой наружу — он же пульс
# ------------------------------------------------------------------
# Страница выше остаётся в журнале, а наружу уходит одна строка. Смысл у
# неё двойной. Во-первых, это итог: покрыт ли день, сколько собрано по
# искомому сегменту и сколько снятий. Во-вторых, это пульс — он приходит
# каждый вечер в 23:50 по Москве, и его отсутствие само по себе сигнал:
# значит молчит не площадка, а бокс, cron или канал. Именно этого не
# хватало 12-22.10.2026, когда дашборд десять дней показывал мёртвую
# копию базы и ни одна проверка об этом не сказала.
#
# Поэтому пульс идёт с --always: ограничение частоты к нему не
# применяется. Подавленный пульс читался бы как беда.

# Норма искомого сегмента за день, замер 13-21.10.2026. Та же величина в
# box_watch.sh и box_daily.sh.
NORM=16500

notify() { bash "$REPO/scripts/box_notify.sh" "$@" >/dev/null 2>&1; }

SEG="$(q "select count(distinct s.offer_id)
            from raw.offer_snapshot s
            join core.offer o using (source, offer_id)
           where s.snapshot_date = '$DAY' and o.rooms_count in (1,2);" | tr -d '[:space:]')"
# Покрытие считается так же, как на странице выше: только успешные
# прогоны и только корзины без ошибок. От этого зависят снятия.
COVERED="$(q "select count(*)
                from raw.collection_run r join raw.bucket b using (run_id)
               where r.source = 'cian' and r.status = 'ok'
                 and r.snapshot_date = '$DAY' and b.error is null;" | tr -d '[:space:]')"
GONE="$(q "select count(*) from core.offer where disappeared_date = '$DAY';" | tr -d '[:space:]')"

DAY_SHORT="${DAY:8:2}.${DAY:5:2}"

if [ -z "$SEG" ]; then
  # База не ответила — пульс всё равно уходит. Молчание читалось бы как
  # беда с боксом, а беда тут другая и называть её надо своим именем.
  PULSE="Итог дня $DAY_SHORT: база не ответила

Числа дня неизвестны — сам сбор мог при этом пройти.

Посмотреть машину:
\$ ssh -p ${BOX_SSH_PORT} ${BOX_SSH_USER}@${BOX_SSH_HOST} 'docker ps'"
else
  PCT=$(( SEG * 100 / NORM ))
  if [ "${COVERED:-0}" -gt 0 ]; then COVER="день покрыт"; else COVER="день НЕ ПОКРЫТ"; fi
  PULSE="Итог дня $DAY_SHORT: $COVER

1-2 комнаты: $SEG из $NORM ($PCT%)
Снятий за день: ${GONE:-0}"
  # Семьдесят процентов нормы — рядом с порогом самого сторожа (12 000,
  # то есть 73%): ниже этого день стоит догонять руками, выше он
  # содержателен и гонять обход ради последних процентов незачем.
  if [ "${COVERED:-0}" = 0 ] || [ "$PCT" -lt 70 ]; then
    PULSE="$PULSE

Догнать:
\$ ~/rent_msk/scripts/box_daily.sh narrow $DAY"
  fi
fi

notify --always pulse "$PULSE"
