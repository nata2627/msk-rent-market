{{ config(materialized='view') }}

-- Колонки объявления перечислены явно, а не через o.*: у обеих таблиц есть
-- source и offer_id, и звёздочка с двух сторон дала бы две одноимённые
-- колонки — Postgres такое представление не создаст.
select distinct on (ob.source, ob.offer_id)
       ob.*,
       o.url, o.first_seen_date, o.last_seen_date, o.is_active,
       o.disappeared_date, o.published_at, o.rooms_count, o.flat_type,
       o.is_apartments, o.total_area, o.living_area, o.kitchen_area,
       o.floor, o.floors_total, o.building_material, o.build_year, o.has_lift,
       o.address, o.lat, o.lon, o.okrug, o.district, o.street, o.house,
       o.metro_walk_min, o.metro_real_walk_min,
       o.park_distance_m, o.large_park_distance_m,
       o.inside_mkad
from {{ source('core', 'offer') }} o
join {{ source('core', 'offer_observation') }} ob
  on ob.source = o.source and ob.offer_id = o.offer_id
order by ob.source, ob.offer_id, ob.snapshot_date desc
