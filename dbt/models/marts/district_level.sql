{{ config(materialized='table',
          indexes=[{'columns': ['district']}],
          post_hook='analyze {{ this }}') }}

with d as (
  select district
       , count(*)                                                    as offers
       , percentile_cont(0.5) within group (order by price / total_area) as ppm
  from {{ ref('offer_segment') }}
  where source = 'cian' and price is not null and total_area > 0
    and district is not null and rooms_count in (1, 2)
    and not coalesce(is_duplicate, false)
    and not coalesce(is_price_outlier, false)
  group by 1 having count(*) >= 10),
city as (select percentile_cont(0.5) within group (order by ppm) as ppm from d)
select d.district, d.offers, d.ppm::numeric(12,0) as district_ppm,
       (d.ppm / city.ppm)::numeric(6,3) as ppm_vs_city
from d, city
