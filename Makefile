VENV := .venv
PY   := $(VENV)/bin/python
PG   := docker compose exec -T postgres psql -v ON_ERROR_STOP=1 \
          -U $${POSTGRES_USER:-rent} -d $${POSTGRES_DB:-rent}

.PHONY: dashboard dedup help venv up down schema access narrow full dryrun xlsx pets-calibrate wayback parks marts checks report flats drops districts psql logs test selftest clean

help:
	@echo "make venv    — создать окружение и поставить зависимости"
	@echo "make up      — поднять PostgreSQL в Docker"
	@echo "make schema  — применить DDL повторно (идемпотентно)"
	@echo "make access  — один запрос: отдаёт ли ЦИАН данные прямо сейчас"
	@echo "make dryrun  — проверить нарезку и доступность площадки, без записи"
	@echo "make narrow  — ежедневный узкий срез (1-2 комнаты, вся Москва)"
	@echo "make full    — еженедельный полный обход Москвы"
	@echo "make xlsx FILE=... — импорт ручной выгрузки ЦИАНа в Excel"
	@echo "make pets-calibrate FILE=... — проверить разбор животных по эталону"
	@echo "make wayback — исторические объявления из веб-архива"
	@echo "make parks   — расстояние до парков по OpenStreetMap"
	@echo "make dedup   — схлопнуть дубли объявлений"
	@echo "make rebuild — пересобрать панель из сырых снимков"
	@echo "make marts   — пересобрать материализованные витрины"
	@echo "make checks  — проверки качества собранных данных"
	@echo "make flats   — показать лучшие варианты в моём сегменте"
	@echo "make drops   — показать подешевевшие объявления"
	@echo "make districts — сравнить районы по цене"
	@echo "make report  — всё вместе"
	@echo "make dashboard — собрать дашборд в Metabase (подбор + рынок)"
	@echo "make psql    — консоль базы"
	@echo "make test    — прогнать тесты"
	@echo "make selftest— сквозная проверка записи и панели на фикстуре"

venv:
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

up:
	docker compose up -d
	@echo "Postgres: localhost:$${POSTGRES_PORT:-5433}"

down:
	docker compose down

schema:
	@for f in sql/schema/0*.sql; do echo ">> $$f"; \
	  $(PG) -f /docker-entrypoint-initdb.d/$$(basename $$f); done

access:
	$(PY) -m etl.check_access

# Через настоящий Chrome — работает, когда площадка закрыта для Python.
# Окно браузера будет видно: в headless-режиме площадка отвечает 403.
access-browser:
	$(PY) -m etl.check_access --browser

# Предохранитель на 40 запросов: проверить, что площадка отвечает и нарезка
# работает, не устраивая полуторачасовой обход.
dryrun:
	$(PY) -m etl.collect --mode narrow --dry-run --max-requests 40

narrow:
	$(PY) -m etl.collect --mode narrow --transport browser

full:
	$(PY) -m etl.collect --mode full --transport browser

# Ручная выгрузка с кнопки «Сохранить файл Excel» на странице выдачи.
# Отдаёт не больше 200 объявлений и верхушку промо-ранжирования, поэтому
# заменой сбору не является — но содержит анкетный флаг животных.
FILE ?= data/raw/offers.xlsx

xlsx:
	$(PY) -m etl.xlsx_import $(FILE)

pets-calibrate:
	$(PY) -m etl.pets_calibrate $(FILE)

# Исторические снимки выдачи из веб-архива. Настоящие объявления с ценами
# за прошлые годы — то, чего живой сбор не даст в принципе.
SNAPSHOTS ?= 30
MONTHS ?= 202409,202410

wayback:
	$(PY) -m etl.wayback --months $(MONTHS) --limit $(SNAPSHOTS)

wayback-list:
	$(PY) -m etl.wayback --list

# Парки из OpenStreetMap. --fetch нужен один раз, дальше только пересчёт.
parks-fetch:
	$(PY) -m etl.parks --fetch

dedup:
	$(PY) -m src.dedup

dedup-report:
	$(PY) -m src.dedup --report

# Сверка с дедупликацией самой площадки: блок «У других арендодателей».
dedup-validate:
	$(PY) -m src.dedup --validate

# Пересборка панели из сырых снимков — без обращения к площадке.
rebuild:
	$(PY) -m etl.rebuild_core

parks:
	$(PY) -m etl.parks

marts:
	$(PG) -c "select marts.refresh_all();"

checks:
	@for f in sql/checks/*.sql; do echo ">> $$f"; $(PG) < $$f; done

flats:
	@$(PG) < sql/queries/01_my_flats.sql

drops:
	@$(PG) < sql/queries/02_price_drops.sql

districts:
	@$(PG) < sql/queries/03_districts.sql

report: districts flats drops

dashboard:
	$(PY) scripts/setup_dashboard.py

psql:
	docker compose exec postgres psql -U $${POSTGRES_USER:-rent} -d $${POSTGRES_DB:-rent}

logs:
	docker compose logs -f --tail=50

test:
	$(VENV)/bin/pytest -q tests

selftest:
	$(PY) -m etl.selftest

clean:
	docker compose down -v
