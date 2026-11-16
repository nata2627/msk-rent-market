#!/bin/bash
# Добор после сбоя: ждет, пока площадка снова отдаст выдачу, и продолжает
# оборванный обход той же даты с места обрыва.
#
# Зачем. 15.11.2026 недельный добор 3+ упал на 516-м запросе: площадка
# закрыла доступ, за день с адреса ушло около 1600 запросов. 09.11 она
# закрыла доступ в 18:18 и открыла к 19:20, то есть обрыв обычно не на
# сутки, а сбор в такой день стоял до следующего расписания.
#
# Чем отличается от сторожа box_watch.sh. Тот запускал полный обход, как
# только за сутки собрано меньше порога, то есть каждую ночь после
# полуночи. Этот трогает площадку, только когда прогон сам оставил
# отметку о сбое сбора (data/retry/<режим>, внутри дата снимка), и только
# в те же сутки: собранное завтра под вчерашней датой было бы неправдой о
# том, что висело вчера.
#
# Порядок за тик:
#   отметки нет                → выход молча
#   дата отметки не сегодня    → отметка снимается, одно сообщение
#   идет другой прогон (замок) → выход
#   позже 22:00                → выход, отметка ждет конца суток
#   с последнего сбоя < паузы  → выход (площадка не открывается сразу)
#   попытки кончились          → отметка снимается, одно сообщение
#   проба не прошла            → строка в журнал, выход
#   проба прошла               → box_daily.sh <режим> <дата> с --resume
#
# Таймер rent-retry, раз в 30 минут. Проба стоит два запроса.

set -uo pipefail

export TZ=Europe/Moscow

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

IMAGE="rent-collector:latest"

# Пауза после сбоя и предел перезапусков за сутки. Через переменные,
# чтобы проверять логику, не трогая файл. Час паузы по 09.11: доступ
# вернулся примерно через час. Шесть перезапусков, потому что каждый
# стоит сотни запросов и, если площадка закрывает снова и снова, лучше
# отдать хвост дня, чем закрепить за адресом дурную славу.
PAUSE_MIN="${RETRY_PAUSE_MIN:-60}"
MAX_RUNS="${RETRY_MAX_RUNS:-6}"
# Последний час, когда еще можно начать перезапуск. Срез 1-2 комнат идет
# около двух часов, и начатый позже ушел бы за полночь, записывая
# объявления следующих суток под датой предыдущих.
LAST_HOUR="${RETRY_LAST_HOUR:-22}"

RETRY_DIR="$REPO/data/retry"
LOG_DIR="$REPO/data/logs"
mkdir -p "$LOG_DIR"
RETRY_LOG="$LOG_DIR/box_retry.log"

note() { echo "$(date '+%d.%m %H:%M') $*" >>"$RETRY_LOG"; }
notify() { bash "$REPO/scripts/box_notify.sh" "$@" >/dev/null 2>&1; }
seg_line() { bash "$REPO/scripts/box_seg.sh" "$1"; }

TODAY="$(date +%F)"

# Сначала снимаются отметки прошлых суток: их уже не добрать честно.
for f in "$RETRY_DIR"/*; do
  [ -f "$f" ] || continue
  MODE="$(basename "$f")"
  DAY="$(head -n 1 "$f")"
  [ "$DAY" = "$TODAY" ] && continue
  RUNS="$(grep -c '^run ' "$f")"
  rm -f "$f"
  note "$MODE за $DAY: сутки кончились, отметка снята (перезапусков было $RUNS)"
  notify "retry-late-$MODE" "Добор за $DAY не удался
$(date '+%d.%m, %H:%M')

Режим $MODE: сутки кончились, площадка так и не дала собрать до конца.
Перезапусков было $RUNS. По сегменту за $DAY: $(seg_line "$DAY")."
done

# Срез 1-2 комнат важнее добора 3+: если оборваны оба, первым идет он.
MODE=""
for m in narrow full weekly; do
  if [ -f "$RETRY_DIR/$m" ]; then MODE="$m"; break; fi
done
[ -n "$MODE" ] || exit 0
FILE="$RETRY_DIR/$MODE"

[ -d "$REPO/data/box.lock" ] && exit 0
[ "$((10#$(date +%H)))" -lt "$LAST_HOUR" ] || exit 0

AGE_MIN=$(( ($(date +%s) - $(date -r "$FILE" +%s)) / 60 ))
[ "$AGE_MIN" -ge "$PAUSE_MIN" ] || exit 0

RUNS="$(grep -c '^run ' "$FILE")"
if [ "$RUNS" -ge "$MAX_RUNS" ]; then
  rm -f "$FILE"
  note "$MODE за $TODAY: $RUNS перезапусков, дальше не трогаю площадку"
  notify "retry-gaveup-$MODE" "Добор остановлен
$(date '+%d.%m, %H:%M')

Режим $MODE за $TODAY: $RUNS перезапусков, площадка закрывает снова.
По сегменту $(seg_line "$TODAY").

Если хочется еще раз вручную: /catchup"
  exit 0
fi

# Имя контейнера пробы освобождается заранее: убитая по таймауту проба
# оставляет имя занятым, и все следующие падали бы на конфликте имени.
timeout 20 docker rm -f rent_probe_retry >/dev/null 2>&1

# Проба та же, что у сторожа (scripts/box_probe.py), в том же профиле
# браузера, где копятся куки.
PROBE_OUT="$(timeout 300 docker run --rm \
  --name rent_probe_retry \
  --network host \
  --shm-size=1g \
  --memory=1024m \
  --memory-swap=1024m \
  -v "$REPO":/work \
  -w /work \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e TZ=Europe/Moscow \
  -e CHROME_NO_SANDBOX=1 \
  -e PYTHONUNBUFFERED=1 \
  -e PYTHONPATH=/work \
  "$IMAGE" \
  bash -c 'Xvfb :99 -screen 0 1440x900x24 >/tmp/xvfb.log 2>&1 &
           sleep 3
           DISPLAY=:99 python scripts/box_probe.py' 2>&1 | tail -n 1)"
PROBE_CODE=$?

if [ "$PROBE_CODE" != 0 ]; then
  note "$MODE за $TODAY: ${PROBE_OUT:-проба без ответа, код $PROBE_CODE}"
  exit 0
fi

echo "run $(date '+%H:%M')" >>"$FILE"
note "$MODE за $TODAY: $PROBE_OUT, перезапуск $((RUNS + 1)) из $MAX_RUNS"
BOX_RETRY=1 exec "$REPO/scripts/box_daily.sh" "$MODE" "$TODAY"
