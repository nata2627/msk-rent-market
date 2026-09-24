"""Единый дашборд проекта: две вкладки в одном месте.

Почему вкладки, а не два дашборда. Поиск и аналитика отвечают на разные
вопросы и не должны мешать друг другу: фильтры подбора не могут влиять на
витрину рынка, иначе сравнение «мой сегмент против рынка» потеряет смысл —
сегмент будет меняться вместе с фильтром. Но и разводить это по разным
адресам незачем: вкладки дают и разделение, и одно место входа.

Вкладка «Подбор» — инструмент: условия задаются фильтрами, всё
пересчитывается. Вкладка «Рынок» — выводы: фиксированная база, фильтры на
неё не действуют.

Запуск: python scripts/setup_dashboard.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
import uuid
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from etl.config import DB  # noqa: E402

MB = "http://localhost:3000"
EMAIL, PASSWORD = "", ""
DB_ID = 3
DASH_NAME = "Аренда Москвы"

SESSION = json.loads(urllib.request.urlopen(urllib.request.Request(
    f"{MB}/api/session", method="POST",
    data=json.dumps({"username": EMAIL, "password": PASSWORD}).encode(),
    headers={"Content-Type": "application/json"})).read())["id"]


def api(path, method="GET", payload=None):
    req = urllib.request.Request(
        f"{MB}{path}", method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"X-Metabase-Session": SESSION, "Content-Type": "application/json"})
    body = urllib.request.urlopen(req).read()
    return json.loads(body) if body else None


def districts() -> list[str]:
    """Список районов — из самих данных, а не зашитый в код.

    Состав районов приходит от площадки и меняется; разъехавшийся список
    молча отфильтровал бы всё в ноль.
    """
    with psycopg.connect(**DB) as c, c.cursor() as cur:
        cur.execute("""select district from core.offer
                       where source='cian' and district is not null
                       group by 1 having count(*) >= 3 order by 1""")
        return [r[0] for r in cur.fetchall()]


# ---------------------------------------------------------------------
# Фильтры вкладки «Подбор»
# ---------------------------------------------------------------------
FILTERS = [
    ("max_price", "Цена до, руб/мес", "number", "number/<=", None),
    ("min_area", "Площадь от, м²", "number", "number/>=", None),
    ("rooms", "Комнат", "number", "number/=",
     [[1, "1"], [2, "2"], [3, "3"], [4, "4"], [5, "5"]]),
    ("max_metro", "До метро пешком", "number", "number/<=",
     [[5, "до 5 минут"], [10, "до 10 минут"], [15, "до 15 минут"], [20, "до 20 минут"]]),
    # Метры сами по себе ничего не говорят: 800 — это близко или далеко?
    ("max_park", "Парк рядом", "number", "number/<=",
     [[500, "пешком, до 500 м"], [1000, "до 1 км"], [2000, "до 2 км"], [5000, "до 5 км"]]),
    ("pets", "Животные", "text", "string/=",
     [["allowed", "можно"], ["negotiable", "по договорённости"],
      ["forbidden", "нельзя"], ["unknown", "не указано"]]),
    # На этапе отсева вопрос двоичный: платить посреднику или нет.
    ("fee", "Комиссия", "text", "string/=",
     [["no", "без комиссии"], ["yes", "с комиссией"]]),
    ("freshness", "Опубликовано", "number", "number/<=",
     [[0, "сегодня"], [3, "за 3 дня"], [7, "за неделю"], [30, "за месяц"]]),
    ("max_entry", "Нужно денег сразу, до", "number", "number/<=", None),
    # Внутри МКАД или за ним. Считается по настоящей геометрии кольца,
    # а не по списку районов: Выхино-Жулебино лежит по обе стороны,
    # и никакой список этого не разделит.
    ("mkad", "Где", "text", "string/=",
     [["in", "внутри МКАД"], ["out", "за МКАД"]]),
    ("district", "Район", "text", "string/=", "DISTRICTS"),
]

# Сколько денег нужно отдать единовременно при заселении: первый месяц
# плюс залог плюс комиссия агенту. Именно эта сумма определяет, потянешь
# ли вариант прямо сейчас, — а вовсе не месячная плата. На проверенных
# данных четыре квартиры с одинаковой ценой 60 000 требовали на входе
# от 60 000 до 710 000 рублей, то есть разброс в двенадцать раз.
ENTRY = ("(price + coalesce(deposit_rub, 0) "
         "+ price * coalesce(client_fee_pct, 0) / 100.0)")

WHERE = f"""
  -- Дубли из подбора убираются: одна квартира не должна занимать три
  -- строки списка. В аналитике на вкладке «Рынок» они остаются видны.
  and coalesce(is_duplicate, false) = false
  and coalesce(is_daily, false) = false
  and coalesce(is_apartments, false) = false
  and coalesce(lease_term_type, 'longTerm') = 'longTerm'
  and price is not null
  [[and price <= {{{{max_price}}}}]]
  [[and total_area >= {{{{min_area}}}}]]
  [[and rooms_count = {{{{rooms}}}}]]
  [[and metro_walk_min <= {{{{max_metro}}}}]]
  [[and large_park_distance_m <= {{{{max_park}}}}]]
  [[and pets_state = {{{{pets}}}}]]
  [[and district = {{{{district}}}}]]
  [[and {ENTRY} <= {{{{max_entry}}}}]]
  [[and ((({{{{fee}}}}) = 'no'  and coalesce(client_fee_pct, 0) = 0)
      or (({{{{fee}}}}) = 'yes' and coalesce(client_fee_pct, 0) > 0))]]
  [[and published_at >= current_date - ({{{{freshness}}}})]]
  [[and ((({{{{mkad}}}}) = 'in'  and inside_mkad)
      or (({{{{mkad}}}}) = 'out' and not inside_mkad))]]
"""


def tags():
    return {slug: {"id": str(uuid.uuid4()), "name": slug,
                   "display-name": label, "type": kind, "required": False}
            for slug, label, kind, _, _ in FILTERS}


def search_card(name, select, display="table", viz=None, order="order by price",
                wrap=None):
    sql = f"select {select}\nfrom marts.offer_segment\nwhere source = 'cian'\n{WHERE}\n{order}"
    if wrap:
        # Двухступенчатая сортировка: сначала отбор, потом показ.
        #
        # Наивное «order by район» вместе с limit 300 отрезало бы всё после
        # буквы «М» — Ясенево и Южнопортовый исчезли бы из таблицы молча.
        # Отбираем самые дешёвые, а по алфавиту раскладываем уже их.
        sql = f"select * from (\n{sql}\n) t\n{wrap}"
    return {"name": name, "display": display, "filtered": True,
            "dataset_query": {"type": "native", "database": DB_ID,
                              "native": {"query": sql, "template-tags": tags()}},
            "visualization_settings": viz or {}}


def market_card(name, sql, display="table", viz=None):
    return {"name": name, "display": display, "filtered": False,
            "dataset_query": {"type": "native", "database": DB_ID,
                              "native": {"query": sql}},
            "visualization_settings": viz or {}}


# ---------------------------------------------------------------------
SEARCH = [
    search_card("Найдено", 'count(*) as "Найдено"', "scalar", order=""),

    search_card("Карта: где искать", """
           district as "Район", count(*) as "Вариантов",
           percentile_cont(0.5) within group (order by price)::int as "Медиана\"""",
                "map",
                {"map.type": "region", "map.region": "moscow_districts",
                 "map.dimension": "Район", "map.metric": "Медиана"},
                order="and district is not null group by 1 having count(*) >= 1"),

    # Ссылка вторым столбцом и коротким текстом.
    #
    # Полный адрес объявления занимал половину ширины таблицы и вытеснял
    # всё остальное за горизонтальную прокрутку, а кликать по нему надо
    # было, домотав до правого края. Metabase умеет показывать столбец
    # как ссылку с произвольной подписью — в данных остаётся адрес,
    # в таблице видно слово «открыть».
    search_card("Найденные квартиры", f"""
           -- Пустой район подписывается явно. В сортировке по алфавиту
           -- null уходит в самый конец, и десяток безымянных строк внизу
           -- выглядит как обрыв таблицы, а не как «площадка не указала».
           coalesce(district, '— район не указан') as "Район",
           split_part(url, '?', 1) as "Ссылка",
           coalesce(rooms_count::text, 'студия') as "Комн",
           total_area::int as "м²",
           price::int as "Цена", (price / total_area)::int as "За м²",
           {ENTRY}::int as "Нужно сразу",
           case when coalesce(client_fee_pct, 0) > 0
                then client_fee_pct::int || '%' else 'нет' end as "Комиссия",
           case when large_park_distance_m <= 500 then 'пешком'
                when large_park_distance_m <= 1000 then 'до 1 км'
                when large_park_distance_m <= 2000 then 'до 2 км'
                else 'далеко' end as "Парк",
           metro_walk_min as "Метро, мин",
           floor || '/' || floors_total as "Этаж", repair_type as "Ремонт",
           case pets_state when 'allowed' then 'можно'
                           when 'negotiable' then 'по договорённости'
                           when 'forbidden' then 'нельзя'
                           else 'не указано' end as "Животные",
           -- Долго висящее объявление — повод торговаться.
           (current_date - published_at::date) as "Висит, дней\"""",
                wrap='order by "Район", "Цена"',
                viz={"column_settings": {
                        '["name","Ссылка"]': {
                            "view_as": "link",
                            "link_url": "{{Ссылка}}",
                            "link_text": "открыть",
                        }},
                     "table.cell_column": "Цена"},
                order="order by price limit 300"),

    # floor, а не ::int: приведение к целому в Postgres ОКРУГЛЯЕТ, и
    # корзины получались центрированными вместо выровненных по границе —
    # 32 000 попадало в «30», а 33 000 уже в «35». Для гистограммы это
    # означает, что подписи не соответствуют содержимому столбца.
    search_card("Как распределены цены по этому запросу", """
           (floor(price / 5000) * 5)::int || '–' ||
           (floor(price / 5000) * 5 + 5)::int as "Цена, тыс. руб",
           count(*) as "Объявлений\"""",
                "bar",
                {"graph.dimensions": ["Цена, тыс. руб"], "graph.metrics": ["Объявлений"],
                 "graph.show_values": True,
                 "graph.label_value_frequency": "all",
                 "graph.x_axis.title_text": "Цена, тысяч рублей в месяц",
                 "graph.y_axis.title_text": "Сколько объявлений"},
                order="group by floor(price / 5000) order by floor(price / 5000)"),

    # Число в название не выносится.
    #
    # «Двенадцать самых доступных» — обещание, которое запрос не даёт:
    # limit 12 это потолок, а порог «хотя бы три объявления в районе»
    # при узких фильтрах оставляет и девять районов, и три. Заголовок
    # с числом тогда просто врёт.
    #
    # Двенадцать строк, а не двадцать, — это про другое.
    #
    # Metabase сворачивает всё сверх своего предела в категорию «Other» и
    # СУММИРУЕТ значения. Для медиан сумма бессмысленна: на графике
    # появлялся столбец «Other (4)» в 324 тысячи, которого нет в природе.
    # Настройка, отключающая свёртку, в этой версии не срабатывает,
    # поэтому предел задан там, где он точно соблюдается, — в самом
    # запросе. Полный список районов есть на карте рядом.
    search_card("Где дешевле по этому запросу", """
           district || ' (' || count(*) || ')' as "Район",
           percentile_cont(0.5) within group (order by price)::int as "Медиана\"""",
                "row",
                {"graph.dimensions": ["Район"], "graph.metrics": ["Медиана"],
                 "graph.show_values": True,
                 "graph.max_categories_enabled": False,
                 "graph.max_categories": 12,
                 "graph.x_axis.title_text": "Медиана, руб./мес",
                 "graph.y_axis.title_text": "Район (в скобках — сколько вариантов)"},
                order="""and district is not null
                         group by district having count(*) >= 3
                         order by 2 limit 12"""),
]

# Последний ЗАВЕРШЁННЫЙ обход, а не просто последняя дата в панели.
#
# Пока сбор идёт, сегодняшний день уже есть в наблюдениях, но собран
# наполовину. Витрина рынка, посчитанная по нему, покажет не рынок, а то,
# до какой корзины успел дойти обход, — и заметить это будет нечем.
LATEST = ("(select max(snapshot_date) from raw.collection_run "
          "where source='cian' and status='ok')")

MARKET = [
    market_card("Карта рынка: медиана аренды по районам", f"""
        select o.district as "Район", count(*) as "Объявлений",
               percentile_cont(0.5) within group (order by ob.price)::int as "Медиана"
        from core.offer_observation ob join core.offer o using (source, offer_id)
        where ob.source='cian' and ob.snapshot_date = {LATEST}
          and ob.price is not null and o.district is not null and not ob.is_daily
        group by 1 having count(*) >= 5
    """, "map", {"map.type": "region", "map.region": "moscow_districts",
                 "map.dimension": "Район", "map.metric": "Медиана"}),

    market_card("Подешевевшие объявления", f"""
        select pc.changed_on as "Когда", o.district as "Район",
               o.rooms_count as "Комн", o.total_area::int as "м²",
               pc.prev_price::int as "Было", pc.new_price::int as "Стало",
               pc.delta_pct as "%", pc.days_between_observations as "Дней между снимками",
               split_part(o.url,'?',1) as "Ссылка"
        from marts.price_drops pc join core.offer o using (source, offer_id)
        where pc.source='cian' order by pc.delta_pct limit 100
    """),

    market_card("Медиана по комнатности", f"""
        select o.rooms_count::text as "Комнат", count(*) as "Объявлений",
               percentile_cont(0.5) within group (order by ob.price)::int as "Медиана",
               percentile_cont(0.5) within group (order by ob.price/o.total_area)::int as "За м²"
        from core.offer_observation ob join core.offer o using (source, offer_id)
        where ob.source='cian' and ob.snapshot_date = {LATEST}
          and ob.price is not null and o.rooms_count is not null and o.total_area > 0
        group by 1 order by 1
    """),

    market_card("Животные: что известно", f"""
        select case pets_state when 'allowed' then 'Можно'
                               when 'forbidden' then 'Нельзя (из текста)'
                               when 'negotiable' then 'По договорённости'
                               else 'Не указано' end as "Животные",
               count(*) as "Объявлений"
        from core.offer_observation
        where source='cian' and snapshot_date = {LATEST}
        group by 1 order by 2 desc
    """, "row", {"graph.dimensions": ["Животные"], "graph.metrics": ["Объявлений"],
                 "graph.show_values": True, "graph.max_categories_enabled": False}),

    market_card("Дубли: одна квартира в нескольких объявлениях", """
        select matched_on as "По чему совпало",
               count(*) as "Объявлений",
               round(avg(text_similarity), 2) as "Похожесть текста"
        from core.offer_duplicate where source='cian' and matched_on <> 'представитель группы'
        group by 1
        union all
        select 'подозрительные, НЕ схлопнуты', count(*), round(avg(text_similarity), 2)
        from core.duplicate_suspect where source='cian'
        order by 2 desc
    """),

    market_card("Полнота обходов", """
        select run_id as "Запуск", mode as "Режим", snapshot_date as "Дата",
               status as "Статус", offers_seen as "Объявлений",
               buckets_total as "Корзин", buckets_underfilled as "Недобрано",
               (finished_at - started_at)::text as "Длительность"
        from raw.collection_run where source='cian' order by run_id desc limit 20
    """),
]


YEARS = [[y, str(y)] for y in range(2015, 2027)]

# Фильтры вкладки «История» — свои, отдельные от подбора: там квартиры,
# здесь годы, и общего у них ничего нет.
HISTORY_FILTERS = [
    ("year_from", "Год с", "number", "number/>=", YEARS),
    ("year_to", "Год по", "number", "number/<=", YEARS),
]

PERIOD_WHERE = """
  [[and extract(year from r.period) >= {{year_from}}]]
  [[and extract(year from r.period) <= {{year_to}}]]
"""


def history_tags():
    return {slug: {"id": str(uuid.uuid4()), "name": slug,
                   "display-name": label, "type": kind, "required": False}
            for slug, label, kind, _, _ in HISTORY_FILTERS}


def history_card(name, sql, display="table", viz=None):
    return {"name": name, "display": display, "filtered": "history",
            "dataset_query": {"type": "native", "database": DB_ID,
                              "native": {"query": sql, "template-tags": history_tags()}},
            "visualization_settings": viz or {}}


# Дефлятор: накопленный индекс потребительских цен от начала ряда.
# Без него «рост в 2.3 раза за одиннадцать лет» ничего не значит — за тот
# же срок подорожало всё. Содержательный вопрос в том, обогнала ли аренда
# инфляцию, и ответ на него даёт только сопоставление с ИПЦ.
CPI_INDEX = """
with cpi as (
  select period, exp(sum(ln(1 + mom_pct/100.0)) over (order by period)) as idx
  from core.rosstat_cpi where region='Москва')
"""

HISTORY = [
    # Три числа, ради которых люди вообще смотрят на исторический ряд.
    # Отдельными плитками, а не строкой таблицы: заголовок дашборда должен
    # отвечать на вопрос до того, как читатель дойдёт до графиков.
    # Единицы измерения прямо на плитке. Голое число без «руб.» и «%»
    # читатель вынужден домысливать, а домысливает он не всегда верно:
    # 132 — это рубли, проценты или индекс?
    history_card("Однокомнатная сейчас", """
        select price::int as v from core.rosstat_rent
        where region='Москва' and rooms=1 order by period desc limit 1
    """, "scalar", {"column_settings": {'["name","v"]': {"suffix": " ₽"}},
                    "scalar.field": "v",
                    # Без сокращения: «61.2k руб./ме…» не читается и обрезается.
                    "scalar.compact_primary_number": False}),

    history_card("Рост с 2015 года, в ценах того времени", """
        with f as (select price from core.rosstat_rent
                   where region='Москва' and rooms=1 order by period limit 1),
             l as (select price from core.rosstat_rent
                   where region='Москва' and rooms=1 order by period desc limit 1)
        select round(100*(l.price/f.price - 1)) as v from f, l
    """, "scalar", {"column_settings": {'["name","v"]': {"prefix": "+", "suffix": " %"}},
                    "scalar.field": "v", "scalar.compact_primary_number": False}),

    history_card("Рост с 2015 года, с поправкой на инфляцию", """
        with cpi as (select exp(sum(ln(1+mom_pct/100.0))) as idx
                     from core.rosstat_cpi where region='Москва'
                       and period > (select min(period) from core.rosstat_rent
                                     where region='Москва')),
             f as (select price from core.rosstat_rent
                   where region='Москва' and rooms=1 order by period limit 1),
             l as (select price from core.rosstat_rent
                   where region='Москва' and rooms=1 order by period desc limit 1)
        select round(100*(l.price/f.price/cpi.idx - 1)) as v from f, l, cpi
    """, "scalar", {"column_settings": {'["name","v"]': {"prefix": "+", "suffix": " %"}},
                    "scalar.field": "v", "scalar.compact_primary_number": False}),

    # Рост, а не уровень.
    #
    # Раньше здесь был график двух индексов, приведённых к 100. Он отвечал
    # на вопрос «обогнала ли аренда инфляцию», но не на главный: какой
    # был рост. А именно это и нужно: если цена выросла на 4%, а инфляция
    # была 6%, то роста не было — было отставание. Номинальный и реальный
    # рост рядом показывают это прямо, и величина инфляции сама по себе
    # уже не нужна.
    #
    # Реальный рост считается делением, а не вычитанием:
    # (1+рост)/(1+инфляция)-1. На малых числах разница невелика, но на
    # 2024 годе с его 34% вычитание завысило бы результат.
    history_card("Рост год к году: номинальный и реальный", f"""
        with r as (select period, price,
                          lag(price,12) over (order by period) py
                   from core.rosstat_rent where region='Москва' and rooms=1),
             c as (select period,
                          exp(sum(ln(1+mom_pct/100.0)) over (order by period)) idx
                   from core.rosstat_cpi where region='Москва'),
             j as (select r.period, r.price, r.py, c.idx,
                          lag(c.idx,12) over (order by r.period) idx_y
                   from r join c using (period))
        select period as "Месяц",
               round(100*(price/py-1), 1) as "Номинальный рост, %",
               round(100*((price/py)/(idx/idx_y)-1), 1) as "Реальный рост, %"
        from j where py is not null {PERIOD_WHERE.replace("r.period", "period")}
        order by 1
    """, "line", {"graph.dimensions": ["Месяц"],
                  "graph.metrics": ["Номинальный рост, %", "Реальный рост, %"],
                  "graph.y_axis.title_text": "Процентов к тому же месяцу год назад",
                  "graph.x_axis.title_text": "",
                  "graph.show_goal": True, "graph.goal_value": 0,
                  "graph.goal_label": "нулевой рост",
                  "series_settings": {"Номинальный рост, %": {"color": "#2a78d6"},
                                      "Реальный рост, %": {"color": "#eb6834"}}}),

    history_card("Рост месяц к месяцу: номинальный и реальный", f"""
        with r as (select period, price,
                          lag(price) over (order by period) pm
                   from core.rosstat_rent where region='Москва' and rooms=1),
             c as (select period, mom_pct from core.rosstat_cpi where region='Москва'),
             j as (select r.period, r.price, r.pm, c.mom_pct from r join c using (period))
        select period as "Месяц",
               round(100*(price/pm-1), 2) as "Номинальный рост, %",
               round(100*((price/pm)/(1+mom_pct/100.0)-1), 2) as "Реальный рост, %"
        from j where pm is not null {PERIOD_WHERE.replace("r.period", "period")}
        order by 1
    """, "line", {"graph.dimensions": ["Месяц"],
                  "graph.metrics": ["Номинальный рост, %", "Реальный рост, %"],
                  "graph.y_axis.title_text": "Процентов к предыдущему месяцу",
                  "graph.x_axis.title_text": "",
                  "graph.show_goal": True, "graph.goal_value": 0,
                  "graph.goal_label": "нулевой рост",
                  "series_settings": {"Номинальный рост, %": {"color": "#2a78d6"},
                                      "Реальный рост, %": {"color": "#eb6834"}}}),

    # Наивная сезонная модель: берём средний прирост каждого календарного
    # месяца за всю историю и продолжаем им последнее известное значение.
    # Это не предсказание рынка, а ответ на вопрос «если всё пойдёт как
    # обычно». Разброс по годам показан рядом намеренно: в ноябре он
    # больше самого прироста, и полагаться на одну цифру нельзя.
    history_card("Что будет с ценой, если сезонность повторится", """
        with mom as (
          select rooms, period, price,
                 price / lag(price) over (partition by rooms order by period) as factor
          from core.rosstat_rent where region='Москва'),
        seasonal as (
          select extract(month from period)::int as m,
                 avg(factor) as f, stddev_samp(factor) as sd, count(*) as years
          from mom where factor is not null group by 1),
        last as (
          select distinct on (rooms) rooms, period, price
          from core.rosstat_rent where region='Москва' order by rooms, period desc),
        steps as (
          select l.rooms, l.price as base, s.step,
                 (l.period + (s.step || ' month')::interval)::date as period
          from last l, generate_series(1,4) s(step)),
        proj as (
          select st.rooms, st.period, st.base, st.step, se.f, se.sd, se.years,
                 exp(sum(ln(se.f)) over (partition by st.rooms order by st.step)) as cum
          from steps st join seasonal se on se.m = extract(month from st.period)::int)
        select to_char(period,'YYYY-MM') as "Месяц",
               rooms as "Комнат",
               round(100*(f-1), 2) as "Типичный прирост, %",
               round(100*sd, 2) as "Разброс по годам, п.п.",
               (base*cum)::int as "Ожидаемая цена, руб",
               round(100*(cum-1), 1) as "Итого от последнего факта, %"
        from proj order by rooms, period
    """),

    # Две серии: выбранный период и весь ряд рядом.
    #
    # Поводом послужила настоящая путаница. При фильтре «Год с 2025» в
    # каждом столбце оставалось 2-4 наблюдения, и ноябрь показывал +0.13
    # вместо +1.29 по полному ряду — выглядело как ошибка расчёта, хотя
    # расчёт был верен. Постоянный ориентир «за весь ряд» делает разницу
    # между «так на рынке» и «так вышло на двух годах» видимой сразу,
    # а число наблюдений в подписи говорит, чему верить.
    history_card("Сезонность: аренда сверх инфляции", f"""
        with mom as (
          select r.period, r.price, c.mom_pct,
                 lag(r.price) over (partition by r.rooms order by r.period) prev
          from core.rosstat_rent r
          join core.rosstat_cpi c on c.period = r.period and c.region = 'Москва'
          where r.region = 'Москва'),
        all_years as (
          select to_char(period,'MM') m,
                 avg(100.0*(price/prev-1)) - avg(mom_pct) v
          from mom where prev is not null group by 1),
        picked as (
          select to_char(period,'MM') m, count(*) n,
                 avg(100.0*(price/prev-1)) - avg(mom_pct) v
          from mom r where prev is not null {PERIOD_WHERE}
          group by 1)
        select p.m || ' (' || p.n || ' набл.)' as "Месяц",
               round(p.v::numeric, 2) as "За выбранный период, п.п.",
               round(a.v::numeric, 2) as "За весь ряд 2015-2026, п.п."
        from picked p join all_years a using (m)
        order by p.m
    """, "bar", {"graph.dimensions": ["Месяц"],
                 "graph.metrics": ["За выбранный период, п.п.",
                                   "За весь ряд 2015-2026, п.п."],
                 "graph.show_values": True,
                 "graph.x_axis.title_text": "Календарный месяц",
                 "graph.y_axis.title_text": "Сверх инфляции, п.п.",
                 "series_settings": {"За выбранный период, п.п.": {"color": "#2a78d6"},
                                     "За весь ряд 2015-2026, п.п.": {"color": "#eb6834"}}}),

    market_card("Сверка: Росстат против наших объявлений", """
        select 'Росстат, последний месяц' as "Источник", 1 as "Комнат",
               (select price::int from core.rosstat_rent
                where region='Москва' and rooms=1 order by period desc limit 1) as "Рублей",
               'средняя по обследованию' as "Что это"
        union all
        select 'Наша панель', 1,
               (select percentile_cont(0.5) within group (order by ob.price)::int
                from core.offer_observation ob join core.offer o using (source, offer_id)
                where ob.source='cian' and o.rooms_count=1 and not ob.is_daily
                  and ob.snapshot_date = (select max(snapshot_date) from raw.collection_run
                                          where source='cian' and status='ok')),
               'медиана запрашиваемых цен'
        union all
        select 'Росстат, последний месяц', 2,
               (select price::int from core.rosstat_rent
                where region='Москва' and rooms=2 order by period desc limit 1),
               'средняя по обследованию'
        union all
        select 'Наша панель', 2,
               (select percentile_cont(0.5) within group (order by ob.price)::int
                from core.offer_observation ob join core.offer o using (source, offer_id)
                where ob.source='cian' and o.rooms_count=2 and not ob.is_daily
                  and ob.snapshot_date = (select max(snapshot_date) from raw.collection_run
                                          where source='cian' and status='ok')),
               'медиана запрашиваемых цен'
    """),
]


def _mappings(spec, card_id, slug_to_id):
    """Какие фильтры дашборда подключены к этой карточке.

    Карточки подбора слушают фильтры квартир, карточки истории — фильтры
    периода, витрины рынка не слушают никого: их база фиксирована, иначе
    сравнение «мой сегмент против рынка» поплывёт вместе с фильтром.
    """
    which = {True: FILTERS, "history": HISTORY_FILTERS}.get(spec["filtered"], [])
    return [{"parameter_id": slug_to_id[slug], "card_id": card_id,
             "target": ["variable", ["template-tag", slug]]}
            for slug, _, _, _, _ in which]


def size_for(spec):
    return {"scalar": (8, 3), "map": (24, 10), "table": (24, 13),
            "row": (24, 8), "bar": (24, 7), "line": (24, 10)}.get(spec["display"], (24, 8))


def main() -> None:
    # Существующий дашборд ОБНОВЛЯЕТСЯ, а не пересоздаётся. Иначе адрес
    # меняется при каждом запуске скрипта, и закладка в браузере ведёт
    # на архив.
    DESCRIPTION = ("Подбор — фильтры задаются сверху. "
                   "Рынок — фиксированная база, фильтры на неё не действуют.")
    existing = [d for d in api("/api/dashboard") if d["name"] == DASH_NAME]
    if existing:
        dash = existing[0]
        api(f"/api/dashboard/{dash['id']}", "PUT", {"description": DESCRIPTION})
        print("Обновляю существующий дашборд, id =", dash["id"])
    else:
        dash = api("/api/dashboard", "POST",
                   {"name": DASH_NAME, "description": DESCRIPTION})
        print("Дашборд создан, id =", dash["id"])

    params, slug_to_id = [], {}
    dlist = districts()
    for slug, label, _, ptype, values in FILTERS + HISTORY_FILTERS:
        pid = uuid.uuid4().hex[:8]
        slug_to_id[slug] = pid
        p = {"id": pid, "name": label, "slug": slug,
             "type": ptype, "sectionId": ptype.split("/")[0]}
        if values == "DISTRICTS":
            values = [[d] for d in dlist]
        if values:
            p["values_source_type"] = "static-list"
            p["values_source_config"] = {"values": values}
            p["values_query_type"] = "list"
        params.append(p)

    tabs = [{"id": -1, "name": "Подбор", "position": 0},
            {"id": -2, "name": "Рынок", "position": 1},
            {"id": -3, "name": "История", "position": 2}]

    dashcards, n = [], 0
    for tab_id, specs in ((-1, SEARCH), (-2, MARKET), (-3, HISTORY)):
        row, kpi_col = 0, 0
        for spec in specs:
            n += 1
            payload = {k: v for k, v in spec.items() if k != "filtered"}
            c = api("/api/card", "POST", payload)
            w, h = size_for(spec)
            # Плитки KPI выкладываются в одну строку, остальное — в столбец.
            if spec["display"] == "scalar" and kpi_col + w <= 24:
                col_pos, row_pos = kpi_col, row
                kpi_col += w
            else:
                if kpi_col:
                    row += 3
                    kpi_col = 0
                col_pos, row_pos = 0, row
            dc = {"id": -n, "card_id": c["id"], "dashboard_tab_id": tab_id,
                  "row": row_pos, "col": col_pos, "size_x": w, "size_y": h,
                  "parameter_mappings": _mappings(spec, c["id"], slug_to_id)}
            dashcards.append(dc)
            if spec["display"] != "scalar":
                row += h
            tab_name = {-1: "Подбор ", -2: "Рынок  ", -3: "История"}[tab_id]
            print(f"  [{tab_name}] {spec['name']}")

    api(f"/api/dashboard/{dash['id']}", "PUT",
        {"parameters": params, "tabs": tabs, "dashcards": dashcards})

    # Каждая пересборка создаёт карточки заново, и прежние остаются висеть
    # в списке вопросов, ни на что не ссылаясь. Через полтора десятка
    # запусков там десяток одноимённых дублей, и уже непонятно, какой из
    # них на дашборде. Убираем всё, что не используется ни одним дашбордом.
    live = set()
    for d in api("/api/dashboard"):
        if d.get("archived"):
            continue
        for dc in api(f"/api/dashboard/{d['id']}").get("dashcards", []):
            if dc.get("card_id"):
                live.add(dc["card_id"])
    orphans = [c for c in api("/api/card")
               if not c["archived"] and c["id"] not in live]
    for c in orphans:
        api(f"/api/card/{c['id']}", "PUT", {"archived": True})
    if orphans:
        print(f"Убрано в архив неиспользуемых карточек: {len(orphans)}")

    print(f"\nГотово: {MB}/dashboard/{dash['id']}")


if __name__ == "__main__":
    main()
