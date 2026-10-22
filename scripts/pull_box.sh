#!/bin/bash
# Перелив живой базы с Mac mini в копию на макбуке.
#
# Зачем. Сбор с 12.10.2026 живёт на Mac mini, а дашборд Metabase крутится
# в соседнем проекте на макбуке и подключён к ЛОКАЛЬНОЙ базе
# (host.docker.internal:5433). С переездом сбора эта база замерла, и
# дашборд десять дней показывал мёртвые данные, ничего об этом не говоря.
# Прямо подключить Metabase к боксу нельзя: бокс за NAT, снаружи проброшен
# только ssh на 2202.
#
# Поэтому дашборд кормится переливом по требованию. Данные в нём свежи на
# момент команды — это цена простоты, зато ни туннеля, ни ключа в чужом
# контейнере, ни запросов Metabase на слабую машину сбора.
#
#   make pull-box                  # перелить
#   PULL_FORCE=1 make pull-box     # даже если на боксе идёт обход
#
# Что НЕ делает: не трогает базу на боксе (только читает) и не меняет
# подключение Metabase.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

BOX="ssh -o ConnectTimeout=20 -o BatchMode=yes -p ${BOX_SSH_PORT} ${BOX_SSH_USER}@${BOX_SSH_HOST}"
DUMP_DIR="$REPO/data/dumps"
DUMP="$DUMP_DIR/rent_$(date +%Y%m%d_%H%M).dump"

say() { echo "$(date +%H:%M:%S) === $*"; }
die() { echo "$(date +%H:%M:%S) !!! $*" >&2; exit 1; }

mkdir -p "$DUMP_DIR"

# Бокс отвечает не всегда с первого раза: когда ему тяжело, рукопожатие
# ssh отваливается по таймауту. Поэтому все обращения — с повторами.
box() {
  local i out
  for i in $(seq 1 15); do
    out="$($BOX "$@" 2>&1)" && { echo "$out"; return 0; }
  done
  echo "$out"
  return 1
}

say "Проверяю бокс"
BOX_STATE="$(box "[ -d ~/rent_msk/data/box.lock ] && echo обход_идёт || echo свободен")" \
  || die "бокс не отвечает после пятнадцати попыток"

if [ "$BOX_STATE" = "обход_идёт" ] && [ -z "${PULL_FORCE:-}" ]; then
  # Дамп читает базу целиком, а машина слабая: два ядра и три гигабайта.
  # Снимок при этом консистентный (MVCC), дело не в данных, а в нагрузке
  # на машину посреди обхода.
  die "на боксе идёт обход. Подождите его конца или повторите с PULL_FORCE=1"
fi

say "Снимаю дамп (nice, сжатие на стороне бокса)"
# Дамп идёт потоком через ssh и НИКОГДА не ложится на диск бокса: там
# свободно около четырнадцати гигабайт, и они общие со вторым боксом.
if ! $BOX 'nice -n 10 docker exec -i rent_pg pg_dump -U rent -d rent -Fc -Z 6' >"$DUMP"; then
  rm -f "$DUMP"
  die "дамп не снялся"
fi

SIZE_MB=$(( $(wc -c <"$DUMP") / 1024 / 1024 ))
[ "$SIZE_MB" -lt 100 ] && die "дамп подозрительно мал: ${SIZE_MB} МБ"
say "Дамп на месте: ${SIZE_MB} МБ, $DUMP"

# Целостность проверяется до того, как что-то роняем локально: испорченный
# дамп, залитый поверх копии, оставил бы вместо неё кашу.
OBJECTS=$(pg_restore -l "$DUMP" 2>/dev/null | grep -c '^[0-9]')
[ "${OBJECTS:-0}" -lt 50 ] && die "в дампе всего $OBJECTS объектов, это не похоже на нашу базу"
say "В дампе $OBJECTS объектов"

docker compose exec -T postgres pg_isready -U rent -d rent >/dev/null 2>&1 \
  || die "локальная база не отвечает. Поднимите её: make up"

# Metabase держит соединения к этой базе, и его блокировки мешают
# --clean дропать таблицы. Соединения только читающие, поэтому рвём их
# без сожалений: Metabase переподключится сам на следующем запросе.
say "Отключаю читателей локальной базы (Metabase переподключится сам)"
docker compose exec -T postgres psql -qtA -U rent -d rent -c \
  "select count(pg_terminate_backend(pid))
     from pg_stat_activity
    where datname = 'rent' and pid <> pg_backend_pid();" >/dev/null 2>&1

say "Заливаю (это самая долгая часть)"
# Дамп кладётся внутрь контейнера, чтобы pg_restore работал в четыре
# потока: из stdin параллельный перелив не умеет.
docker compose exec -T postgres bash -c 'cat > /tmp/rent.dump' <"$DUMP" \
  || die "дамп не удалось положить в контейнер"
docker compose exec -T postgres pg_restore -U rent -d rent \
  --clean --if-exists --no-owner -j 4 /tmp/rent.dump
RESTORE_CODE=$?
docker compose exec -T postgres rm -f /tmp/rent.dump >/dev/null 2>&1

# Код возврата pg_restore не ноль даже из-за безобидных «не существует»
# при --clean на пустом месте, поэтому решает не он, а сверка ниже.
[ "$RESTORE_CODE" != 0 ] && say "pg_restore вернул $RESTORE_CODE — смотрю, что получилось"

say "Сверяю с боксом"
Q="select max(snapshot_date) || ' | ' || count(distinct snapshot_date) || ' дней | ' || count(*) || ' снимков' from raw.offer_snapshot;"
THERE="$(box "docker exec -i rent_pg psql -qtA -U rent -d rent -c \"$Q\"")"
HERE="$(docker compose exec -T postgres psql -qtA -U rent -d rent -c "$Q" | tr -d '\r')"

echo
echo "  на боксе:  $THERE"
echo "  у себя:    $HERE"
echo

if [ "$THERE" = "$HERE" ]; then
  say "Сошлось. Дашборд теперь показывает данные на этот момент"
else
  die "НЕ СОШЛОСЬ. Локальную копию считать негодной, пока не разобрались"
fi

# Дампы занимают по пара гигабайт, и диск макбука не резиновый: держим
# два последних, остальные удаляем.
ls -t "$DUMP_DIR"/rent_*.dump 2>/dev/null | tail -n +3 | while read -r old; do
  rm -f "$old" && say "Удалён старый дамп: $(basename "$old")"
done
