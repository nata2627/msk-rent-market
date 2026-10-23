-- Активное объявление с датой исчезновения противоречит само себе.
select source, offer_id
from {{ source('core', 'offer') }}
where is_active and disappeared_date is not null
