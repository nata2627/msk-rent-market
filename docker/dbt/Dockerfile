# Образ dbt для бокса. На машине сбора нет ни venv, ни sudo, поэтому dbt
# живет в своем контейнере, как сбор и модель цены. Не поверх образа
# сбора: dbt тянет десятки пакетов с закрепленными версиями, и делить с
# обходом одно окружение значит рано или поздно сломать обход.
#
#   docker build -t rent-dbt -f docker/dbt/Dockerfile .
FROM python:3.11
COPY requirements-dbt.txt /tmp/requirements-dbt.txt
RUN pip install --no-cache-dir -r /tmp/requirements-dbt.txt
