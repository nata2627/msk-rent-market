{{ config(materialized='table',
          indexes=[{'columns': ['snapshot_date', 'okrug', 'district', 'rooms_count']}],
          post_hook='analyze {{ this }}') }}

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
from {{ source('core', 'offer_observation') }} ob
join {{ source('core', 'offer') }} o on o.source = ob.source and o.offer_id = ob.offer_id
where ob.price is not null
  and not ob.is_daily
  and o.total_area is not null
group by ob.snapshot_date, o.okrug, o.district, o.rooms_count
