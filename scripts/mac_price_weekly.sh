#!/bin/bash
# Недельное переобучение модели цены на макбуке. Временно, пока у Mac
# mini нет нового диска: там это делал box_price_weekly.sh в контейнере.
#
# Когда. Воскресенье 19:00, после недельного добора (15:00, около трех
# часов), и запасная попытка в понедельник 19:00, если в воскресенье
# макбук был выключен. Модель этой недели после первой удачной попытки
# уже есть, и вторая ничего не делает. С переезда 29.10 переобучения не
# было вовсе: 01.11 пропало, модель простояла с 23.10 до 06.11.
#
# Замок тот же, что у сбора. Ждет его до трех часов: добор мог
# затянуться, а модель этой недели лучше обучить на полной неделе.
#
# Замер 06.11.2026 на макбуке: 305 с, пик памяти процесса 398 МБ из 8 ГБ.
#
# Сводка модели пишется в клон ~/rent_site, оттуда ее забирает хвост дня
# (mac_tail.sh) вместе с витриной.

set -uo pipefail
export TZ=Europe/Moscow
export PATH="/opt/homebrew/opt/coreutils/libexec/gnubin:/opt/homebrew/bin:$PATH"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SITE="$HOME/rent_site"
cd "$REPO" || exit 1

LOG_DIR="$REPO/data/logs"
mkdir -p "$LOG_DIR" "$REPO/data/models"
exec >>"$LOG_DIR/price_$(date +%Y%m).log" 2>&1
say() { echo "$(date '+%d.%m %H:%M:%S') === $*"; }
notify() { bash "$REPO/scripts/box_notify.sh" "$@" >/dev/null 2>&1; }

# Неделя с воскресенья (%U), а не по ISO: в ISO воскресенье последний
# день недели, начатой в понедельник, и обучение в будний день той же
# недели заранее «занимало» воскресенье. 08.11.2026 так пропало
# воскресное переобучение после ручного 06.11.
WEEK="$(date +%Y-U%U)"
MARK="$REPO/data/models/last_week"
if [ "$(cat "$MARK" 2>/dev/null)" = "$WEEK" ]; then
  say "Модель недели $WEEK уже есть"
  exit 0
fi

LOCK="$REPO/data/daily.lock"
GOT_LOCK=0
for _ in $(seq 1 180); do
  if mkdir "$LOCK" 2>/dev/null; then GOT_LOCK=1; break; fi
  sleep 60
done
if [ "$GOT_LOCK" = 0 ]; then
  say "Замок не взялся за три часа, переобучение в следующую попытку"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

git -C "$SITE" pull -q --rebase --autostash 2>/dev/null || say "клон сайта не обновился, пишу в то, что есть"

say "Переобучение модели цены, неделя $WEEK"
RESULTS_DIR="$SITE/results" MLFLOW_DISABLE_AGENT_HINT=1 PYTHONUNBUFFERED=1 \
  timeout 3600 caffeinate -i -s "$REPO/.venv/bin/python" -m src.price_model
CODE=$?
if [ "$CODE" -ne 0 ]; then
  say "Переобучение упало, код $CODE"
  notify price-fail "Переобучение модели цены упало
$(date '+%d.%m, %H:%M')

Код $CODE. /price отвечает оценками прошлой недели, данные панели не задеты."
  exit "$CODE"
fi
echo "$WEEK" > "$MARK"
ls -1t "$REPO"/data/models/price-*.cbm 2>/dev/null | tail -n +9 | xargs -r rm -f

# Порог выводится из первых недель, до него тревоги нет и быть не может.
ROW="$(docker exec -i rent_pg psql -qtA -F'|' -U rent -d rent -c \
  "select mape_pct, coalesce(threshold_pct::text, '') from ml.price_drift
    order by week_start desc limit 1;" 2>/dev/null)"
MAPE="${ROW%%|*}"; THR="${ROW##*|}"
say "Ошибка на новой неделе ${MAPE}%, порог ${THR:-еще не выведен}"
if [ -n "$THR" ] && awk -v m="$MAPE" -v t="$THR" 'BEGIN { exit !(m > t) }'; then
  notify price-drift "Модель цены ошибается больше обычного
$(date '+%d.%m, %H:%M')

Ошибка на объявлениях новой недели ${MAPE}% при пороге ${THR}%. Рынок сдвинулся или сбор поменялся, оценкам /price доверять осторожнее."
fi
exit 0
