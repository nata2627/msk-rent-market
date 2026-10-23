-- Подбор одним определением: в offer_pick не должно попасть то, что
-- он исключает. Иначе бот и дашборд снова покажут разное.
select source, offer_id
from {{ ref('offer_pick') }}
where is_duplicate or is_room_share or is_daily or price is null
