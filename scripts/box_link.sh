#!/bin/bash
# Одно объявление по ссылке из бота (etl/link_fetch.py) в контейнере сбора.
#
#   scripts/box_link.sh 336070353
#
# Печатает строку JSON для бота, журнал идет в data/logs/link.log.
#
# Почему без замка обхода. Обходу мешает второй браузер в его профиле, а
# здесь профиль свой (data/browser_profile_link), поэтому ссылка
# открывается и во время обхода. Два открытия подряд бот не запускает
# сам: у него на это один замок на процесс.
#
# Дисплей :98, а не :99 обхода: сеть у контейнеров общая с машиной, и
# одинаковый номер мог бы столкнуться. Память 1,5 ГиБ: одна страница без
# картинок, у обхода на выдаче честный пик 413 МБ браузера.

export TZ=Europe/Moscow
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ID="$1"
case "$ID" in
  ''|*[!0-9]*) echo '{"status": "error", "message": "нужен номер объявления"}'; exit 2 ;;
esac
mkdir -p "$REPO/data/logs"

timeout 150 docker run --rm --init \
  --name "rent_link_$ID" \
  --network host \
  --shm-size=1g \
  --memory=1500m --memory-swap=1500m \
  -v "$REPO":/work -w /work \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -e TZ=Europe/Moscow -e CHROME_NO_SANDBOX=1 -e PYTHONUNBUFFERED=1 \
  rent-collector:latest \
  bash -c "Xvfb :98 -screen 0 1440x900x24 >/tmp/xvfb.log 2>&1 &
           sleep 2
           DISPLAY=:98 python -m etl.link_fetch $ID" \
  2>>"$REPO/data/logs/link.log"
CODE=$?
# timeout снимает только клиент docker, контейнер без него доживал бы сам.
if [ "$CODE" = 124 ]; then
  docker rm -f "rent_link_$ID" >/dev/null 2>&1
  echo '{"status": "error", "message": "страница не открылась за две с половиной минуты"}'
fi
exit 0
