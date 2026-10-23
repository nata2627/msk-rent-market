{{ config(materialized='table',
          indexes=[{'columns': ['exit_date', 'rooms_count']}],
          post_hook='analyze {{ this }}') }}

with e as (
    select s.last_seen_date as d, s.rooms_count, s.price
    from {{ ref('offer_scored') }} s
    where s.source = 'cian' and s.rooms_count in (1, 2)
      and s.disappeared_date is not null
      and s.price > 0 and s.total_area > 0
      and not coalesce(s.is_duplicate, false)
      and not coalesce(s.is_room_share, false)
      and not coalesce(s.is_price_outlier, false))
select d as exit_date, rooms_count::smallint as rooms_count, count(*) as exits,
       percentile_disc(0.5) within group (order by price)::int as med_price
from e group by 1, 2
union all
select d, 0::smallint, count(*),
       percentile_disc(0.5) within group (order by price)::int
from e group by 1
