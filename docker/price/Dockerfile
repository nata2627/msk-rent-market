# Образ недельного переобучения модели цены. Поверх образа сбора, чтобы
# Python и зависимости проекта были теми же, что у обхода, а ML-пакеты не
# раздували сам образ сбора: тому они не нужны.
#
#   docker build -t rent-price -f docker/price/Dockerfile .
FROM rent-collector:latest
COPY requirements-ml.txt /tmp/requirements-ml.txt
RUN pip install --no-cache-dir -r /tmp/requirements-ml.txt
