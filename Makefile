VENV := .venv
PY   := $(VENV)/bin/python
PG   := docker compose exec -T postgres psql -v ON_ERROR_STOP=1 \
          -U $${POSTGRES_USER:-rent} -d $${POSTGRES_DB:-rent}

.PHONY: hedonic hedonic-full pets-backfill disappeared-backfill terms-backfill fields districts-geo scrub weekly schedule-weekly unschedule-weekly schedule unschedule schedule-status catchup survival drops-analysis figures readme-numbers daily districts-ref dashboard dedup help venv up down schema access narrow full dryrun xlsx pets-calibrate wayback parks marts checks report flats drops districts psql logs test selftest clean

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
	@echo "make hedonic — гедоническая регрессия: цена разрешения на животных"
	@echo "make terms-backfill — пересчитать предоплату и залог из описаний"
	@echo "make fields   — пересобрать опись полей в docs/fields.md"
	@echo "make districts-geo — добрать контуры районов для карты из OSM"
	@echo "make districts-ref — пересобрать справочник районов из собранных страниц"
	@echo "make daily    — весь ежедневный прогон разом (сбор + пересчёт)"
	@echo "make schedule HOUR=10 — ставить прогон на расписание, ежедневно"
	@echo "make weekly   — добрать комнатности 3+ (в ежедневный срез они не входят)"
	@echo "make schedule-weekly — добор раз в неделю, по воскресеньям"
	@echo "make schedule-status — идёт ли расписание и чем кончились прогоны"
	@echo "make catchup DATE=2026-10-04 ROOMS=1,2 — догнать пропущенный день"
	@echo "make survival — срок жизни объявления до снятия"
	@echo "make drops-analysis — кто снижает цену и через сколько дней"
	@echo "make figures — перерисовать графики README"
	@echo "make readme-numbers — подставить в README свежие числа из базы"
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
	$(PY) -m etl.districts

full:
	$(PY) -m etl.collect --mode full --transport browser
	$(PY) -m etl.districts

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

# Справочник районов для нарезки полного обхода. Собирается из уже
# скачанных страниц, к площадке не обращается. Служебный эндпоинт, которым
# он брался раньше, отвечает 404.
# --- расписание ---------------------------------------------------------
# Ежедневный прогон через launchd. Не cron: сбору нужен видимый Chrome,
# то есть живой графический сеанс, а пропущенный из-за сна запуск launchd
# выполняет при пробуждении.
#
# Час задаётся здесь: make schedule HOUR=10 MINUTE=30
HOUR ?= 10
MINUTE ?= 0
LABEL := com.nata2627.rent-msk.daily
PLIST := $(HOME)/Library/LaunchAgents/$(LABEL).plist

# macOS не пускает фоновые задачи в «Рабочий стол», «Документы» и
# «Загрузки». Установить расписание оттуда можно, но работать оно не
# будет — задача падает с Operation not permitted ещё до первой строки
# скрипта, и узнать об этом получится только через сутки по пустому
# журналу. Поэтому проверка здесь, а не в комментарии.
schedule:
	@case "$(CURDIR)" in \
	  $(HOME)/Desktop/*|$(HOME)/Documents/*|$(HOME)/Downloads/*) \
	    echo "Проект лежит в $(CURDIR)."; \
	    echo "macOS не пускает туда фоновые задачи — расписание не заработает."; \
	    echo "Перенесите папку за пределы Desktop/Documents/Downloads."; \
	    exit 1;; \
	esac
	@mkdir -p $(HOME)/Library/LaunchAgents
	@sed -e 's|__REPO__|$(CURDIR)|g' \
	     -e 's|__LABEL__|$(LABEL)|g' \
	     -e 's|__HOUR__|$(HOUR)|g' \
	     -e 's|__MINUTE__|$(MINUTE)|g' \
	     scripts/daily.plist.template > $(PLIST)
	@launchctl bootout gui/$$(id -u)/$(LABEL) 2>/dev/null || true
	@launchctl bootstrap gui/$$(id -u) $(PLIST)
	@echo "Прогон поставлен на $(HOUR):$(shell printf %02d $(MINUTE)) ежедневно."
	@echo "Проверить: make schedule-status"

# Недельный добор комнатностей, которых нет в ежедневном срезе.
# Воскресенье по умолчанию, час позже ежедневного прогона: замок у них
# общий, и добор дождётся окончания обхода, а не пропустится молча.
WEEKDAY ?= 0
WEEKLY_HOUR ?= 13
WEEKLY_LABEL := com.nata2627.rent-msk.weekly
WEEKLY_PLIST := $(HOME)/Library/LaunchAgents/$(WEEKLY_LABEL).plist

schedule-weekly:
	@mkdir -p $(HOME)/Library/LaunchAgents
	@sed -e 's|__REPO__|$(CURDIR)|g' \
	     -e 's|__LABEL__|$(WEEKLY_LABEL)|g' \
	     -e 's|__WEEKDAY__|$(WEEKDAY)|g' \
	     -e 's|__HOUR__|$(WEEKLY_HOUR)|g' \
	     scripts/weekly.plist.template > $(WEEKLY_PLIST)
	@launchctl bootout gui/$$(id -u)/$(WEEKLY_LABEL) 2>/dev/null || true
	@launchctl bootstrap gui/$$(id -u) $(WEEKLY_PLIST)
	@echo "Недельный добор: день недели $(WEEKDAY) (0 — воскресенье), $(WEEKLY_HOUR):00."

unschedule-weekly:
	@launchctl bootout gui/$$(id -u)/$(WEEKLY_LABEL) 2>/dev/null || true
	@rm -f $(WEEKLY_PLIST)
	@echo "Недельный добор снят."

# Догон пропущенного дня. Та же дата снимка, только нужные комнатности;
# уже собранное не задваивается ключом панели.
#
#   make catchup                      # сегодня, комнатности 1,2
#   make catchup DATE=2026-10-04 ROOMS=1,2
#
# Разовой задачей launchd, а не прямо из терминала. Часовой обход,
# запущенный из сеанса, умирает вместе с ним — так 26.09.2026 прогон
# оборвался на 191-й корзине из 294. Задача снимает себя сама, в начале
# работы скрипта, поэтому второй раз при входе в систему не выстрелит.
CATCHUP_LABEL := com.nata2627.rent-msk.catchup
CATCHUP_PLIST := $(HOME)/Library/LaunchAgents/$(CATCHUP_LABEL).plist

catchup:
	@mkdir -p $(HOME)/Library/LaunchAgents
	@d="$(DATE)"; [ -n "$$d" ] || d="$$(date +%F)"; \
	 r="$(ROOMS)"; [ -n "$$r" ] || r="1,2"; \
	 sed -e 's|__REPO__|$(CURDIR)|g' \
	     -e 's|__LABEL__|$(CATCHUP_LABEL)|g' \
	     -e 's|__PLIST__|$(CATCHUP_PLIST)|g' \
	     -e "s|__DATE__|$$d|g" \
	     -e "s|__ROOMS__|$$r|g" \
	     scripts/catchup.plist.template > $(CATCHUP_PLIST); \
	 launchctl bootout gui/$$(id -u)/$(CATCHUP_LABEL) 2>/dev/null || true; \
	 launchctl bootstrap gui/$$(id -u) $(CATCHUP_PLIST); \
	 echo "Догон поставлен: дата $$d, комнатности $$r."
	@echo "Он ждёт освобождения замка, до трёх часов. Журнал — data/logs/catchup_*.log"

# Добор руками, без расписания.
weekly:
	@./scripts/weekly.sh; tail -3 data/logs/daily_summary.log

unschedule:
	@launchctl bootout gui/$$(id -u)/$(LABEL) 2>/dev/null || true
	@rm -f $(PLIST)
	@echo "Расписание снято."

schedule-status:
	@launchctl print gui/$$(id -u)/$(LABEL) 2>/dev/null \
	  | grep -E 'state|last exit code|program|runs' || echo "Не установлено."
	@echo "--- последние прогоны ---"
	@tail -6 data/logs/daily_summary.log 2>/dev/null || echo "прогонов ещё не было"

daily:
	@./scripts/daily.sh; tail -3 data/logs/daily_summary.log

districts-ref:
	$(PY) -m etl.districts

# Гедоническая регрессия: сколько стоит разрешение держать животное.
# Шесть спецификаций, от разницы средних до сопоставления внутри ячеек.
hedonic:
	$(PY) -m src.hedonic

# Второй и третий вопросы проекта. Оба считаются из панели и не ходят
# на площадку, поэтому запускать можно сколько угодно.
survival:      ## срок жизни объявления до снятия
	$(PY) -m src.survival

drops-analysis: ## кто снижает цену и через сколько дней
	$(PY) -m src.drops

figures:       ## перерисовать графики для README из текущих данных
	$(PY) -m src.figures

readme-numbers: ## подставить в README свежие числа из базы
	$(PY) -m src.readme_numbers

hedonic-full:
	$(PY) -m src.hedonic --coefficients

# Повторить проход с фильтром животных по уже сохранённым корзинам.
# Нужен, только если разметка потерялась: обычный сбор делает это сам.
pets-backfill:
	$(PY) -m etl.pets_backfill --date $(DATE)

# Пересчитать снятия с публикации по всей панели. К площадке не
# обращается: в какие дни какая комнатность собиралась, записано в
# журнале обхода. Нужен после `make rebuild` и после правок правила.
disappeared-backfill:
	$(PY) -m etl.disappeared_backfill

# Пересчитать условия платежа (предоплата и залог в месяцах) из уже
# сохранённых описаний. К площадке не обращается — нужен после правок
# разбора, чтобы старые дни догнали новое правило.
terms-backfill:
	$(PY) -m etl.terms_backfill

# Опись собираемых полей с заполненностью. Пересобирается командой, а не
# пишется руками: список полей меняется вместе со схемой, а смотрят на
# него, решая, что брать в оценку варианта.
fields:
	$(PY) -m etl.field_report > docs/fields.md
	@echo "docs/fields.md пересобран"

# Контуры районов для карты. К площадке не обращается, ходит в
# OpenStreetMap. --list только покажет, чего не хватает.
districts-geo:
	$(PY) -m etl.district_geo

# Разовая чистка персональных данных в уже собранном. Новые данные
# чистятся при записи, см. etl/privacy.py.
scrub:
	$(PY) -m etl.scrub_existing --dry-run

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
