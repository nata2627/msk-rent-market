{{ config(materialized='view') }}

-- ---------------------------------------------------------------------
-- Подешевевшие объявления: прямая практическая польза к ноябрю.
-- ---------------------------------------------------------------------
select pc.*
     , o.district
     , o.rooms_count
     , o.total_area
     , o.url
     , o.first_seen_date
     , pc.changed_on - o.first_seen_date as days_on_market_at_drop
from {{ source('core', 'price_change') }} pc
join {{ source('core', 'offer') }} o on o.source = pc.source and o.offer_id = pc.offer_id
where pc.direction = 'down'
