-- =====================================================================
-- Слой 3: витрины.
--
-- Здесь и только здесь появляется сегмент заказчицы. Важно, что он —
-- разметка над полными данными, а не фильтр сбора: рынок собран целиком,
-- а сегмент это столбец, по которому его можно разрезать. Поменяется
-- представление о желаемой квартире — поменяется одно представление,
-- а пересобирать данные не придётся.
-- =====================================================================

create schema if not exists marts;

-- ---------------------------------------------------------------------
-- Последнее наблюдение по каждому объявлению.
-- ---------------------------------------------------------------------
-- Представления пересоздаются, а не заменяются. `create or replace view`
-- не умеет менять состав и порядок колонок, а offer_latest построено через
-- ob.* и обязано подхватывать новые колонки панели при каждом make schema.
-- CASCADE снимает зависящее offer_segment, которое создаётся следом.
drop view if exists marts.offer_latest cascade;

-- Колонки объявления перечислены явно, а не через o.*: у обеих таблиц есть
-- source и offer_id, и звёздочка с двух сторон дала бы две одноимённые
-- колонки — Postgres такое представление не создаст.
create or replace view marts.offer_latest as
select distinct on (ob.source, ob.offer_id)
       ob.*,
       o.url, o.first_seen_date, o.last_seen_date, o.is_active,
       o.disappeared_date, o.published_at, o.rooms_count, o.flat_type,
       o.is_apartments, o.total_area, o.living_area, o.kitchen_area,
       o.floor, o.floors_total, o.building_material, o.build_year, o.has_lift,
       o.address, o.lat, o.lon, o.okrug, o.district, o.street, o.house,
       o.metro_walk_min, o.park_distance_m, o.large_park_distance_m,
       o.inside_mkad
from core.offer o
join core.offer_observation ob
  on ob.source = o.source and ob.offer_id = o.offer_id
order by ob.source, ob.offer_id, ob.snapshot_date desc;

-- ---------------------------------------------------------------------
-- Разметка сегмента.
--
-- Критерии заказчицы: 1-2 комнаты, от 40 кв.м, животные разрешены или
-- по договорённости, длительная аренда, не апартаменты, парк рядом.
--
-- Парк вынесен в ОТДЕЛЬНЫЙ флаг, а не вшит в is_my_segment. Причина:
-- park_distance_m заполняется отдельным шагом обогащения по OpenStreetMap
-- и до него равен NULL. Если бы критерий парка входил в основной флаг,
-- до обогащения сегмент был бы пуст, и это выглядело бы как поломка сбора,
-- а не как незаполненное поле.
-- ---------------------------------------------------------------------
-- Дубль ли это объявление: у группы одинаковых остаётся один
-- представитель, остальные помечаются. Не удаляются: сырые данные и
-- панель не трогаются, а аналитика сама решает, фильтровать или нет.
create or replace view marts.offer_segment as
select l.*
     , coalesce(d.canonical_id, l.offer_id) <> l.offer_id as is_duplicate
     , d.matched_on as duplicate_reason
     , (    l.rooms_count in (1, 2)
        and l.total_area >= 40
        and l.pets_state in ('allowed', 'negotiable')
        and coalesce(l.is_apartments, false) = false
        and coalesce(l.is_daily, false) = false
        and coalesce(l.lease_term_type, 'longTerm') = 'longTerm'
       ) as is_my_segment
     -- Парк считается по КРУПНОМУ парку, а не по любому: сквером между
     -- домами в OpenStreetMap помечено больше половины зелёных контуров,
     -- и по ним «парк рядом» оказывается у 88% объявлений. По паркам от
     -- 10 га — у 17%, и критерий начинает различать.
     , (l.large_park_distance_m is not null and l.large_park_distance_m <= 1000)
         as is_near_park
from marts.offer_latest l
left join core.offer_duplicate d
       on d.source = l.source and d.offer_id = l.offer_id;

-- ---------------------------------------------------------------------
-- Медианы по району и комнатности в динамике.
--
-- Медиана, а не среднее: распределение арендных ставок скошено вправо,
-- несколько объявлений за миллион рублей утащат среднее туда, где не живёт
-- ни одна реальная квартира.
--
-- Материализованное представление: запрос тяжёлый, а данные меняются раз
-- в сутки. Обновляется командой `make marts`.
-- ---------------------------------------------------------------------
create materialized view if not exists marts.median_price_by_district as
select ob.snapshot_date
     , o.okrug
     , o.district
     , o.rooms_count
     , count(*)                                                        as offers
     , percentile_cont(0.25) within group (order by ob.price)::numeric(12,0) as p25
     , percentile_cont(0.50) within group (order by ob.price)::numeric(12,0) as median_price
     , percentile_cont(0.75) within group (order by ob.price)::numeric(12,0) as p75
     , percentile_cont(0.50) within group (order by ob.price / nullif(o.total_area, 0))::numeric(12,0)
                                                                       as median_price_per_sqm
from core.offer_observation ob
join core.offer o on o.source = ob.source and o.offer_id = ob.offer_id
where ob.price is not null
  and not ob.is_daily
  and o.total_area is not null
group by ob.snapshot_date, o.okrug, o.district, o.rooms_count;

create unique index if not exists median_price_by_district_key
    on marts.median_price_by_district(snapshot_date, okrug, district, rooms_count);

-- ---------------------------------------------------------------------
-- Подешевевшие объявления: прямая практическая польза к ноябрю.
-- ---------------------------------------------------------------------
create or replace view marts.price_drops as
select pc.*
     , o.district
     , o.rooms_count
     , o.total_area
     , o.url
     , o.first_seen_date
     , pc.changed_on - o.first_seen_date as days_on_market_at_drop
from core.price_change pc
join core.offer o on o.source = pc.source and o.offer_id = pc.offer_id
where pc.direction = 'down';

-- ---------------------------------------------------------------------
-- Обновление всех материализованных витрин одной командой.
-- CONCURRENTLY требует уникального индекса (он выше) и не блокирует чтение.
-- ---------------------------------------------------------------------
create or replace function marts.refresh_all() returns void as $$
begin
    refresh materialized view concurrently marts.median_price_by_district;
exception when others then
    -- CONCURRENTLY падает, если представление ещё ни разу не наполнялось.
    refresh materialized view marts.median_price_by_district;
end;
$$ language plpgsql;
