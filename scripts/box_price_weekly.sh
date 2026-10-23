#!/bin/bash
# Недельное переобучение модели справедливой цены на Mac mini.
#
# Когда. Воскресенье, попытки в 16:00, 18:00 и 20:00 по Москве: недельный
# добор начинается в 13:00 и может затянуться. Модель этой недели уже
# есть, значит следующие попытки ничего не делают.
#
# Замок. Берется тот же замок, что у обхода. Обход в пике держит около
# 2,4 ГБ из 3 ГиБ бокса, переобучение до 900 МБ: вместе они не влезают, и
# одновременно не идут никогда. Пока замок у переобучения, сторож обход
# не начинает (замок свежий), а переобучение укладывается в час.
#
# Память и время. Свой контейнер, лимит 900 МБ, общий срок 60 минут.
# Первый прогон на Mac mini 23.10.2026: 5,6 минуты, пик контейнера 450 МБ,
# весь бокс 724 МБ. Лимит 500 МБ оставлял 10% запаса при растущей таблице,
# поэтому поднят: обход в это время не идет, и вместе выходит до 1,2 ГБ
# из 3 ГиБ.
#
# Что пишет. Схема ml в базе (оценки для /price и журнал дрейфа), модель в
# data/models (хранятся восемь последних), MLflow в data/mlruns, сводку в
# results клона ~/rent_site, откуда ее забирает box_site_export.sh.
#
# Тревога. Падение или ошибка новой недели выше порога, выведенного из
# первых четырех недель, уходят через box_notify.sh.

export TZ=Europe/Moscow
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SITE="$HOME/rent_site"
cd "$REPO" || exit 1

LOG_DIR="$REPO/data/logs"
mkdir -p "$LOG_DIR" "$REPO/data/models"
LOG="$LOG_DIR/price_$(date +%Y%m%d_%H%M).log"
exec >>"$LOG" 2>&1
say() { echo "$(date +%H:%M:%S) === $*"; }
notify() { bash "$REPO/scripts/box_notify.sh" "$@" >/dev/null 2>&1; }
find "$LOG_DIR" -name 'price_*.log' -mtime +60 -delete 2>/dev/null

WEEK="$(date +%G-W%V)"
MARK="$REPO/data/models/last_week"
if [ "$(cat "$MARK" 2>/dev/null)" = "$WEEK" ]; then
  say "Модель недели $WEEK уже есть"
  exit 0
fi

LOCK="$REPO/data/box.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  say "Идет обход, переобучение подождет следующей попытки"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

git -C "$SITE" pull -q --ff-only 2>/dev/null || say "клон сайта не обновился, пишу в то, что есть"

say "Переобучение модели цены, неделя $WEEK"
timeout 3600 docker run --rm \
  --name rent_price \
  --network host \
  --memory=900m --memory-swap=900m \
  -v "$REPO":/work -w /work \
  -v "$SITE/results":/results \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -e TZ=Europe/Moscow -e PYTHONUNBUFFERED=1 \
  -e MLFLOW_DISABLE_AGENT_HINT=1 -e RESULTS_DIR=/results \
  rent-price:latest \
  python -m src.price_model
CODE=$?

if [ "$CODE" -ne 0 ]; then
  say "Переобучение упало, код $CODE"
  notify price-fail "Переобучение модели цены упало
$(date '+%d.%m, %H:%M')

Код $CODE. /price отвечает оценками прошлой недели, данные панели не задеты."
  exit "$CODE"
fi
echo "$WEEK" > "$MARK"
# Модели старше восьми последних не нужны: в MLflow остаются их метрики.
ls -1t "$REPO"/data/models/price-*.cbm 2>/dev/null | tail -n +9 | xargs -r rm -f

# Дрейф: последняя неделя против порога. Первые четыре недели порога нет,
# тревоги тогда нет и быть не может, это записано в README.
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
