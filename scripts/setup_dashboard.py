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
import time
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
# Шестое поле — колонка витрины для «фильтра по полю» Metabase.
#
# Обычная переменная в нативном запросе принимает РОВНО одно значение:
# выбрать «1 и 2 комнаты» ею нельзя в принципе. Множественный выбор умеют
# только фильтры по полю — те, что привязаны к настоящей колонке таблицы.
# Поэтому комнатность, животные и парк переведены на них, а остальные
# фильтры остались переменными: для «цена до» и «площадь от» несколько
# значений и не нужны.
FILTERS = [
    ("max_price", "Цена до, руб/мес", "number", "number/<=", None, None),
    ("min_area", "Площадь от, м²", "number", "number/>=", None, None),
    ("rooms", "Комнат", "number", "number/=",
     [[1, "1"], [2, "2"], [3, "3"], [4, "4"], [5, "5"]], "rooms_count"),
    ("max_metro", "До метро пешком", "number", "number/<=",
     [[5, "до 5 минут"], [10, "до 10 минут"], [15, "до 15 минут"], [20, "до 20 минут"]],
     None),
    # Полосы не вложены друг в друга — иначе множественный выбор не имел
    # бы смысла: «до 1 км» и так включало бы «до 500 м».
    ("park", "Парк рядом", "text", "string/=",
     [["до 500 м, пешком"], ["500 м – 1 км"], ["1–2 км"], ["2–5 км"],
      ["дальше 5 км"], ["не известно"]], "park_band"),
    ("pets", "Животные", "text", "string/=",
     [["allowed", "можно"], ["negotiable", "по договорённости"],
      ["forbidden", "нельзя"], ["unknown", "не указано"]], "pets_state"),
    # На этапе отсева вопрос двоичный: платить посреднику или нет.
    ("fee", "Комиссия", "text", "string/=",
     [["no", "без комиссии"], ["yes", "с комиссией"]], None),
    ("freshness", "Опубликовано", "number", "number/<=",
     [[0, "сегодня"], [3, "за 3 дня"], [7, "за неделю"], [30, "за месяц"]], None),
    ("max_entry", "Нужно денег сразу, до", "number", "number/<=", None, None),
    # Считается по настоящей геометрии кольца, а не по списку районов:
    # Выхино-Жулебино лежит по обе стороны, и никакой список этого не
    # разделит. Заголовок «Где» ничего не объяснял: в соседнем фильтре
    # «Район» тоже про «где», и какой из двух что задаёт, было непонятно.
    ("mkad", "Внутри МКАД", "text", "string/=",
     [["in", "да"], ["out", "нет, за МКАД"]], None),
    ("district", "Район", "text", "string/=", "DISTRICTS", None),
]

# Сумма на входе переехала в витрину: она нужна и таблице, и фильтру, и
# оценке, а считаться должна один раз и одинаково. Заодно там же лежит
# признак deposit_assumed — залог не был указан и взят по умолчанию.
ENTRY = "entry_cost"

WHERE = f"""
  -- Дубли из подбора убираются: одна квартира не должна занимать три
  -- строки списка. В аналитике на вкладке «Рынок» они остаются видны.
  and coalesce(is_duplicate, false) = false
  -- Комната в квартире — не квартира: площадь указана всей квартиры,
  -- и в подборе такое объявление выглядит как дешёвая большая.
  and coalesce(is_room_share, false) = false
  and coalesce(is_daily, false) = false
  -- Апартаменты НЕ исключаются: они получают минимум очков в оценке и
  -- уходят вниз списка сами. Собирать и показывать — разные вещи, а
  -- апартаменты иногда стоят того, если всё остальное хорошо.
  and coalesce(lease_term_type, 'longTerm') = 'longTerm'
  and price is not null
  [[and price <= {{{{max_price}}}}]]
  [[and total_area >= {{{{min_area}}}}]]
  -- Фильтры по полю подставляют условие целиком, вместе с именем
  -- колонки и оператором: отсюда голое {{{{rooms}}}} без «=».
  [[and {{{{rooms}}}}]]
  -- Метрополитен, без МЦК и диаметров: диаметр ходит по расписанию
  -- с интервалом в 10-15 минут, и «семь минут до станции» означает
  -- совсем другое время в пути.
  [[and metro_real_walk_min <= {{{{max_metro}}}}]]
  [[and {{{{park}}}}]]
  [[and {{{{pets}}}}]]
  [[and district = {{{{district}}}}]]
  [[and {ENTRY} <= {{{{max_entry}}}}]]
  [[and ((({{{{fee}}}}) = 'no'  and coalesce(client_fee_pct, 0) = 0)
      or (({{{{fee}}}}) = 'yes' and coalesce(client_fee_pct, 0) > 0))]]
  [[and published_at >= current_date - ({{{{freshness}}}})]]
  [[and ((({{{{mkad}}}}) = 'in'  and inside_mkad)
      or (({{{{mkad}}}}) = 'out' and not inside_mkad))]]
"""


def segment_fields() -> dict[str, int]:
    """Идентификаторы колонок marts.offer_scored в Metabase.

    Ищутся по имени, а не зашиты числами: идентификаторы свои у каждой
    установки Metabase, и зашитые превратили бы скрипт в работающий
    ровно на одной машине. Свежедобавленные колонки Metabase видит только
    после синхронизации схемы, поэтому она запрашивается заранее.
    """
    api(f"/api/database/{DB_ID}/sync_schema", "POST")
    for _ in range(30):
        for t in api(f"/api/database/{DB_ID}/metadata").get("tables", []):
            if t["schema"] == "marts" and t["name"] == "offer_scored":
                found = {f["name"]: f["id"] for f in t["fields"]}
                if "park_band" in found:
                    return found
        time.sleep(2)
    raise RuntimeError("Metabase не увидел marts.offer_scored с колонкой park_band")


_FIELDS: dict[str, int] | None = None


def fields() -> dict[str, int]:
    """Идентификаторы колонок, запрошенные один раз за запуск."""
    global _FIELDS
    if _FIELDS is None:
        _FIELDS = segment_fields()
    return _FIELDS


def tags(fields: dict[str, int]):
    out = {}
    for slug, label, kind, ptype, _, column in FILTERS:
        tag = {"id": str(uuid.uuid4()), "name": slug,
               "display-name": label, "required": False}
        if column:
            # Фильтр по полю: только он умеет несколько значений сразу.
            tag |= {"type": "dimension",
                    "dimension": ["field", fields[column], None],
                    "widget-type": ptype}
        else:
            tag["type"] = kind
        out[slug] = tag
    return out


# Формула оценки словами — она же висит подсказкой у заголовка таблицы.
#
# Держится здесь копией, а не выводится из базы: веса живут в
# marts.offer_scored, достать их оттуда можно только разбором текста
# представления, и лекарство вышло бы хуже болезни. ПРАВИТЬ ВМЕСТЕ С
# marts.offer_scored — разъехавшаяся подсказка хуже отсутствующей.
SCORE_HELP = """Оценка 0–100 — не модель и не предсказание, а взвешенная сумма предпочтений. Сумма весов ровно 100, поэтому итог читается как проценты.

ДОМ · вес 18 — эпоха и материал вместе.
старый фонд до 1918 и сталинка 100 · до 1960 — 90 · монолит-кирпич 85 · монолит 78 · кирпич 75 · новостройка от 2010 — 75 · 1991–2009 — 45 · панель и блок 30.

МЕТРО · вес 15 — только метрополитен, без МЦД и МЦК.
до 10 минут пешком 100 · до 15 — 90 · до 20 — 80 · до 25 — 55 · до 30 — 35 · дальше 20 · пешего метро нет 15.

РАЙОН · вес 15 — медиана цены за м² района к медиане по городу.
балл = 50 + (отношение − 1) × 120. Разброс по городу от 0,58 до 1,93.

ЦЕНА · вес 14 — СИГНАЛ КАЧЕСТВА, а не выгода.
балл = 50 + (цена ÷ медиана похожих − 1) × 250, обрезано в 0–100.
Дороже похожих — выше балл. Знак перевёрнут намеренно: на панели связь отклонения цены с годом постройки +0,30, с упоминанием «требует ремонта» −0,13. Дешевизна означает не удачу, а то, чего в данных не видно. Бюджет задаётся фильтром «Цена до», а не оценкой.

ПЛОЩАДЬ · вес 12 — так же относительно похожих.
балл = 50 + (площадь ÷ медиана похожих − 1) × 250.

ПАРК · вес 10 — до парка от 10 гектаров.
до 500 м 100 · до 1 км 75 · до 2 км 50 · до 5 км 25 · дальше 0.

ЖИВОТНЫЕ · вес 8 — можно 100 · по договорённости 95 · не указано 50 · нельзя 0.

АПАРТАМЕНТЫ · вес 5 — обычная квартира 100 · апартаменты 0 · не указано 50.

ЭТАЖ · вес 3 — средний 100 · последний 70 · первый 40.

«Похожие» — тот же район и та же комнатность, ячейка не меньше пяти объявлений. Где сравнивать не с чем, цена и площадь дают нейтральные 50.

Чего в оценке НЕТ: типа ремонта. Выдача поиска его не отдаёт вовсе, он есть только на странице объявления. Пока его место держит эпоха дома."""


def search_card(name, select, display="table", viz=None, order="order by price",
                wrap=None, cte=None, description=None):
    sql = f"select {select}\nfrom marts.offer_scored\nwhere source = 'cian'\n{WHERE}\n{order}"
    if cte:
        # Отобранное становится общим выражением, а поверх него считается
        # что угодно — например, границы шкалы гистограммы, которые без
        # второго прохода по той же выборке не получить.
        #
        # MATERIALIZED обязателен. По умолчанию Postgres подставляет тело
        # выражения в каждое место использования, а здесь их два — расчёт
        # границ и раскладка по корзинам. Витрина с оценкой пересчитывалась
        # дважды, и гистограмма под фильтрами рисовалась 11 секунд вместо
        # полутора.
        sql = f"with t as materialized (\n{sql}\n)\n{cte}"
    if wrap:
        # Двухступенчатая сортировка: сначала отбор, потом показ.
        #
        # Наивное «order by район» вместе с limit 300 отрезало бы всё после
        # буквы «М» — Ясенево и Южнопортовый исчезли бы из таблицы молча.
        # Отбираем самые дешёвые, а по алфавиту раскладываем уже их.
        sql = f"select * from (\n{sql}\n) t\n{wrap}"
    card = {"name": name, "display": display, "filtered": True,
            "dataset_query": {"type": "native", "database": DB_ID,
                              "native": {"query": sql, "template-tags": tags(fields())}},
            "visualization_settings": viz or {}}
    if description:
        card["description"] = description
    return card


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
    # Оценка первым столбцом и сортировка по ней.
    #
    # Смысл таблицы поменялся: раньше это был список «всё, что подошло по
    # фильтрам», и читать его приходилось подряд, открывая ссылки одну за
    # другой. Теперь сверху лежит то, что больше похоже на искомое, и
    # хватает первых двух десятков строк. Состав оценки — в комментарии
    # к marts.offer_scored, там же веса.
    search_card("Найденные квартиры", f"""
           score as "Оценка",
           split_part(url, '?', 1) as "Ссылка",
           -- Пустой район подписывается явно. В сортировке по алфавиту
           -- null уходит в самый конец, и десяток безымянных строк внизу
           -- выглядит как обрыв таблицы, а не как «площадка не указала».
           coalesce(district, '— район не указан') as "Район",
           coalesce(rooms_count::text, 'студия') as "Комн",
           case when is_apartments then 'апартаменты' else '' end as "Тип",
           total_area::int as "м²",
           price::int as "Цена", (price / total_area)::int as "За м²",
           -- Цена относительно похожих квартир — то же, что стоит за
           -- главным весом оценки, но в понятном виде: «на 12% дешевле».
           case when med_price is null then null
                else round(100 * (price / med_price - 1))::int end as "К похожим, %",
           {ENTRY}::int as "Нужно сразу",
           -- Залог виден отдельно, потому что у трети объявлений он
           -- не указан вовсе и в сумме стоит допущение, а не факт.
           case when deposit_assumed then '≈ месяц (не указан)'
                else to_char(deposit_rub, 'FM999 999 999') end as "Залог",
           case when coalesce(client_fee_pct, 0) > 0
                then client_fee_pct::int || '%' else 'нет' end as "Комиссия",
           park_band as "Парк",
           metro_real_walk_min as "Метро, мин",
           floor || '/' || floors_total as "Этаж",
           -- Эпоха и материал вместе: кирпич 1967 года и панель 1970-го —
           -- разные дома, и рынок платит за метр в них по-разному.
           coalesce(build_year::text, '?') || ' ' ||
           case building_material when 'stalin' then 'сталинка'
                                  when 'old' then 'старый фонд'
                                  when 'brick' then 'кирпич'
                                  when 'monolith' then 'монолит'
                                  when 'monolithBrick' then 'монолит-кирпич'
                                  when 'panel' then 'панель'
                                  when 'block' then 'блок'
                                  else '' end as "Дом",
           -- Уровень района относительно города по цене за метр.
           round(ppm_vs_city, 2) as "Район к городу",
           case pets_state when 'allowed' then 'можно'
                           when 'negotiable' then 'по договорённости'
                           when 'forbidden' then 'нельзя'
                           else 'не указано' end as "Животные",
           -- Долго висящее объявление — повод торговаться.
           (current_date - published_at::date) as "Висит, дней\"""",
                description=SCORE_HELP,
                wrap='order by "Оценка" desc, "Цена"',
                viz={"column_settings": {
                        '["name","Ссылка"]': {
                            "view_as": "link",
                            "link_url": "{{Ссылка}}",
                            "link_text": "открыть",
                        }},
                     "table.cell_column": "Оценка"},
                order="order by score desc, price limit 300"),

    # Шкала подстраивается под выборку, а не фиксирована.
    #
    # Раньше ширина корзины была жёстко 5000 рублей на всём диапазоне до
    # самого дорогого объявления. Без фильтров это давало больше семидесяти
    # столбцов, растянутых хвостом до 350 тысяч, с подписью на каждом —
    # читать такое нельзя: сорок столбцов правее медианы содержат по
    # одному-два объявления, а весь рынок сжат в левую пятую часть.
    #
    # Теперь границы берутся по 5-му и 95-му перцентилям самой выборки,
    # хвосты с обеих сторон сводятся в крайние столбцы («меньше N»,
    # «больше N») и не растягивают шкалу, а ширина корзины считается так,
    # чтобы столбцов было около двух десятков при любых фильтрах.
    #
    # floor, а не ::int: приведение к целому в Postgres ОКРУГЛЯЕТ, и
    # корзины получались центрированными вместо выровненных по границе —
    # 32 000 попадало в «30», а 33 000 уже в «35». Для гистограммы это
    # означает, что подписи не соответствуют содержимому столбца.
    search_card("Как распределены цены по этому запросу", "price",
                "bar",
                {"graph.dimensions": ["Цена, тыс. руб"], "graph.metrics": ["Объявлений"],
                 # Подписи на столбцах сняты: при двух десятках корзин они
                 # наезжают друг на друга, а высоту и так видно по оси.
                 "graph.show_values": False,
                 "graph.x_axis.title_text": "Цена, тысяч рублей в месяц",
                 "graph.y_axis.title_text": "Сколько объявлений",
                 # Метабейз по умолчанию сокращает числа до «5.2k». Для
                 # рублей это читается как чужая валюта, а не как тысячи.
                 "column_settings": {'["name","Объявлений"]': {"compact": False}}},
                order="",
                cte="""
, lim as (
    select percentile_cont(0.05) within group (order by price) as lo,
           percentile_cont(0.95) within group (order by price) as hi
    from t),
  w as (
    -- Ширина корзины — всегда кратная пяти тысячам, и не меньше пяти.
    -- Корзин около четырнадцати, а не двадцати: при двадцати Metabase
    -- не помещает все подписи и прячет каждую вторую, отчего в шкале
    -- мерещатся пропуски — «есть 70-75, есть 80-85, а где 75-80?».
    --
    -- Узкие корзины дают гребёнку, а не распределение: арендные ставки
    -- липнут к круглым числам, и при корзине в две тысячи столбцы с
    -- 50, 55, 60 тысячами оказываются втрое выше соседних, где круглого
    -- числа просто нет. Видна не форма распределения, а привычка людей
    -- округлять цену.
    select lo, hi,
           greatest(5000, round((hi - lo) / 14.0 / 5000) * 5000) as width
    from lim),
  aligned as (
    -- Граница первой корзины опускается до кратной ширине. Иначе корзины
    -- начинаются от 5-го перцентиля — скажем, от 47 300, — и круглые
    -- ценники снова раскладываются по столбцам через один.
    select floor(lo / width) * width as lo, hi, width from w),
  binned as (
    select case when t.price <  a.lo then -1
                when t.price >= a.hi then 9999
                else floor((t.price - a.lo) / a.width)::int end as bin,
           a.lo, a.hi, a.width
    from t, aligned a)
select case when bin = -1   then 'меньше ' || round(lo / 1000)::int
            when bin = 9999 then 'больше ' || round(hi / 1000)::int
            else round((lo + bin * width) / 1000)::int || '–' ||
                 round((lo + (bin + 1) * width) / 1000)::int
       end as "Цена, тыс. руб",
       count(*) as "Объявлений"
from binned
group by bin, lo, hi, width
order by bin"""),

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
                 # Без сокращения: ось в рублях, и «80k» читается хуже,
                 # чем 80 000.
                 "column_settings": {'["name","Медиана"]': {"compact": False}},
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
    ("year_from", "Год с", "number", "number/>=", YEARS, None),
    ("year_to", "Год по", "number", "number/<=", YEARS, None),
]

PERIOD_WHERE = """
  [[and extract(year from r.period) >= {{year_from}}]]
  [[and extract(year from r.period) <= {{year_to}}]]
"""


def history_tags():
    return {slug: {"id": str(uuid.uuid4()), "name": slug,
                   "display-name": label, "type": kind, "required": False}
            for slug, label, kind, _, _, _ in HISTORY_FILTERS}


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
    # У фильтра по полю цель другого рода: «dimension», а не «variable».
    # С неверным типом Metabase молча создаёт связь, которая ни на что не
    # влияет — карточка отвечает, но фильтр её не трогает, и понять это
    # по виду дашборда невозможно.
    return [{"parameter_id": slug_to_id[slug], "card_id": card_id,
             "target": [("dimension" if column else "variable"),
                        ["template-tag", slug]]}
            for slug, _, _, _, _, column in which]


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
    for slug, label, _, ptype, values, _ in FILTERS + HISTORY_FILTERS:
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

        # Формула оценки внизу вкладки подбора.
        #
        # Она же висит подсказкой у заголовка таблицы, но подсказка
        # показывается по наведению и может обрезаться, а формулу надо
        # уметь прочитать целиком и спокойно. Внизу — чтобы не отодвигать
        # сам список вариантов.
        if tab_id == -1:
            n += 1
            dashcards.append({
                "id": -n, "card_id": None, "dashboard_tab_id": tab_id,
                "row": row, "col": 0, "size_x": 24, "size_y": 12,
                "parameter_mappings": [],
                "visualization_settings": {
                    "virtual_card": {"name": None, "display": "text",
                                     "visualization_settings": {},
                                     "dataset_query": {}, "archived": False},
                    "text": "## Как считается оценка\n\n" + SCORE_HELP,
                    "text.align_vertical": "top",
                },
            })
            print("  [Подбор ] Как считается оценка")

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
