#!/bin/bash
# Витрины на боксе через dbt, в контейнере rent-dbt.
#
#   scripts/box_marts.sh                    # рабочая сборка в marts
#   DBT_SCHEMA=dbt_marts scripts/box_marts.sh   # сборка рядом, для сверки
#
# build, а не run: тесты моделей идут сразу за сборкой, и витрина с
# нарушенным ключом или чужим состоянием животных дает ненулевой код, а
# не тихо уходит в бот и на витрину.
#
# Замок не берется: скрипт зовут изнутри box_daily.sh, который уже держит
# его. Запуская руками, следить, чтобы обход не шел.
#
# --memory: dbt с двумя потоками на этой базе держит около 200 МБ, пик
# самих запросов приходится на Postgres, а не на контейнер. 600 МБ с
# запасом и не дают ему отъесть память у базы.
# target и журналы dbt во временном каталоге контейнера: на диске бокса
# от них проку нет, а итог сборки печатается в журнал прогона.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
timeout 1800 docker run --rm \
  --name rent_dbt \
  --network host \
  --memory=600m --memory-swap=600m \
  -v "$REPO":/work -w /work/dbt \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -e TZ=Europe/Moscow \
  -e DBT_SCHEMA="${DBT_SCHEMA:-marts}" \
  rent-dbt:latest \
  bash -c 'set -a; . /work/.env; set +a
           dbt build --target-path /tmp/target --log-path /tmp/dbt-logs --no-use-colors'
