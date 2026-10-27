#!/bin/bash
# Перелив живой базы с Mac mini в копию на макбуке.
#
# Зачем. Сбор с 12.10.2026 живёт на Mac mini, а дашборд Metabase крутится
# в соседнем проекте на макбуке и подключён к ЛОКАЛЬНОЙ базе
# (host.docker.internal:5433). С переездом сбора эта база замерла, и
# дашборд десять дней показывал мёртвые данные, ничего об этом не говоря.
# Прямо подключить Metabase к боксу нельзя: бокс за NAT, снаружи проброшен
# только ssh.
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

DUMP_DIR="$REPO/data/dumps"
DUMP="$DUMP_DIR/rent_$(date +%Y%m%d_%H%M).dump"

say() { echo "$(date +%H:%M:%S) === $*"; }
die() { echo "$(date +%H:%M:%S) !!! $*" >&2; exit 1; }

# Адрес машины сбора берётся из .env, а не из кода: репозиторий публичный,
# и сведения о домашней сети в нём не нужны никому, кроме нас. Образец
# переменных — в .env.example.
box_env() {
    sed -n "s/^[[:space:]]*$1=//p" "$REPO/.env" 2>/dev/null | tail -n 1 \
      | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}
BOX_HOST="${BOX_SSH_HOST:-$(box_env BOX_SSH_HOST)}"
BOX_PORT="${BOX_SSH_PORT:-$(box_env BOX_SSH_PORT)}"
BOX_USER="${BOX_SSH_USER:-$(box_env BOX_SSH_USER)}"
if [ -z "$BOX_HOST" ] || [ -z "$BOX_USER" ]; then
    die "не заданы BOX_SSH_HOST и BOX_SSH_USER — ни в окружении, ни в .env (образец в .env.example)"
fi
BOX="ssh -o ConnectTimeout=20 -o BatchMode=yes -p ${BOX_PORT:-22} ${BOX_USER}@${BOX_HOST}"

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

# Запрет на перелив посреди обхода касается только СНЯТИЯ дампа: он
# читает базу целиком, а машина слабая — два ядра и три гигабайта.
# Снимок при этом консистентный (MVCC), дело не в данных, а в нагрузке.
# С готовым дампом (PULL_DUMP) бокс не трогается вовсе, кроме одного
# запроса на сверку, — запрещать тут нечего.
if [ "$BOX_STATE" = "обход_идёт" ] && [ -z "${PULL_FORCE:-}" ] && [ -z "${PULL_DUMP:-}" ]; then
  die "на боксе идёт обход. Подождите его конца или повторите с PULL_FORCE=1"
fi

# Повторное использование дампа. Дамп едет с бокса четырнадцать минут и
# грузит слабую машину, поэтому если он уже снят — а перелив сорвался
# дальше по дороге, как 22.10.2026 на проверке целостности, — второй раз
# его тянуть незачем.
if [ -n "${PULL_DUMP:-}" ]; then
  [ -f "$PULL_DUMP" ] || die "дампа $PULL_DUMP нет"
  DUMP="$PULL_DUMP"
  say "Беру готовый дамп от $(date -r "$DUMP" '+%d.%m %H:%M'): $DUMP"
  say "ВНИМАНИЕ: копия будет свежа на этот момент, а не на сейчас"
else
say "Снимаю дамп (nice, сжатие на стороне бокса)"
# Дамп идёт потоком через ssh и НИКОГДА не ложится на диск бокса: там
# свободно около четырнадцати гигабайт, и они общие со вторым боксом.
if ! $BOX 'nice -n 10 docker exec -i rent_pg pg_dump -U rent -d rent -Fc -Z 6' >"$DUMP"; then
  rm -f "$DUMP"
  die "дамп не снялся"
fi

fi

SIZE_MB=$(( $(wc -c <"$DUMP") / 1024 / 1024 ))
[ "$SIZE_MB" -lt 100 ] && die "дамп подозрительно мал: ${SIZE_MB} МБ"
say "Дамп на месте: ${SIZE_MB} МБ, $DUMP"

docker compose exec -T postgres pg_isready -U rent -d rent >/dev/null 2>&1 \
  || die "локальная база не отвечает. Поднимите её: make up"

# Дамп кладётся внутрь контейнера один раз: и для проверки целостности, и
# для самого перелива в четыре потока (из stdin параллельный не умеет).
say "Кладу дамп в контейнер"
docker compose exec -T postgres bash -c 'cat > /tmp/rent.dump' <"$DUMP" \
  || die "дамп не удалось положить в контейнер"

# Целостность проверяется до того, как что-то роняем локально:
# испорченный дамп, залитый поверх копии, оставил бы вместо неё кашу.
#
# Проверка идёт ВНУТРИ контейнера, а не на макбуке. На макбуке
# клиентских утилит Postgres нет вовсе — в проекте всё ходит через
# контейнер, — и `pg_restore -l` молча не находился: вывод пустой,
# объектов ноль, перелив падал на здоровом дампе в два гигабайта
# (22.10.2026).
OBJECTS=$(docker compose exec -T postgres pg_restore -l /tmp/rent.dump 2>/dev/null | grep -c '^[0-9]')
[ "${OBJECTS:-0}" -lt 50 ] && die "в дампе всего $OBJECTS объектов, это не похоже на нашу базу"
say "В дампе $OBJECTS объектов"

# Metabase держит соединения к этой базе, и его блокировки мешают
# --clean дропать таблицы. Соединения только читающие, поэтому рвём их
# без сожалений: Metabase переподключится сам на следующем запросе.
say "Отключаю читателей локальной базы (Metabase переподключится сам)"
docker compose exec -T postgres psql -qtA -U rent -d rent -c \
  "select count(pg_terminate_backend(pid))
     from pg_stat_activity
    where datname = 'rent' and pid <> pg_backend_pid();" >/dev/null 2>&1

# Схемы проекта сносятся целиком, а не через --clean. --clean удаляет
# только то, что есть в дампе, и спотыкается о локальное, чего в дампе
# нет: 27.10.2026 витрины на боксе стали таблицами dbt, а в копии
# остались матвитрины, зависящие от raw. Сырые таблицы не удалились, и
# pg_restore долил строки поверх: снимков вышло 1 669 886 против 921 061
# на боксе. Копия только зеркало бокса, своего в ней нет.
say "Убираю прежнюю копию"
docker compose exec -T postgres psql -qtA -v ON_ERROR_STOP=1 -U rent -d rent -c "
  do \$\$ declare n text; begin
    for n in select nspname from pg_namespace
              where nspname not like 'pg\\_%' and nspname not in ('information_schema', 'public')
    loop execute format('drop schema %I cascade', n); end loop;
  end \$\$;" || die "прежнюю копию убрать не удалось"
say "Заливаю (это самая долгая часть)"
docker compose exec -T postgres pg_restore -U rent -d rent \
  --no-owner -j 4 /tmp/rent.dump
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

# Сверка строгая только тогда, когда бокс стоял на месте: дамп снят
# сейчас же и обхода не было. Иначе расхождение законно — обход дописывает
# снимки, пока дамп едет, — и падать на нём значило бы кричать на здоровом
# переливе. Так и вышло 22.10.2026: перелив пошёл из дампа, снятого на
# полчаса раньше, а на боксе тем временем добирался день.
STRICT=1
[ -n "${PULL_DUMP:-}" ] && STRICT=0
[ -n "${PULL_FORCE:-}" ] && STRICT=0

if [ "$THERE" = "$HERE" ]; then
  say "Сошлось. Дашборд теперь показывает данные на этот момент"
elif [ "$STRICT" = 1 ]; then
  die "НЕ СОШЛОСЬ. Локальную копию считать негодной, пока не разобрались"
else
  say "Не сошлось, и это ожидаемо: бокс ушёл вперёд с момента дампа"
fi

# Проверка, что копия вообще жива, а не выборочно пуста. Нужна именно
# когда строгая сверка отключена: тогда ничто другое не отличило бы
# «перелив прошёл» от «перелив прошёл наполовину».
SANE="$(docker compose exec -T postgres psql -qtA -F'|' -U rent -d rent -c \
  "select (select count(*) from raw.offer_snapshot),
          (select count(*) from core.offer),
          (select count(*) from marts.offer_scored),
          (select verdict from marts.data_freshness);" | tr -d '\r')"
echo "  в копии: снимков|объявлений|витрина подбора|свежесть"
echo "           $SANE"
case "$SANE" in
  0\|*|*\|0\|*|*\|0\|*) die "в копии пустые таблицы, перелив негоден" ;;
esac
say "Копия цела"

# Дампы занимают по пара гигабайт, и диск макбука не резиновый: держим
# два последних, остальные удаляем.
ls -t "$DUMP_DIR"/rent_*.dump 2>/dev/null | tail -n +3 | while read -r old; do
  rm -f "$old" && say "Удалён старый дамп: $(basename "$old")"
done
