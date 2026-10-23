#!/bin/bash
# Недельное переобучение модели справедливой цены на боксе.
#
# Когда. В воскресенье после недельного добора комнатностей 3+, в 16:00
# по Москве: добор начинается в 13:00 и к этому часу обычно кончается.
# Если обход еще идет, скрипт ждет замка и не начинает: обход важнее, день
# панели потерять нельзя, а модель подождет до следующего запуска.
#
# Сколько. Отдельный контейнер со своим лимитом памяти 500 МБ (Chrome
# обхода держит до 1,6 ГБ при лимите бокса 3 ГиБ) и общим сроком 60 минут.
# Пик на макбуке замерен в make price-model, см. docs/operations.md.
#
# Что пишет. Схема ml в базе (оценки для /price и журнал дрейфа), модель в
# data/models, MLflow в data/mlruns. raw и core не трогает.
#
# Тревога. Если ошибка модели прошлой недели на объявлениях этой недели
# выше порога, выведенного из первых четырех недель, или переобучение
# упало, сообщение уходит через box_notify.sh.

export TZ=Europe/Moscow
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

LOG_DIR="$REPO/data/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/price_$(date +%Y%m%d_%H%M).log"
exec >>"$LOG" 2>&1
say() { echo "$(date +%H:%M:%S) === $*"; }
notify() { bash "$REPO/scripts/box_notify.sh" "$@" >/dev/null 2>&1; }

if [ -d "$REPO/data/box.lock" ]; then
  say "Идет обход, переобучение пропускаю до следующей недели"
  exit 0
fi

say "Переобучение модели цены"
timeout 3600 docker run --rm \
  --name rent_price \
  --network host \
  --memory=500m --memory-swap=500m \
  -v "$REPO":/work -w /work \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -e TZ=Europe/Moscow -e PYTHONUNBUFFERED=1 \
  -e MLFLOW_DISABLE_AGENT_HINT=1 \
  rent-price:latest \
  python -m src.price_model
CODE=$?

if [ "$CODE" -ne 0 ]; then
  say "Переобучение упало, код $CODE"
  notify price-fail "$(date '+%d.%m %H:%M') ВНИМАНИЕ: недельное переобучение модели цены упало (код $CODE). /price отвечает оценками прошлой недели."
  exit "$CODE"
fi

# Дрейф: последняя неделя против порога. Порога нет первые четыре недели,
# тогда тревоги нет и быть не может, это записано в README.
ROW="$(docker exec -i rent_pg psql -qtA -F'|' -U rent -d rent -c \
  "select mape_pct, coalesce(threshold_pct::text, '') from ml.price_drift
    order by week_start desc limit 1;" 2>/dev/null)"
MAPE="${ROW%%|*}"; THR="${ROW##*|}"
say "Ошибка на новой неделе ${MAPE}%, порог ${THR:-еще не выведен}"
if [ -n "$THR" ] && awk -v m="$MAPE" -v t="$THR" 'BEGIN { exit !(m > t) }'; then
  notify price-drift "$(date '+%d.%m %H:%M') ВНИМАНИЕ: ошибка модели цены на новой неделе ${MAPE}% выше порога ${THR}%. Рынок сдвинулся или сбор поменялся, оценки /price осторожнее."
fi
exit 0
