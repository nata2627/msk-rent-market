{{ config(materialized='table',
          indexes=[{'columns': ['district', 'rooms_count']}],
          post_hook='analyze {{ this }}') }}

select district
     , rooms_count
     , count(*)                                                          as comparable_offers
     , percentile_cont(0.5) within group (order by price)                as med_price
     , percentile_cont(0.5) within group (order by total_area)           as med_area
from {{ ref('offer_segment') }}
where source = 'cian'
  and price is not null and total_area > 0
  and district is not null and rooms_count is not null
  and not coalesce(is_duplicate, false)
  and not coalesce(is_price_outlier, false)
  and not coalesce(is_daily, false)
group by 1, 2
having count(*) >= 5
