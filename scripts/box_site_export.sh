#!/bin/bash
# Ежедневная выгрузка агрегатов витрины с бокса в репозиторий.
#
# Пересчитывает site/data/site.json из базы и пушит один этот файл через
# деплой-ключ, у которого права только на этот репозиторий. Сайт
# пересобирает GitHub Actions (.github/workflows/pages.yml). Если бокс
# выключат, сайт продолжит показывать последнюю дату и скажет, сколько
# дней она не обновлялась.
#
# Когда. 19:30 по Москве: ежедневный обход в 12:00 к этому часу кончается
# даже в плохой день, а замок проверяется, чтобы не мешать догону.
#
# Ключ. ~/.ssh/rent_site_deploy, деплой-ключ с правом записи, выдан только
# на nata2627/msk-rent-market. Срок у деплой-ключа GitHub не ставит, он
# живет до отзыва; дата выдачи записывается в README и operations.md.
# Отказ доступа уходит тревогой через box_notify.sh.

export TZ=Europe/Moscow
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1
LOG_DIR="$REPO/data/logs"; mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/site_$(date +%Y%m).log" 2>&1
say() { echo "$(date '+%d.%m %H:%M:%S') === $*"; }
notify() { bash "$REPO/scripts/box_notify.sh" "$@" >/dev/null 2>&1; }

[ -d "$REPO/data/box.lock" ] && { say "Идет обход, выгрузку пропускаю"; exit 0; }

timeout 600 docker run --rm --network host --memory=500m --memory-swap=500m \
  -v "$REPO":/work -w /work --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -e TZ=Europe/Moscow rent-collector:latest python -m src.site_data \
  || { say "Пересчет витрины упал"; notify site-fail "$(date '+%d.%m %H:%M') ВНИМАНИЕ: агрегаты витрины не пересчитались, сайт показывает прошлую дату."; exit 1; }

git diff --quiet -- site/data/site.json && { say "Нечего выгружать"; exit 0; }
export GIT_SSH_COMMAND="ssh -i $HOME/.ssh/rent_site_deploy -o IdentitiesOnly=yes -o BatchMode=yes"
git -c user.name=nata2627 -c user.email=gordeevanatalia04@gmail.com \
  commit -q -m "Данные витрины за $(date +%d.%m.%Y)" -- site/data/site.json
if ! timeout 120 git push -q origin HEAD:main; then
  say "Пуш отклонен"
  notify site-push "$(date '+%d.%m %H:%M') ВНИМАНИЕ: выгрузка витрины не прошла на GitHub. Проверить деплой-ключ или расхождение веток."
  exit 1
fi
say "Выгружено"
