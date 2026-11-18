#!/bin/bash
# Недельное переобучение модели справедливой цены на Mac mini.
#
# Когда. Воскресенье, попытки в 21:00, 22:00 и 23:00 по Москве: недельный
# добор начинается в 18:00 (с 18.11.2026, чтобы забрать воскресные
# объявления и не идти сразу за срезом) и с перерывом идет около 2,5 часа.
# Если добор оборвется и повторы займут замок до полуночи, остается
# запасная попытка в понедельник 21:00; сделанная неделя ее пропускает. Модель этой недели уже
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

# Неделя с воскресенья (%U), как в mac_price_weekly.sh: по ISO обучение в
# будний день занимало воскресенье той же недели (08.11.2026).
WEEK="$(date +%Y-U%U)"
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
# Недели старше восьми последних не нужны: в MLflow остаются их метрики.
# У недели три файла: модель цены, модель ухода (-exit) и описание для
# ежедневного пересчета (.json), поэтому список идет по описаниям, а
# старые модели без описания (до 17.11.2026) по самим моделям.
for OLD in $(ls -1t "$REPO"/data/models/price-*.json 2>/dev/null | tail -n +9); do
  rm -f "${OLD%.json}.json" "${OLD%.json}.cbm" "${OLD%.json}-exit.cbm"
done
ls -1t "$REPO"/data/models/price-????????.cbm 2>/dev/null | tail -n +9 | xargs -r rm -f

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
