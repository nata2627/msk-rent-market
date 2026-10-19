#!/bin/bash
# Ежедневный прогон на Mac mini под Ubuntu — тот же порядок шагов, что в
# scripts/daily.sh, но без macOS-обвязки.
#
# Чего здесь нет и почему. Проверки питания нет: машина стоит на столе и
# питается от сети всегда, а именно батарея дважды стоила нам дня
# (04.10.2026 и 09.10.2026). caffeinate нет: Linux не усыпляет машину под
# нагрузкой. Запуска Docker Desktop нет: демон живёт в системе и
# поднимается сам. Коммита чисел README нет: репозиторий с историей
# остаётся на макбуке, здесь только рабочее зеркало кода.
#
# Весь сбор идёт в контейнере rent-collector: настоящий Chrome там уже
# установлен, а в боксе его поставить нельзя — нет sudo.
#
#   scripts/box_daily.sh              # ежедневный срез, 1-2 комнаты
#   scripts/box_daily.sh weekly       # недельный добор комнатностей 3+
#   scripts/box_daily.sh full         # полный обход, все комнатности
#   scripts/box_daily.sh narrow 2026-10-12   # за конкретную дату снимка

set -uo pipefail

# Машина живёт по UTC, а панель — по Москве. Без этого дата снимка и часы
# в журнале уехали бы на три часа, а вечерний прогон записался бы уже
# следующим днём. Пояс задаётся переменной, потому что системный сменить
# нельзя: в боксе нет sudo.
export TZ=Europe/Moscow

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

MODE="${1:-narrow}"
SNAPSHOT_DATE="${2:-$(date +%F)}"

IMAGE="rent-collector:latest"
PG_CONTAINER="rent_pg"

LOG_DIR="$REPO/data/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/box_$(date +%Y%m%d_%H%M).log"
exec >>"$LOG" 2>&1

say() { echo "$(date +%H:%M:%S) === $*"; }

# Удаляются только файлы журналов прогона (data/logs/box_*.log), и
# никогда данные: база живёт в отдельном томе Docker, а raw вообще только
# пополняется. Срок 60 дней — как на макбуке. Здесь это не опрятность, а
# необходимость: диск общий со вторым боксом машины, и у соседей он
# кончался трижды за сентябрь.
find "$LOG_DIR" -name 'box_*.log' -mtime +60 -delete 2>/dev/null

# Замок. Второй обход в том же профиле браузера сломал бы сессию обоим,
# поэтому прогон занимает каталог и молча уходит, если предыдущий жив.
LOCK="$REPO/data/box.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  say "Предыдущий прогон ещё идёт ($LOCK). Выхожу."
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

say "Начало, режим $MODE, дата снимка $SNAPSHOT_DATE"

# База. Без неё собирать некуда, поэтому это единственная проверка,
# которая останавливает прогон целиком.
if ! docker exec "$PG_CONTAINER" pg_isready -U rent -d rent >/dev/null 2>&1; then
  say "База не отвечает, поднимаю"
  # Каталог с compose-файлом базы: она живёт отдельно от зеркала кода,
  # чтобы пересылка кода не задевала данные.
  (cd "$HOME/rent_data" && docker compose up -d)
  for i in $(seq 1 24); do
    sleep 5
    docker exec "$PG_CONTAINER" pg_isready -U rent -d rent >/dev/null 2>&1 && break
    [ "$i" = 24 ] && { say "ОШИБКА: база так и не поднялась. Прогон отменён."; exit 1; }
  done
fi
say "База отвечает"

# Кеш браузера растёт примерно по 200 МБ в сутки: 15.10.2026 его почистили
# до 57 МБ, через четыре дня профиль весил 828 МБ. Чистится только кеш,
# файл Cookies лежит отдельно и не страдает — в нём накопленная сессия
# площадки, без неё выдача даётся заметно хуже.
CACHE_DIR="$REPO/data/browser_profile/Default"
if [ -d "$CACHE_DIR/Cache" ]; then
  CACHE_MB=$(du -sm "$CACHE_DIR/Cache" 2>/dev/null | cut -f1)
  if [ "${CACHE_MB:-0}" -gt 300 ]; then
    rm -rf "$CACHE_DIR/Cache" "$CACHE_DIR/Code Cache"
    say "Кеш браузера разросся до ${CACHE_MB} МБ, вычищен (куки не тронуты)"
  fi
fi

# Шаги сбора и разбора идут одним запуском контейнера: браузер
# поднимается один раз на весь обход, как и на макбуке.
#
# --network host — чтобы дотянуться до базы на 127.0.0.1:5433.
# --shm-size=1g — на стандартных 64 МБ Chrome падает без внятной причины.
# --user — иначе контейнер пишет в смонтированный проект от root, и файлы
# журнала и профиля браузера потом не стереть обычным пользователем.
# HOME=/tmp — Chrome требует домашний каталог, доступный на запись, а
# домашнего каталога у подставленного пользователя внутри образа нет.
# Xvfb поднимается отдельной командой, а не обёрткой xvfb-run: обёртка на
# этой машине не завершается и оставляет контейнер висеть (11.10.2026).
# Недельный добор — это не полный обход: комнатности 1-2 собираются
# ежедневно, и повторять их раз в неделю значило бы зря удвоить запросы.
# Ровно те же коды, что в scripts/weekly.sh на макбуке.
case "$MODE" in
  weekly) COLLECT_ARGS="--mode full --rooms 3,4,5,6,7,9" ;;
  *)      COLLECT_ARGS="--mode $MODE" ;;
esac
# --resume: пропустить корзины, уже собранные за эту дату. В обычный день
# пропускать нечего и флаг ничего не меняет, а в день, когда площадка
# роняет обход раз за разом, без него очередь районов не уходит дальше
# первых двух десятков (проверено 12.10.2026).
COLLECT_ARGS="$COLLECT_ARGS --transport browser --resume --snapshot-date $SNAPSHOT_DATE"

docker run --rm \
  --name rent_collect \
  --network host \
  --shm-size=1g \
  -v "$REPO":/work \
  -w /work \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e TZ=Europe/Moscow \
  -e CHROME_NO_SANDBOX=1 \
  -e PYTHONUNBUFFERED=1 \
  "$IMAGE" \
  bash -c "
    set -uo pipefail
    Xvfb :99 -screen 0 1440x900x24 >/tmp/xvfb.log 2>&1 &
    sleep 3
    export DISPLAY=:99

    step() {
      local name=\"\$1\"; shift
      echo \"\$(date +%H:%M:%S) === \$name\"
      \"\$@\"
      # Код снимается первой же строкой: любая подстановка, включая
      # \$(date), затёрла бы его собственным кодом возврата.
      local code=\$?
      if [ \"\$code\" = 0 ]; then
        echo \"\$(date +%H:%M:%S) === \$name — готово\"
      else
        echo \"\$(date +%H:%M:%S) === \$name — СБОЙ (код \$code), иду дальше\"
        return 1
      fi
    }

    fails=0
    # Порядок не случаен: сбор первый и единственный незаменимый, всё
    # остальное пересчитывается из raw когда угодно.
    step 'Сбор'           python -m etl.collect $COLLECT_ARGS || fails=\$((fails+1))
    step 'Справочник'     python -m etl.districts            || fails=\$((fails+1))
    step 'Парки и МКАД'   python -m etl.parks                || fails=\$((fails+1))
    step 'Дедупликация'   python -m src.dedup                || fails=\$((fails+1))
    step 'Витрины'        python -c \"
import psycopg
from etl import config
with psycopg.connect(**config.DB) as conn:
    conn.execute('select marts.refresh_all()')
    conn.commit()
print('витрины пересобраны')
\" || fails=\$((fails+1))

    echo \"\$(date +%H:%M:%S) === Сбоев: \$fails\"
    exit \$fails
  "

FAILS=$?
say "Конец, сбоев: $FAILS"

# Код возврата честный: он отражает число сбойных шагов, и таймер покажет
# его в journalctl.
exit "$FAILS"
