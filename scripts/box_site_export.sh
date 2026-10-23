#!/bin/bash
# Ежедневная выгрузка агрегатов витрины с Mac mini на GitHub.
#
# Пересчитывает site/data/site.json из базы и пушит его вместе со сводкой
# модели цены (results/price_*), если та поменялась, через деплой-ключ с
# правом только на этот репозиторий. Сайт пересобирает GitHub Actions.
# Если машину выключат, сайт продолжит показывать последнюю дату и
# скажет, сколько дней она не обновлялась.
#
# Код на машине лежит копией без git, поэтому наружу файлы уходят из
# отдельного клона ~/rent_site.
#
# Когда. 19:30 по Москве и только если день уже собран: обход важнее, а
# замок берется тот же, что у обхода, чтобы они не шли вместе (обход в
# пике около 2,4 ГБ из 3 ГиБ бокса, пересчет до 500 МБ).
#
# Ключ. ~/.ssh/rent_site_deploy, деплой-ключ, выдан 23.10.2026. Срока у
# деплой-ключей GitHub нет, живет до отзыва. Отказ доступа уходит
# тревогой через box_notify.sh.

export TZ=Europe/Moscow
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SITE="$HOME/rent_site"
cd "$REPO" || exit 1
LOG_DIR="$REPO/data/logs"; mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/site_$(date +%Y%m).log" 2>&1
say() { echo "$(date '+%d.%m %H:%M:%S') === $*"; }
notify() { bash "$REPO/scripts/box_notify.sh" "$@" >/dev/null 2>&1; }
find "$LOG_DIR" -name 'site_*.log' -mtime +60 -delete 2>/dev/null

LOCK="$REPO/data/box.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  say "Идет обход или переобучение, выгрузку пропускаю"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

export GIT_SSH_COMMAND="ssh -i $HOME/.ssh/rent_site_deploy -o IdentitiesOnly=yes -o BatchMode=yes"
if ! timeout 120 git -C "$SITE" pull -q --rebase; then
  say "Клон сайта не обновился"
  notify site-push "Витрина не обновилась
$(date '+%d.%m, %H:%M')

Клон репозитория на машине сбора не подтянул изменения с GitHub. Сайт показывает прошлую дату, данные панели не задеты."
  exit 1
fi

if ! timeout 600 docker run --rm --network host --memory=500m --memory-swap=500m \
  -v "$REPO":/work -w /work -v "$SITE":/site --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -e TZ=Europe/Moscow \
  -e RESULTS_DIR=/site/results -e SITE_OUT=/site/site/data/site.json \
  rent-collector:latest python -m src.site_data; then
  say "Пересчет витрины упал"
  notify site-fail "Витрина не пересчиталась
$(date '+%d.%m, %H:%M')

Сайт показывает прошлую дату, данные панели не задеты."
  exit 1
fi

cd "$SITE" || exit 1
git add site/data/site.json results/price_model.json results/price_runs.csv 2>/dev/null
if git diff --cached --quiet; then
  say "Нечего выгружать"
  exit 0
fi
git -c user.name=nata2627 -c user.email=gordeevanatalia04@gmail.com \
  commit -q -m "Данные витрины за $(date +%d.%m.%Y)"
if ! timeout 120 git push -q origin HEAD:main; then
  say "Пуш отклонен"
  notify site-push "Витрина не выгрузилась на GitHub
$(date '+%d.%m, %H:%M')

Проверить деплой-ключ rent_site_deploy или расхождение веток. Сайт показывает прошлую дату."
  exit 1
fi
say "Выгружено"
