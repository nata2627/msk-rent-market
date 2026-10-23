{{ config(materialized='table',
          indexes=[{'columns': ['source', 'offer_id']}],
          post_hook='analyze {{ this }}') }}

with scored as (
select s.*
     , c.comparable_offers
     , c.med_price
     , c.med_area
     , dl.ppm_vs_city
     -- ЦЕНА КАК СИГНАЛ КАЧЕСТВА, а не как выгода. Знак перевёрнут.
     --
     -- Сначала балл рос, когда квартира дешевле похожих, — и наверх
     -- лезло всё дешёвое и убитое. Измерено на панели: связь отклонения
     -- цены от медианы района с годом постройки дома +0.30, с упоминанием
     -- «требует ремонта» в описании −0.13. То есть цена — это оценка
     -- качества, которую рынок уже сделал за нас, и дешевизна означает
     -- не удачу, а то, чего мы не видим: состояние, вид, шум, этаж.
     --
     -- Цена за метр проверена как замена и оказалась не лучше: связь с
     -- годом дома 0.276 против 0.300, с плохим ремонтом −0.143 против
     -- −0.134. Разница в третьем знаке, поэтому оставлена цена целиком —
     -- площадь уже учтена отдельной составляющей.
     --
     -- Насыщение на +20%: выше это уже не качество, а оптимизм
     -- арендодателя. Бюджет задаётся фильтром «цена до», а не здесь.
     --
     -- ОГОВОРКА: составляющая отчасти тавтологична — «дорогое значит
     -- хорошее». Это заплатка на время, пока нет типа ремонта; он есть
     -- только на странице объявления.
     , coalesce(greatest(0, least(100,
         50 + (s.price / nullif(c.med_price, 0) - 1) * 250)), 50) as score_price
     , coalesce(greatest(0, least(100,
         50 + (s.total_area / nullif(c.med_area, 0) - 1) * 250)), 50) as score_area
     -- УРОВЕНЬ РАЙОНА. Без него оценка сравнивала квартиру только с
     -- соседями по двору и не отличала Хамовники от Ново-Переделкино.
     , coalesce(greatest(0, least(100,
         50 + (dl.ppm_vs_city - 1) * 120)), 50) as score_district
     -- ДОМ: эпоха и материал вместе, а не один год постройки.
     --
     -- Прежняя шкала «новее — лучше» противоречит вкусу владелицы
     -- (старый фонд) и данным: рынок платит за метр в домах до 1918 года
     -- 2444 рубля, в сталинках 2120, а в панели 1961-1990 — 1579.
     -- Материал добавляет то, чего год не знает: кирпич 1967 года и
     -- панель 1970-го — разные дома, и разница в цене за метр 350 рублей.
     --
     -- Самая дешёвая эпоха вовсе не панель, а 1991-2009 (1451 за метр) —
     -- поздняя панель и блок без обаяния старого фонда и без качества
     -- новостроек.
     , case when s.building_material in ('old', 'stalin')            then 100
            when s.build_year is not null and s.build_year < 1918    then 100
            when s.build_year between 1918 and 1960                  then  90
            when s.building_material = 'monolithBrick'               then  85
            when s.building_material = 'monolith'                    then  78
            when s.building_material = 'brick'                       then  75
            when s.building_material in ('panel', 'block')           then  30
            when s.build_year >= 2010                                then  75
            when s.build_year between 1991 and 2009                  then  45
            when s.build_year between 1961 and 1990                  then  35
            else 50 end as score_building
     -- МЕТРО — только метрополитен, без МЦК и диаметров.
     --
     -- Диаметр ходит по расписанию с интервалом в 10-15 минут, метро —
     -- каждые две, и «семь минут до станции» означает совершенно разное
     -- время в пути. Из 8958 объявлений с пешей доступностью до 10 минут
     -- 889 держались на диаметре, а у 914 пешего метро нет вовсе.
     , case when s.metro_real_walk_min is null      then  15
            when s.metro_real_walk_min <= 10        then 100
            when s.metro_real_walk_min <= 15        then  90
            when s.metro_real_walk_min <= 20        then  80
            when s.metro_real_walk_min <= 25        then  55
            when s.metro_real_walk_min <= 30        then  35
            else 20 end as score_metro
     , case when s.large_park_distance_m is null  then 25
            when s.large_park_distance_m <= 500   then 100
            when s.large_park_distance_m <= 1000  then 75
            when s.large_park_distance_m <= 2000  then 50
            when s.large_park_distance_m <= 5000  then 25
            else 0 end as score_park
     -- ЖИВОТНЫЕ. Явное разрешение и «по договорённости» почти равны:
     -- разница в пять очков, как и просила владелица. «Не указано» —
     -- середина, это 57% рынка и повод спросить, а не повод вычеркнуть.
     , case when s.pets_state = 'allowed'    then 100
            when s.pets_state = 'negotiable' then  95
            when s.pets_state = 'forbidden'  then   0
            else 50 end as score_pets
     -- Апартаменты — минимум очков, но НЕ исключение из подбора.
     -- Жилыми помещениями они не считаются, коммунальные платежи выше.
     , case when s.is_apartments is true then 0
            when s.is_apartments is false then 100
            else 50 end as score_apartments
     -- ЭТАЖ. Рынок оценивает это сам: первый этаж стоит 1571 рубль за
     -- метр против 1786 на средних, последний — 1700.
     , case when s.floor is null or s.floors_total is null then 70
            when s.floor = 1                               then 40
            when s.floor = s.floors_total                  then 70
            else 100 end as score_floor
from {{ ref('offer_segment') }} s
left join {{ ref('comparable_median') }} c
       on c.district = s.district and c.rooms_count = s.rooms_count
left join {{ ref('district_level') }} dl
       on dl.district = s.district
)
-- Веса. Сумма ровно 100, чтобы итог читался как проценты.
--
-- Оценка за мебель убрана: смысла в ней владелица не увидела, а 97%
-- объявлений имели одно и то же значение, так что она почти ничего не
-- различала.
--
-- Веса спорны по построению, и спорить с ними нужно: опись полей с
-- пустой колонкой под свои веса собирается командой
-- `python -m etl.field_report > docs/fields.md`.
select scored.*
     , round( 0.18 * score_building
            + 0.15 * score_metro
            + 0.15 * score_district
            + 0.14 * score_price
            + 0.12 * score_area
            + 0.10 * score_park
            + 0.08 * score_pets
            + 0.05 * score_apartments
            + 0.03 * score_floor )::int as score
from scored
